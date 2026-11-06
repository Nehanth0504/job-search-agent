"""Find each company's public job board and keep the ones that exist.

    python verify_companies.py            # reads company_candidates.txt, updates companies.yaml
    python verify_companies.py --dry-run  # only report

For every name it tries a few spellings of the board id ("Scale AI" -> scaleai, scale-ai) on Greenhouse,
Lever and Ashby. A board counts only if the company's job-board API answers with a real job list. Existing
companies.yaml entries are kept. A backup of the old file is saved as companies.yaml.bak.
"""
import json
import os
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

import yaml

URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json&limit=1",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{}/postings?limit=1",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{}",
    "recruitee": "https://{}.recruitee.com/api/offers/",
}
FIRST_PASS = ("greenhouse", "ashby", "lever")
SECOND_PASS = ("smartrecruiters", "workable", "recruitee")
THIRD_PASS = ("bamboohr", "breezy", "pinpoint", "rippling")
from fetchers_more import LIST_URLS as _MORE_URLS, has_jobs as _more_has_jobs  # noqa: E402
URLS.update(_MORE_URLS)


SUFFIXES = {"inc", "corp", "corporation", "co", "company", "llc", "ltd", "technologies", "technology", "labs",
            "systems", "software", "group", "holdings", "health", "healthcare", "ai", "io", "com"}


def slug_variants(name: str):
    """Likely job-board ids for a company name ("Scale AI" -> scaleai, scale-ai, scale ...)."""
    base = re.sub(r"[^a-z0-9& ]", " ", name.lower()).replace("&", " and ")
    words = base.split()
    core = [w for w in words if w not in SUFFIXES] or words
    out = []
    for ws in (words, core):
        if ws:
            out += ["".join(ws), "-".join(ws), "_".join(ws)]
    if len(words) > 1:
        out.append(words[0])
    joined = "".join(core)
    out += [joined + "hq", joined + "careers", joined + "inc", joined + "ai", "join" + joined, joined + "jobs"]
    seen = []
    for s in out:
        if s and s not in seen:
            seen.append(s)
    return seen


def is_board(ats: str, data) -> bool:
    if ats == "greenhouse":
        return isinstance(data, dict) and isinstance(data.get("jobs"), list)
    if ats == "lever":
        return isinstance(data, list)
    if ats == "ashby":
        return isinstance(data, dict) and isinstance(data.get("jobs"), list)
    if ats == "smartrecruiters":
        return isinstance(data, dict) and isinstance(data.get("content"), list) and (data.get("totalFound") or 0) > 0
    if ats == "workable":
        return isinstance(data, dict) and isinstance(data.get("jobs"), list) and len(data["jobs"]) > 0
    if ats == "recruitee":
        return isinstance(data, dict) and isinstance(data.get("offers"), list)
    if ats in _MORE_URLS:
        return _more_has_jobs(ats, data)
    return False


def probe_http(client, ats: str, slug: str):
    try:
        r = client.get(URLS[ats].format(slug))
        return is_board(ats, r.json()) if r.status_code == 200 else False
    except Exception:  # noqa: BLE001
        return False


def find_board(name: str, probe, providers=FIRST_PASS):
    for slug in slug_variants(name)[:8]:
        for ats in providers:
            if probe(ats, slug):
                return dict(name=name, ats=ats, slug=slug)
    return None


def load_names(path="company_candidates.txt"):
    names = []
    for line in open(path):
        line = line.split("#")[0].strip()
        if line and line not in names:
            names.append(line)
    return names


def merge(existing, found):
    have = {(c.get("ats"), c.get("slug")) for c in existing}
    names = {c["name"].lower() for c in existing}
    added = [c for c in found if (c["ats"], c["slug"]) not in have and c["name"].lower() not in names]
    return existing + added, added


def main():
    dry = "--dry-run" in sys.argv
    import httpx
    names = load_names()
    existing = yaml.safe_load(open("companies.yaml"))["companies"]
    import threading
    cache_path = "data/verify_cache.json"
    os.makedirs("data", exist_ok=True)
    try:
        cache = json.load(open(cache_path))
    except (OSError, ValueError):
        cache = {}
    todo = [n for n in names if n not in cache]
    print(f"Checking {len(todo)} companies ({len(names) - len(todo)} already checked on an earlier run). "
          "This can take 10-20 minutes; progress prints below. Safe to stop and rerun: it continues.", flush=True)
    done, lock = [0], threading.Lock()

    def work(n, probe):
        r = find_board(n, probe)
        with lock:
            cache[n] = r
            done[0] += 1
            if done[0] % 25 == 0:
                hits = sum(1 for x in cache.values() if x)
                print(f"  {done[0]}/{len(todo)} checked, {hits} boards found so far", flush=True)
                json.dump(cache, open(cache_path, "w"))
        return r

    with httpx.Client(headers={"User-Agent": "job-agent/0.1 (personal job search)"}, timeout=10,
                      follow_redirects=True, limits=httpx.Limits(max_connections=40)) as client:
        probe = lambda ats, slug: probe_http(client, ats, slug)  # noqa: E731
        try:
            with ThreadPoolExecutor(12) as ex:
                list(ex.map(lambda n: work(n, probe), todo))
        except KeyboardInterrupt:
            print("Stopped. Progress saved; run again to continue.")
    json.dump(cache, open(cache_path, "w"))

    def extra_pass(label, providers, path, prev):
        try:
            cache_x = json.load(open(path))
        except (OSError, ValueError):
            cache_x = {}
        todo_x = [n for n in names if not any(c.get(n) for c in prev) and n not in cache_x]
        if todo_x:
            print(f"{label}: {len(todo_x)} companies...", flush=True)
            done_x = [0]

            def work_x(n, probe):
                r = find_board(n, probe, providers)
                with lock:
                    cache_x[n] = r
                    done_x[0] += 1
                    if done_x[0] % 50 == 0:
                        print(f"  {done_x[0]}/{len(todo_x)} checked, {sum(1 for x in cache_x.values() if x)} boards found so far",
                              flush=True)
                        json.dump(cache_x, open(path, "w"))
                return r

            with httpx.Client(headers={"User-Agent": "job-agent/0.1 (personal job search)"}, timeout=10,
                              follow_redirects=True, limits=httpx.Limits(max_connections=40)) as client:
                probe = lambda ats, slug: probe_http(client, ats, slug)  # noqa: E731
                try:
                    with ThreadPoolExecutor(12) as ex:
                        list(ex.map(lambda n: work_x(n, probe), todo_x))
                except KeyboardInterrupt:
                    print("Stopped. Progress saved; run again to continue.")
            json.dump(cache_x, open(path, "w"))
        return cache_x

    cache2 = extra_pass("Second pass (SmartRecruiters, Workable, Recruitee)", SECOND_PASS, "data/verify_cache2.json", [cache])
    cache3 = extra_pass("Third pass (BambooHR, Breezy, Pinpoint, Rippling)", THIRD_PASS, "data/verify_cache3.json",
                        [cache, cache2])
    results = [cache.get(n) or cache2.get(n) or cache3.get(n) for n in names]
    found = [r for r in results if r]
    missing = [n for n, r in zip(names, results) if not r]
    merged, added = merge(existing, found)
    print(f"Found a job board for {len(found)} of {len(names)}. New companies added: {len(added)}. "
          f"Total watched: {len(merged)}.")
    if missing:
        open("data/no_public_board.txt", "w").write("\n".join(missing))
        print(f"No board on a public job-board service for {len(missing)} names (listed in data/no_public_board.txt). "
              "Those companies are still covered by the hourly listing search.")
    if not dry:
        shutil.copyfile("companies.yaml", "companies.yaml.bak")
        header = ("# Companies the agent watches. Built by verify_companies.py; add names to company_candidates.txt\n"
                  "# and run it again. ats: any reader in fetchers.py / fetchers_more.py.\n")
        with open("companies.yaml", "w") as f:
            f.write(header + "companies:\n")
            for c in merged:
                f.write("  - " + json.dumps(c, ensure_ascii=False) + "\n")
        print("Saved companies.yaml (previous copy: companies.yaml.bak)")


if __name__ == "__main__":
    main()
