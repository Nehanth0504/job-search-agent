"""Discover thousands of company job boards, keep the ones hiring for YOUR roles.

    python discover_boards.py                   # scan up to 4000 new boards, update companies.yaml
    python discover_boards.py --max-boards 800  # a quicker first pass
    python discover_boards.py --dry-run         # report only
    python discover_boards.py --urls links.txt  # also use job-board links you pasted into a file

How it works
  1. Common Crawl (a free public web index) lists job-board URLs on Greenhouse, Lever and Ashby. Each URL
     contains the company's board id, so this finds companies nobody typed in.
  2. Every board is asked (through that company's public job API) for its job titles.
  3. A board is kept only if it has at least one opening matching your roles (config.yaml) in the US or
     with no clear location. Results are cached in data/boards_cache.json, so a rerun continues where the
     last one stopped and never re-checks a board.
It can be run again any time (weekly is plenty) to pick up new companies.
"""
import json
import os
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import yaml

HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
}
RESERVED = {"embed", "jobs", "api", "v1", "boards", "assets", "static", "favicon.ico", "robots.txt",
            "sitemap.xml", "careers", "login", "signup", "undefined", "null", "_next", "cdn-cgi"}
LISTING = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
}
CACHE = "data/boards_cache.json"


def slug_from_url(url: str):
    """(ats, slug) from a job-board URL, or None."""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    ats = HOSTS.get((u.hostname or "").lower())
    if not ats:
        return None
    if ats == "greenhouse" and u.path.startswith("/embed"):
        slug = (parse_qs(u.query).get("for") or [""])[0]
    else:
        parts = [p for p in u.path.split("/") if p]
        slug = parts[0] if parts else ""
    slug = slug.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,60}", slug) or slug in RESERVED:
        return None
    return ats, slug


def _get(client, url, params, log, tries=5):
    """GET with patient retries: Common Crawl's index is often overloaded (429/502/503/504)."""
    import time
    r = None
    for i in range(tries):
        try:
            r = client.get(url, params=params, timeout=180)
        except Exception as e:  # noqa: BLE001
            log(f"[crawl] request error: {type(e).__name__}: {e}")
            time.sleep(20 * (i + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(30 * (i + 1))                    # 30s, 60s, 90s ... the index needs time to recover
            continue
        return r
    return r


# What to ask the index for: (url pattern, matchType). Subdomain-based systems use matchType=domain.
CRAWL_TARGETS = [
    ("boards.greenhouse.io/*", None), ("job-boards.greenhouse.io/*", None), ("jobs.lever.co/*", None),
    ("jobs.ashbyhq.com/*", None), ("apply.workable.com/*", None), ("jobs.smartrecruiters.com/*", None),
    ("myworkdayjobs.com", "domain"), ("bamboohr.com", "domain"), ("breezy.hr", "domain"),
    ("recruitee.com", "domain"), ("pinpointhq.com", "domain"), ("icims.com", "domain"),
]


def _parse(url):
    """Any job-board URL -> (ats, fields-json) using the same detector as the website search."""
    from find_careers import boards_in
    out = []
    for ats, fields in boards_in("", url):
        out.append((ats, json.dumps(fields, sort_keys=True)))
    return out


def _collect(text, found):
    n = 0
    for line in text.splitlines():
        try:
            url = json.loads(line)["url"]
        except (ValueError, KeyError, TypeError):
            continue
        for hit in _parse(url):
            if hit not in found:
                found.add(hit)
                n += 1
    return n


def crawl_slugs(client, pages=60, crawls=6, log=print):
    """Collect job boards from Common Crawl's index. Says plainly what the index answered."""
    found = set()
    info = client.get("https://index.commoncrawl.org/collinfo.json").json()
    log(f"[crawl] index answered; using {', '.join(c['id'] for c in info[:crawls])}")
    failed_crawls = 0
    for crawl in info[:crawls]:
        api = crawl["cdx-api"]
        failures = 0
        for pattern, match in CRAWL_TARGETS:
            if failures >= 3:
                log(f"[crawl] {crawl['id']}: index not answering, skipping the rest of this crawl")
                break
            base = {"url": pattern, "output": "json", "fl": "url", "filter": "status:200"}
            if match:
                base["matchType"] = match
            before, got_any = len(found), False
            for page in range(pages):
                r = _get(client, api, dict(base, page=page), log)
                if r is None or r.status_code != 200:
                    if page == 0:
                        failures += 1
                        log(f"[crawl] {pattern}: answered {getattr(r, 'status_code', 'no response')}: "
                            f"{(r.text[:160] if r is not None else '').strip()!r}")
                    break
                got_any = True
                if _collect(r.text, found) == 0 and page > 3:
                    break                                   # pages are sorted; nothing new for a while = done
            log(f"[crawl] {crawl['id']} {pattern}: +{len(found) - before} boards (total {len(found)})")
        if failures >= 3:
            failed_crawls += 1
            if failed_crawls >= 2:
                log("[crawl] Common Crawl's index is overloaded right now; the next search round will try again")
                break
    return found


def slugs_from_file(path):
    """Board links pasted into a text file (any job-board URLs, one per line)."""
    out = set()
    for line in open(path):
        hit = slug_from_url(line.strip())
        if hit:
            out.add(hit)
    return out


def titles_and_places(ats, data):
    """[(title, location)] from a board's job list."""
    out = []
    if ats == "greenhouse":
        for j in data.get("jobs", []):
            out.append((j.get("title", ""), (j.get("location") or {}).get("name", "")))
    elif ats == "lever":
        for j in data if isinstance(data, list) else []:
            cats = j.get("categories") or {}
            out.append((j.get("text", ""), " | ".join(filter(None, [cats.get("location")] + (cats.get("allLocations") or [])))))
    else:
        for j in data.get("jobs", []):
            out.append((j.get("title", ""), " | ".join(filter(None, [j.get("location")] +
                        [(s or {}).get("location") for s in j.get("secondaryLocations") or []]))))
    return out


def scan_board(client, ats, slug, cfg):
    """Ask one board for its titles. Returns {"jobs": n, "hits": k} or None if the board doesn't exist."""
    from locations import classify_location
    from roles import title_ok
    try:
        r = client.get(LISTING[ats].format(slug))
        if r.status_code != 200:
            return None
        rows = titles_and_places(ats, r.json())
    except Exception:  # noqa: BLE001
        return None
    hits = sum(1 for t, loc in rows if title_ok(t, cfg) and classify_location(loc) != "non_us")
    return {"jobs": len(rows), "hits": hits}


def pretty_name(slug: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[-_.]", slug) if w)


def merge(existing, boards):
    """boards: {(ats, slug): {"hits": k}}. Adds boards with hits that are not already watched."""
    have = {(c.get("ats"), c.get("slug")) for c in existing}
    added = [dict(name=pretty_name(s), ats=a, slug=s) for (a, s), v in sorted(boards.items())
             if v and v["hits"] > 0 and (a, s) not in have]
    return existing + added, added


def write_companies(path, companies):
    with open(path, "w") as f:
        f.write("# Companies the agent watches. Maintained by discover_boards.py and verify_companies.py.\n"
                "# ats: greenhouse | lever | ashby\ncompanies:\n")
        for c in companies:
            f.write("  - " + json.dumps(c, ensure_ascii=False) + "\n")


def main():
    """Find job boards in Common Crawl, keep every one that has open jobs, add them to companies.yaml."""
    args = sys.argv[1:]
    dry = "--dry-run" in args
    max_boards = int(args[args.index("--max-boards") + 1]) if "--max-boards" in args else 60000
    import httpx
    from find_careers import validate
    cfg = yaml.safe_load(open("config.yaml"))
    existing = yaml.safe_load(open("companies.yaml"))["companies"]
    os.makedirs("data", exist_ok=True)
    cache_path = "data/crawl_boards.json"
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    with httpx.Client(headers={"User-Agent": "job-agent/0.1 (personal job search)"}, timeout=15,
                      follow_redirects=True, limits=httpx.Limits(max_connections=40)) as client:
        print("Step 1/2: finding job boards in the Common Crawl index...", flush=True)
        boards = set()
        if "--urls" in args:
            for line in open(args[args.index("--urls") + 1]):
                boards |= set(_parse(line.strip()))
        try:
            boards |= crawl_slugs(client)
        except Exception as e:  # noqa: BLE001
            print(f"[crawl] could not reach Common Crawl: {type(e).__name__}: {e}", flush=True)
        todo = sorted(f"{a}|{f}" for a, f in boards if f"{a}|{f}" not in cache)[:max_boards]
        print(f"Found {len(boards)} boards; {len(todo)} not checked yet. Step 2/2: checking each has open jobs...",
              flush=True)
        import threading
        lock, done = threading.Lock(), [0]

        def work(key):
            ats, fields = key.split("|", 1)
            try:
                ok = validate(client, ats, json.loads(fields), cfg)
            except Exception:  # noqa: BLE001
                ok = False
            with lock:
                cache[key] = bool(ok)
                done[0] += 1
                if done[0] % 250 == 0:
                    print(f"  checked {done[0]}/{len(todo)}, {sum(cache.values())} with open jobs", flush=True)
                    json.dump(cache, open(cache_path, "w"))

        with ThreadPoolExecutor(12) as ex:
            list(ex.map(work, todo))
        json.dump(cache, open(cache_path, "w"))
    key = lambda c: (c.get("ats"), json.dumps({k: v for k, v in c.items() if k not in ("name", "ats")},  # noqa: E731
                                              sort_keys=True))
    have = {key(c) for c in existing}
    names = {c["name"].lower() for c in existing}
    added = []
    for k, ok in cache.items():
        if not ok:
            continue
        ats, fields = k.split("|", 1)
        f = json.loads(fields)
        entry = dict(name=pretty_name(f.get("slug") or f.get("tenant") or (f.get("host") or "").split(".")[0]),
                     ats=ats, **f)
        if key(entry) in have:
            continue
        if entry["name"].lower() in names:
            entry["name"] = f"{entry['name']} ({ats})"
        have.add(key(entry))
        names.add(entry["name"].lower())
        added.append(entry)
    print(f"Boards with open jobs: {sum(cache.values())}. New companies added: {len(added)}. "
          f"Total watched: {len(existing) + len(added)}.", flush=True)
    if not dry and added:
        shutil.copyfile("companies.yaml", "companies.yaml.bak")
        write_companies("companies.yaml", existing + added)
        print("Saved companies.yaml (previous copy: companies.yaml.bak)", flush=True)


if __name__ == "__main__":
    main()
