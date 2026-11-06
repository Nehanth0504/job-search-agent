"""Find career sites the way a person would: open the company's own website, follow its careers page, and
read which job system it uses (Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee).

    python find_careers.py            # works through data/no_public_board.txt (written by verify_companies.py)
    python find_careers.py --dry-run  # report only

A board is added only after its public job list answers and has an opening for your roles (Workday, Greenhouse,
Lever, Ashby) or is a real job list (SmartRecruiters, Workable, Recruitee). Results are cached in
data/careers_cache.json, so reruns skip finished companies. companies.yaml is backed up first.
"""
import json
import os
import re
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import yaml

from verify_companies import is_board, slug_variants, URLS

LINK = re.compile(r"https?://[a-z0-9.-]+\.(?:myworkdayjobs\.com|greenhouse\.io|lever\.co|ashbyhq\.com|"
                  r"smartrecruiters\.com|workable\.com|recruitee\.com|bamboohr\.com|breezy\.hr|pinpointhq\.com|"
                  r"rippling\.com|icims\.com|oraclecloud\.com)[^\s\"'<>)\\]*", re.I)
ORACLE_SITE = re.compile(r"/CandidateExperience/[a-z-]+/sites/([A-Za-z0-9_]+)", re.I)


def candidate_sites(name: str):
    """Likely company website addresses for a name, best first."""
    doms = []
    for s in slug_variants(name)[:4]:
        if "_" in s or s.startswith("join") or s.endswith(("hq", "careers", "jobs", "inc")):
            continue
        for tld in (".com", ".ai", ".io"):
            doms.append(s.replace("-", "") + tld)
            if "-" in s:
                doms.append(s + tld)
    out = []
    for d in doms:
        if d not in out:
            out.append(d)
    return out[:6]


def pages_for(domain: str):
    return [f"https://www.{domain}/careers", f"https://{domain}/careers", f"https://careers.{domain}",
            f"https://jobs.{domain}", f"https://www.{domain}/jobs", f"https://www.{domain}"]


def boards_in(text: str, final_url: str = ""):
    """[(ats, entry-fields)] for every job-board link in a page."""
    from discover_boards import slug_from_url
    from learn import workday_from_url
    found = []
    for url in LINK.findall(text or "") + ([final_url] if final_url else []):
        u = url.replace("\\/", "/")
        host = (urlparse(u).hostname or "").lower()
        if host.endswith("myworkdayjobs.com"):
            w = workday_from_url(u)
            if w:
                found.append(("workday", w))
        elif host.endswith("smartrecruiters.com"):
            parts = [p for p in urlparse(u).path.split("/") if p]
            if parts and host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"):
                found.append(("smartrecruiters", dict(slug=parts[0])))
        elif host.endswith("workable.com") and host.startswith("apply."):
            parts = [p for p in urlparse(u).path.split("/") if p]
            if parts and parts[0] not in ("api", "j"):
                found.append(("workable", dict(slug=parts[0].lower())))
        elif host.endswith(".recruitee.com") and host.count(".") == 2:
            found.append(("recruitee", dict(slug=host.split(".")[0])))
        elif host.endswith(".bamboohr.com") and host.count(".") == 2 and host.split(".")[0] not in ("www", "api"):
            found.append(("bamboohr", dict(slug=host.split(".")[0])))
        elif host.endswith(".breezy.hr") and host.count(".") == 2 and host.split(".")[0] not in ("www", "app", "api"):
            found.append(("breezy", dict(slug=host.split(".")[0])))
        elif host.endswith(".pinpointhq.com") and host.count(".") == 2 and host.split(".")[0] not in ("www", "app"):
            found.append(("pinpoint", dict(slug=host.split(".")[0])))
        elif host == "ats.rippling.com":
            parts = [p for p in urlparse(u).path.split("/") if p]
            if parts and parts[0] not in ("api", "static"):
                found.append(("rippling", dict(slug=parts[0])))
        elif host.endswith(".icims.com") and not host.startswith(("www.", "login.", "cdn")):
            found.append(("icims", dict(host=host)))
        elif host.endswith(".oraclecloud.com"):
            m = ORACLE_SITE.search(u)
            if m:
                found.append(("oracle", dict(host=host, site=m.group(1))))
        else:
            s = slug_from_url(u)
            if s:
                found.append((s[0], dict(slug=s[1])))
    uniq = []
    for f in found:
        if f not in uniq:
            uniq.append(f)
    return uniq


RT_WIDGET = re.compile(r'RTWidget\(\s*\{[^}]*?"companies"\s*:\s*\[\s*(\d+)', re.S)


def recruitee_widget(client, page: str, page_url: str):
    """A Recruitee job widget embedded in the company's own site -> {ats, slug, careers_page} or None."""
    m = RT_WIDGET.search(page or "")
    if not m:
        return None
    try:
        r = client.get(f"https://api.recruitee.com/c/{m.group(1)}/careers/offers")
        offers = (r.json() or {}).get("offers") or [] if r.status_code == 200 else []
    except Exception:  # noqa: BLE001
        return None
    for o in offers:
        h = (urlparse(o.get("careers_url") or "").hostname or "")
        if h.endswith(".recruitee.com"):
            return dict(ats="recruitee", slug=h.split(".")[0], careers_page=page_url.split("#")[0])
    return None


def validate(client, ats, fields, cfg):
    """True if the board's public job list answers with at least one job. Any open job counts (not only your
    current roles), so the company stays watched if you change target roles later."""
    from discover_boards import scan_board
    from learn import scan_workday
    if ats == "workday":
        r = scan_workday(client, fields, cfg)
        return bool(r and r["jobs"] > 0)
    if ats in ("greenhouse", "lever", "ashby"):
        r = scan_board(client, ats, fields["slug"], cfg)
        return bool(r and r["jobs"] > 0)
    from fetchers_more import LIST_URLS, READERS, has_jobs
    if ats in LIST_URLS:
        try:
            r = client.get(LIST_URLS[ats].format(fields["slug"]))
            return r.status_code == 200 and has_jobs(ats, r.json())
        except Exception:  # noqa: BLE001
            return False
    if ats in ("oracle", "icims"):
        try:
            return any(True for _ in READERS[ats](client, dict(name="probe", queries=["engineer"], **fields)))
        except Exception:  # noqa: BLE001
            return False
    try:
        r = client.get(URLS[ats].format(fields["slug"]))
        return r.status_code == 200 and is_board(ats, r.json())
    except Exception:  # noqa: BLE001
        return False


def find(name, client, cfg, domains=None):
    """The company's job board, or else its own careers page (read directly by the agent).
    `domains`: websites to try first (e.g. the employer website a listing gave us)."""
    from fetchers_more import looks_like_careers_page
    careers = None
    tried = list(domains or []) + [d for d in candidate_sites(name) if d not in (domains or [])]
    for domain in tried[:6]:
        alive = False
        for page in pages_for(domain):
            try:
                r = client.get(page)
            except Exception:  # noqa: BLE001
                continue
            if r.status_code >= 400:
                continue
            alive = True
            text = r.text[:300000]
            for ats, fields in boards_in(text, str(r.url)):
                if validate(client, ats, fields, cfg):
                    return dict(name=name, ats=ats, **fields)
            w = recruitee_widget(client, text, str(r.url))   # jobs shown on the company's own page by Recruitee
            if w:
                return dict(name=name, **w)
            if careers is None and ("/careers" in page or page.startswith(("https://careers.", "https://jobs."))) \
                    and looks_like_careers_page(text):
                careers = str(r.url)
        if alive:
            break           # the right website was found; do not try other spellings of the domain
    if careers:
        return dict(name=name, ats="careerpage", url=careers)
    return None


def main():
    import httpx
    dry = "--dry-run" in sys.argv
    cfg = yaml.safe_load(open("config.yaml"))
    existing = yaml.safe_load(open("companies.yaml"))["companies"]
    path = "data/no_public_board.txt"
    if not os.path.exists(path):
        sys.exit("Run python verify_companies.py first (it writes data/no_public_board.txt).")
    names = [l.strip() for l in open(path) if l.strip()]
    cache_path = "data/careers_cache2.json"   # v2: more job systems + own careers pages
    try:
        cache = json.load(open(cache_path))
    except (OSError, ValueError):
        cache = {}
    todo = [n for n in names if n not in cache]
    print(f"Opening the websites of {len(todo)} companies ({len(names) - len(todo)} done earlier). "
          "Progress prints below; safe to stop and rerun.", flush=True)
    done, lock = [0], threading.Lock()

    def work(n, client):
        try:
            r = find(n, client, cfg)
        except Exception:  # noqa: BLE001
            r = None
        with lock:
            cache[n] = r
            done[0] += 1
            if done[0] % 20 == 0:
                print(f"  {done[0]}/{len(todo)} done, {sum(1 for x in cache.values() if x)} career sites found",
                      flush=True)
                json.dump(cache, open(cache_path, "w"))

    with httpx.Client(headers={"User-Agent": "Mozilla/5.0 (job-agent personal job search)"}, timeout=8,
                      follow_redirects=True, limits=httpx.Limits(max_connections=40)) as client:
        try:
            with ThreadPoolExecutor(8) as ex:
                list(ex.map(lambda n: work(n, client), todo))
        except KeyboardInterrupt:
            print("Stopped. Progress saved; run again to continue.")
    json.dump(cache, open(cache_path, "w"))
    found = [v for v in cache.values() if v]
    key = lambda c: (c.get("ats"), c.get("slug") or c.get("url") or c.get("host", "") + "/" + c.get("site", ""))  # noqa: E731
    have = {key(c) for c in existing}
    added = []
    for f in found:
        k = key(f)
        if k not in have:
            have.add(k)
            added.append(f)
    print(f"Found career sites for {len(found)} companies; {len(added)} are new. "
          f"Total watched: {len(existing) + len(added)}.")
    by = {}
    for a in added:
        by[a["ats"]] = by.get(a["ats"], 0) + 1
    print("New by job system:", by)
    if not dry and added:
        shutil.copyfile("companies.yaml", "companies.yaml.bak")
        with open("companies.yaml", "w") as f:
            f.write("# Companies the agent watches. Maintained by verify_companies.py and find_careers.py.\ncompanies:\n")
            for c in existing + added:
                f.write("  - " + json.dumps(c, ensure_ascii=False) + "\n")
        print("Saved companies.yaml (previous copy: companies.yaml.bak)")


if __name__ == "__main__":
    main()
