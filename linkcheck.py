"""Check that each listed job's "Open posting" link still opens that same job.

For every job in New match (and the other listed tabs) it opens the link the way the company's job system
does and reports one of:
  open      the link opens this job
  closed    the company took the posting down (the page says not found / no longer available)
  mismatch  the link opens, but a different job (the title on the page doesn't match)
  unknown   the page couldn't be read (blocked, timed out); left as it is

Run by hand on the server to see a report:   python3 linkcheck.py
The agent also runs it after each full check and moves closed postings to "Posting closed" and links that open
a different job to "Link opens a different job", so New match only has links that work.
"""
from __future__ import annotations

import html
import re
import time
from urllib.parse import urlparse

CLOSED_WORDS = re.compile(
    r"no longer (available|accepting|open|active|posted)|position has been filled|job (posting )?(has )?(expired|closed)"
    r"|this job is closed|job not found|posting (is )?closed|page (you are looking for|not found)|isn.t available anymore"
    r"|requisition (is )?(no longer|closed)|not accepting applications", re.I)
LISTED = ("notified", "review_queue", "capped")
STOP = {"the", "and", "for", "of", "a", "an", "to", "in", "at", "with", "remote", "hybrid", "us", "usa", "sr", "senior",
        "jr", "i", "ii", "iii", "iv", "title", "job", "role"}


def _words(t):
    return {w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if w not in STOP and len(w) > 1}


def title_matches(expected: str, found: str) -> bool:
    """The page's title is this job: most of the job's title words appear in it."""
    we, wf = _words(expected), _words(found)
    if not we or not wf:
        return False
    return len(we & wf) / len(we) >= 0.7


def _page_title(text: str) -> str:
    for pat in (r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', r"<title[^>]*>([^<]*)</title>",
                r"<h1[^>]*>(.*?)</h1>"):
        m = re.search(pat, text, re.I | re.S)
        if m and m.group(1).strip():
            return html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).strip()
    return ""


def _json(client, url, method="GET"):
    r = client.request(method, url, headers={"Accept": "application/json"})
    if r.status_code in (404, 410):
        return None, "closed"
    if r.status_code != 200:
        return None, f"http {r.status_code}"
    try:
        return r.json(), ""
    except ValueError:
        return None, "not json"


def check(url: str, title: str, client) -> tuple[str, str]:
    """(state, what the link shows). state: open | closed | mismatch | unknown."""
    try:
        return _check(url, title, client)
    except Exception as e:  # noqa: BLE001  (network trouble: say so, never guess)
        return "unknown", type(e).__name__


def _verdict(title, found):
    if not found:
        return "unknown", "no title on the page"
    return ("open" if title_matches(title, found) else "mismatch"), found[:120]


def _check(url, title, client):
    u = urlparse(url)
    h, parts = (u.hostname or "").lower(), [p for p in u.path.split("/") if p]

    if h == "jobs.ashbyhq.com" and len(parts) >= 2:
        d, err = _json(client, f"https://api.ashbyhq.com/posting-api/job-board/{parts[0]}")
        if d is None:
            return ("closed", "board gone") if err == "closed" else ("unknown", err)
        job = next((j for j in d.get("jobs", []) if j.get("id") == parts[1]), None)
        return ("closed", "not on the company's board any more") if job is None else _verdict(title, job.get("title"))

    if h.endswith("greenhouse.io") and "jobs" in parts:
        slug, jid = parts[0], parts[parts.index("jobs") + 1] if len(parts) > parts.index("jobs") + 1 else ""
        d, err = _json(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}")
        return ("closed", "not on the company's board any more") if err == "closed" else (
            ("unknown", err) if d is None else _verdict(title, d.get("title")))

    if h == "jobs.lever.co" and len(parts) >= 2:
        d, err = _json(client, f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}")
        return ("closed", "not on the company's board any more") if err == "closed" else (
            ("unknown", err) if d is None else _verdict(title, d.get("text")))

    if h.endswith("myworkdayjobs.com") and "job" in parts:
        i = parts.index("job")
        site = parts[i - 1]
        rest = "/".join(parts[i + 1:])
        d, err = _json(client, f"https://{h}/wday/cxs/{h.split('.')[0]}/{site}/job/{rest}")
        if err == "closed":
            return "closed", "not on the company's board any more"
        if d is None:
            return "unknown", err
        info = d.get("jobPostingInfo") or {}
        if not info:
            return "closed", "Workday says the job is not available"
        return _verdict(title, info.get("title"))

    if h == "jobs.smartrecruiters.com" and len(parts) >= 2:
        jid = parts[1].split("-")[0]
        d, err = _json(client, f"https://api.smartrecruiters.com/v1/companies/{parts[0]}/postings/{jid}")
        if err == "closed":
            return "closed", "not on the company's board any more"
        if d is None:
            return "unknown", err
        if d.get("active") is False:
            return "closed", "marked inactive"
        return _verdict(title, d.get("name"))

    # any other site: open the page itself
    r = client.get(url)
    if r.status_code in (404, 410):
        return "closed", f"page returns {r.status_code}"
    if r.status_code != 200:
        return "unknown", f"http {r.status_code}"
    text = r.text[:400_000]
    if CLOSED_WORDS.search(re.sub(r"<[^>]+>", " ", text)):
        return "closed", "page says the job is no longer available"
    found = _page_title(text)
    if found and title_matches(title, found):
        return "open", found[:120]
    if _words(title) and len(_words(title) & _words(re.sub(r"<[^>]+>", " ", text))) / len(_words(title)) >= 0.8:
        return "open", (found or "title found on the page")[:120]
    return ("mismatch", found[:120]) if found else ("unknown", "page needs a browser to show the job")


def check_listed(tracker, cfg: dict, apply: bool = True, report=None) -> dict:
    """Check every listed job's link; with apply=True move closed / wrong ones out of New match."""
    import httpx

    from fetchers import UA

    every = int((cfg.get("link_check") or {}).get("every_minutes", 30)) * 60
    if apply and every and time.time() - _LAST[0] < every:
        return {}
    _LAST[0] = time.time()
    rows = tracker.db.execute(
        f"SELECT id, company, title, url FROM jobs WHERE status IN ({','.join('?' * len(LISTED))})", LISTED).fetchall()
    counts = {"open": 0, "closed": 0, "mismatch": 0, "unknown": 0}
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/124.0 Safari/537.36", **{k: v for k, v in UA.items() if k != "User-Agent"}}
    with httpx.Client(headers=headers, timeout=httpx.Timeout(15, connect=8), follow_redirects=True) as client:
        from links import kind
        for r in rows:
            if kind(r["url"], r["company"]) == "jobsite":
                continue                                # flagged 'Not confirmed on company site': nothing to check
            state, shows = check(r["url"], r["title"], client)
            counts[state] += 1
            if report is not None:
                report.append((state, r["company"], r["title"], shows, r["url"]))
            if apply and state in ("closed", "mismatch"):
                tracker.set_status(r["id"], "posting_closed" if state == "closed" else "link_wrong")
            time.sleep(0.3)
    if apply and (counts["closed"] or counts["mismatch"]):
        print(f"[links] {counts['closed']} postings closed, {counts['mismatch']} links opened a different job: "
              "moved out of New match", flush=True)
    return counts


_LAST = [0.0]


if __name__ == "__main__":
    import sys

    import yaml

    from db import Tracker

    cfg = yaml.safe_load(open("config.yaml"))
    rep = []
    counts = check_listed(Tracker("jobs.db", cfg["max_per_company"]), cfg, apply="--fix" in sys.argv, report=rep)
    for state, company, title, shows, url in sorted(rep):
        print(f"{state.upper():9} {company} | {title}\n          link shows: {shows}\n          {url}")
    print(counts, "" if "--fix" in sys.argv else "(report only; add --fix to move closed/wrong ones out of New match)")
