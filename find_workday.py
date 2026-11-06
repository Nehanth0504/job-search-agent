"""Find companies' Workday career sites (the system most large employers use) and add them to the watch list.

How: a Workday site lives at https://{tenant}.wd{N}.myworkdayjobs.com/{site}. For each company name the agent
tries likely tenant names on the Workday data centers. A real tenant answers /robots.txt with its career site
names ("Sitemap: https://acme.wd5.myworkdayjobs.com/AcmeCareers/siteMap.xml"); a wrong guess answers an error.
The site must then answer Workday's job search with at least one open job before it is added.

    python find_workday.py            # works through company_candidates.txt, biggest employers first
Safe to stop and rerun (data/workday_cache.json). companies.yaml is backed up before it is changed.
"""
import json
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import yaml

from verify_companies import load_names, slug_variants

DATA_CENTERS = ["wd5", "wd1", "wd3", "wd12", "wd10", "wd103", "wd101", "wd2", "wd4", "wd501", "wd105", "wd108", "wd503"]
FIRST_PASS_CENTERS = 6          # the 6 most common data centers hold most tenants; --all-centers tries all 13
CACHE = "data/workday_cache.json"
SITE_RX = re.compile(r"Sitemap:\s*https?://[^/\s]+/([A-Za-z0-9_\-]+)/siteMap\.xml", re.I)
ALLOW_RX = re.compile(r"^Allow:\s*/([A-Za-z0-9_\-]+)/\s*$", re.I | re.M)
SKIP_SITES = re.compile(r"intern|universit|campus|student|talentcommunity|recruitads|internal|employee|alumni|"
                        r"research|refreshfacet", re.I)


def tenants(name: str):
    """Likely Workday tenant ids: 'Goldman Sachs' -> goldmansachs; 'Capital One Services' -> capitalone, ..."""
    out = []
    for s in slug_variants(name)[:6]:
        t = re.sub(r"[^a-z0-9]", "", s.lower())
        if len(t) >= 3 and not t.startswith("join") and not t.endswith(("careers", "jobs", "hq")) and t not in out:
            out.append(t)
    return out[:2]


def sites_from_robots(text: str):
    """Career site names listed in a Workday robots.txt, the public external site first."""
    found = SITE_RX.findall(text or "") + ALLOW_RX.findall(text or "")
    seen = []
    for s in found:
        if s not in seen and not SKIP_SITES.search(s):
            seen.append(s)
    return seen


class Pace:
    """At most `per_second` requests per second to Workday overall."""

    def __init__(self, per_second=8):
        self.gap, self.next, self.lock = 1.0 / per_second, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            start = max(now, self.next)
            self.next = start + self.gap
        if start > now:
            time.sleep(start - now)


def jobs_total(client, host, tenant, site, pace=None):
    if pace:
        pace.wait()
    try:
        r = client.post(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                        json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
        return int((r.json() or {}).get("total") or 0) if r.status_code == 200 else 0
    except Exception:  # noqa: BLE001
        return 0


def find(name: str, client, pace=None, centers=None):
    """{name, ats: workday, host, tenant, site} or None."""
    for tenant in tenants(name):
        for dc in (centers or DATA_CENTERS):
            host = f"{tenant}.{dc}.myworkdayjobs.com"
            if pace:
                pace.wait()
            try:
                r = client.get(f"https://{host}/robots.txt")
            except Exception:  # noqa: BLE001
                continue
            if r.status_code != 200 or "Sitemap" not in r.text and "Allow" not in r.text:
                continue
            for site in sites_from_robots(r.text)[:3]:
                if jobs_total(client, host, tenant, site, pace) > 0:
                    return dict(name=name, ats="workday", host=host, tenant=tenant, site=site)
            break           # tenant exists on this data center but has no open public site: stop for this tenant
    return None


def main():
    import httpx
    dry = "--dry-run" in sys.argv
    centers = DATA_CENTERS if "--all-centers" in sys.argv else DATA_CENTERS[:FIRST_PASS_CENTERS]
    names = load_names()
    existing = yaml.safe_load(open("companies.yaml"))["companies"]
    watched = {c["name"].lower() for c in existing}
    have_wd = {(c.get("host"), c.get("site")) for c in existing if c.get("ats") == "workday"}
    os.makedirs("data", exist_ok=True)
    try:
        cache = json.load(open(CACHE))
    except (OSError, ValueError):
        cache = {}
    todo = [n for n in names if n not in cache and n.lower() not in watched]
    print(f"Looking for Workday career sites of {len(todo)} companies ({len(cache)} done earlier). "
          "Biggest employers first; progress prints below.", flush=True)
    pace, lock, done = Pace(8), threading.Lock(), [0]

    def work(n, client):
        try:
            r = find(n, client, pace, centers)
        except Exception:  # noqa: BLE001
            r = None
        with lock:
            cache[n] = r
            done[0] += 1
            if done[0] % 50 == 0:
                print(f"  {done[0]}/{len(todo)} checked, {sum(1 for v in cache.values() if v)} Workday sites found",
                      flush=True)
                json.dump(cache, open(CACHE, "w"))

    with httpx.Client(headers={"User-Agent": "Mozilla/5.0 (job-agent personal job search)"}, timeout=10,
                      follow_redirects=False, limits=httpx.Limits(max_connections=20)) as client:
        try:
            with ThreadPoolExecutor(8) as ex:
                list(ex.map(lambda n: work(n, client), todo))
        except KeyboardInterrupt:
            print("Stopped. Progress saved; run again to continue.")
    json.dump(cache, open(CACHE, "w"))
    added = []
    for v in cache.values():
        if v and (v["host"], v["site"]) not in have_wd and v["name"].lower() not in watched:
            have_wd.add((v["host"], v["site"]))
            watched.add(v["name"].lower())
            added.append(v)
    print(f"Workday sites found: {sum(1 for v in cache.values() if v)}. New companies added: {len(added)}. "
          f"Total watched: {len(existing) + len(added)}.", flush=True)
    if not dry and added:
        from discover_boards import write_companies
        current = yaml.safe_load(open("companies.yaml"))["companies"]     # re-read: other steps may have added some
        keys = {(c.get("ats"), c.get("host"), c.get("site")) for c in current}
        shutil.copyfile("companies.yaml", "companies.yaml.bak")
        write_companies("companies.yaml", current + [a for a in added if ("workday", a["host"], a["site"]) not in keys])
        print("Saved companies.yaml (previous copy: companies.yaml.bak)", flush=True)


if __name__ == "__main__":
    main()
