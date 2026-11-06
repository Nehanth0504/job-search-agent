"""Fetch postings from public ATS job-board APIs (no scraping of LinkedIn/Indeed).

Each fetcher yields dicts: company, title, url, location, posted_at, description.
Board slugs live in companies.yaml - add as many companies as you like.
"""
from __future__ import annotations

import html
import re

UA ={"User-Agent": "job-agent/0.1 (personal job search)"}


def _text(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return " ".join(s.split())


def _locations(*parts) -> str:
    """Join every place a posting lists (primary + extra offices), without duplicates."""
    seen, out = set(), []
    for p in parts:
        for piece in ([p] if isinstance(p, str) else (p or [])):
            piece = (piece or "").strip() if isinstance(piece, str) else ""
            if piece and piece.lower() not in seen:
                seen.add(piece.lower())
                out.append(piece)
    return " | ".join(out)


def greenhouse(client: httpx.Client, slug: str, company: str, seen=None, wanted=None):
    """Light list first (titles only); the full text is downloaded only for postings that match your roles
    and have not been seen before. That keeps a check of hundreds of boards every minute cheap."""
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        offices = [(o or {}).get("location") or (o or {}).get("name") for o in (j.get("offices") or [])]
        job = dict(company=company, title=j["title"], url=j["absolute_url"],
                   location=_locations((j.get("location") or {}).get("name"), offices),
                   posted_at=j.get("first_published"), description="")  # not updated_at: it changes on every edit
        if (seen and seen(job["url"])) or (wanted and not wanted(job["title"])):
            yield job                                   # the pipeline drops it before the text is needed
            continue
        if j.get("id") is not None:
            d = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{j['id']}")
            if d.status_code == 200:
                job["description"] = _text(d.json().get("content", ""))
        yield job


def lever(client: httpx.Client, slug: str, company: str):
    r = client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    r.raise_for_status()
    for j in r.json():
        cats = j.get("categories") or {}
        country = "United States" if str(j.get("country") or "").upper() == "US" else ""
        yield dict(
            company=company, title=j["text"], url=j["hostedUrl"],
            location=_locations(cats.get("location"), cats.get("allLocations"), country),
            posted_at=j.get("createdAt"),
            description=_text(j.get("descriptionPlain") or j.get("description", "")),
        )


def ashby(client: httpx.Client, slug: str, company: str):
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"})
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        extra = [(s or {}).get("location") for s in (j.get("secondaryLocations") or [])]
        country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")) or ""
        yield dict(
            company=company, title=j["title"], url=j["jobUrl"],
            location=_locations(j.get("location"), extra, country), posted_at=j.get("publishedAt"),
            description=_text(j.get("descriptionHtml") or j.get("descriptionPlain", "")),
        )


def eightfold(client, c: dict, seen=None, wanted=None):
    """Career sites built on Eightfold (Microsoft's among them). EXPERIMENTAL: the layout is not documented,
    so every unexpected answer is reported instead of guessed at. Check with: python check_source.py Microsoft"""
    base = f"https://{c['host']}/api/pcsx"
    queries = c.get("queries") or ["software engineer AI machine learning", "forward deployed engineer"]
    limit = int(c.get("max_jobs", 60))
    max_details = int(c.get("max_details", 20))     # text downloads per check; the rest wait for the next one
    got, done, fetched = 0, set(), 0
    for q in queries:
        for start in range(0, 100, 10):
            r = client.get(f"{base}/search", params={"domain": c["domain"], "query": q, "start": start,
                                                      "location": c.get("location", "United States"),
                                                      "sort_by": "timestamp"})
            if r.status_code == 429:
                print(f"[fetch] {c['name']}: the site asked us to slow down (HTTP 429); trying again next time.")
                return
            if r.status_code != 200:
                raise RuntimeError(f"search answered HTTP {r.status_code}: {r.text[:120]!r}")
            import time as _t
            _t.sleep(1)                                # be gentle with the site
            data = (r.json() or {}).get("data") or {}
            positions = data.get("positions") or []
            if not positions:
                break
            for p in positions:
                pid = p.get("id")
                if pid in done or not p.get("name"):
                    continue
                done.add(pid)
                url = f"https://{c['host']}{p.get('positionUrl') or '/careers/job/' + str(pid)}"
                desc = ""
                skip = (seen and seen(url)) or (wanted and not wanted(p["name"])) or fetched >= max_details
                if not skip:                                       # text only for new postings that match your roles
                    fetched += 1
                    try:
                        d = client.get(f"{base}/position_details", params={"position_id": pid,
                                                                             "domain": c["domain"], "hl": "en"})
                        if d.status_code == 200:
                            desc = _text(((d.json() or {}).get("data") or {}).get("jobDescription", ""))
                    except Exception:  # noqa: BLE001 - one slow posting must not stop the rest
                        pass
                yield dict(company=c["name"], title=p["name"], url=url,
                           location=_locations(p.get("locations") or []), posted_at=p.get("postedTs"),
                           description=desc)
                got += 1
                if got >= limit:
                    return


def workday(client, c: dict, seen=None, wanted=None):
    """Career sites on Workday (most large non-tech employers). Uses the site's own public search endpoint.
    Entry: {name, ats: workday, host: "nvidia.wd5.myworkdayjobs.com", tenant: "nvidia", site: "NVIDIAExternalCareerSite"}.
    Check with: python check_source.py <name>"""
    host, tenant, site = c["host"], c["tenant"], c["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}"
    queries = c.get("queries") or ["machine learning", "AI engineer", "forward deployed engineer"]
    max_details, fetched, done = int(c.get("max_details", 20)), 0, set()
    for q in queries:
        for offset in (0, 20, 40):
            r = client.post(f"{api}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q})
            if r.status_code == 429:
                print(f"[fetch] {c['name']}: the site asked us to slow down (HTTP 429); trying again next time.")
                return
            if r.status_code != 200:
                raise RuntimeError(f"search answered HTTP {r.status_code}: {r.text[:120]!r}")
            posts = (r.json() or {}).get("jobPostings") or []
            for p in posts:
                path, title = p.get("externalPath"), p.get("title")
                if not (path and title) or path in done:
                    continue
                done.add(path)
                url = f"https://{host}/{site}{path}"
                job = dict(company=c["name"], title=title, url=url, location=_locations(p.get("locationsText")),
                           posted_at=p.get("postedOn"), description="")
                if not ((seen and seen(url)) or (wanted and not wanted(title)) or fetched >= max_details):
                    fetched += 1
                    try:
                        d = client.get(f"{api}{path}")
                        if d.status_code == 200:
                            info = (d.json() or {}).get("jobPostingInfo") or {}
                            job["description"] = _text(info.get("jobDescription", ""))
                            job["location"] = _locations(info.get("location"), info.get("additionalLocations"),
                                                         (info.get("country") or {}).get("descriptor"),
                                                         p.get("locationsText"))
                            job["url"] = info.get("externalUrl") or url
                    except Exception:  # noqa: BLE001
                        pass
                yield job
            if len(posts) < 20:
                break


def smartrecruiters(client, slug: str, company: str, seen=None, wanted=None):
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    for offset in (0, 100, 200):
        r = client.get(base, params={"limit": 100, "offset": offset, "country": "us"})
        r.raise_for_status()
        rows = (r.json() or {}).get("content") or []
        for j in rows:
            loc = j.get("location") or {}
            place = _locations(loc.get("fullLocation"), ", ".join(p for p in (loc.get("city"), loc.get("region"),
                               str(loc.get("country") or "").upper()) if p), "Remote" if loc.get("remote") else "")
            job = dict(company=company, title=j.get("name", ""), url=f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                       location=place, posted_at=j.get("releasedDate"), description="")
            if not ((seen and seen(job["url"])) or (wanted and not wanted(job["title"]))):
                d = client.get(f"{base}/{j.get('id')}")             # the text is needed only for new, matching titles
                if d.status_code == 200:
                    secs = ((d.json() or {}).get("jobAd") or {}).get("sections") or {}
                    job["description"] = _text(" ".join((s or {}).get("text", "") for s in secs.values()))
            yield job
        if len(rows) < 100:
            break


def workable(client, slug: str, company: str):
    r = client.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
    r.raise_for_status()
    for j in (r.json() or {}).get("jobs") or []:
        place = _locations(", ".join(p for p in (j.get("city"), j.get("state"), j.get("country")) if p),
                           "Remote" if j.get("telecommuting") else "")
        yield dict(company=company, title=j.get("title", ""), url=j.get("url") or j.get("shortlink") or "",
                   location=place, posted_at=j.get("published_on") or j.get("created_at"),
                   description=_text(j.get("description", "")))


def recruitee(client, slug: str, company: str, careers_page: str = None):
    """Recruitee boards. When the company shows its jobs on its own website through the Recruitee widget
    (`careers_page`), the link goes to the job on that page (…/#job-<id>) instead of recruitee.com."""
    r = client.get(f"https://{slug}.recruitee.com/api/offers/")
    r.raise_for_status()
    for j in (r.json() or {}).get("offers") or []:
        url = j.get("careers_url") or ""
        if careers_page and j.get("id"):
            url = careers_page.split("#")[0] + f"#job-{j['id']}"
        yield dict(company=company, title=j.get("title", ""), url=url,
                   location=_locations(j.get("location"), j.get("country"), "Remote" if j.get("remote") else ""),
                   posted_at=j.get("published_at"),
                   description=_text((j.get("description") or "") + " " + (j.get("requirements") or "")))


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters,
            "workable": workable, "recruitee": recruitee}


PROVIDERS = {
    "openwebninja": ("https://api.openwebninja.com/jsearch/search", lambda k: {"x-api-key": k}),
    "rapidapi": ("https://jsearch.p.rapidapi.com/search",
                 lambda k: {"X-RapidAPI-Key": k, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}),
}


def _jsearch_job(j: dict):
    """One JSearch result -> our job dict (None if it has no usable link or title).
    The link kept is the employer's own posting whenever the listing has one (see links.py)."""
    from links import best
    opts = j.get("apply_options") or []
    links = [j.get("job_apply_link")] + [o.get("apply_link") for o in opts]
    links = [l for i, l in enumerate(links) if l and l not in links[:i]]
    direct = [o.get("apply_link") for o in opts if o.get("is_direct")]
    if j.get("job_apply_is_direct"):
        direct.append(j.get("job_apply_link"))
    url = best(links, j.get("employer_name", ""), j.get("employer_website") or "", direct)
    if not (url and j.get("job_title") and j.get("employer_name")):
        return None
    place = ", ".join(p for p in (j.get("job_city"), j.get("job_state"), j.get("job_country")) if p)
    if j.get("job_is_remote"):
        place = ("Remote - " + place) if place else "Remote"
    return dict(company=j["employer_name"], title=j["job_title"], url=url, location=place,
                posted_at=j.get("job_posted_at_datetime_utc"), description=_text(j.get("job_description", "")),
                source="aggregator", links=links, employer_site=j.get("employer_website") or "")


def jsearch(client, src: dict, key: str, log=print):
    """Job listings from JSearch (Google for Jobs: LinkedIn, Indeed, Glassdoor, company sites).
    Stops quietly when the plan's request allowance is used up."""
    url, headers = PROVIDERS.get(src.get("provider", "openwebninja"), PROVIDERS["openwebninja"])
    for q in src.get("queries", []):
        for page in range(1, int(src.get("pages_per_query", 3)) + 1):
            r = client.get(url, headers=headers(key), params={
                "query": f"{q} in United States", "page": page, "num_pages": 1,
                "country": src.get("country", "us"), "date_posted": src.get("date_posted", "3days")})
            if r.status_code in (401, 403):
                log("[jsearch] the API key was refused. Check JSEARCH_API_KEY and your plan.")
                return
            if r.status_code == 429:
                log("[jsearch] request allowance used up for now; will resume when it resets.")
                return
            if r.status_code != 200:
                log(f"[jsearch] {q!r} page {page}: HTTP {r.status_code}")
                break
            rows = (r.json() or {}).get("data") or []
            for j in rows:
                job = _jsearch_job(j)
                if job:
                    yield job
            if len(rows) < 10:
                break


def fetch_aggregators(cfg: dict, state_path="data/source_state.json", now=None, log=print):
    """Run the listing aggregator at most every `every_minutes` (it is billed per request)."""
    import json
    import os
    import time
    src = (cfg.get("sources") or {}).get("jsearch") or {}
    if not src.get("enabled"):
        return
    key = os.environ.get("JSEARCH_API_KEY")
    if not key:
        log("[jsearch] enabled in config.yaml but JSEARCH_API_KEY is not set; skipping.")
        return
    now = now or time.time()
    try:
        state = json.load(open(state_path))
    except (OSError, ValueError):
        state = {}
    if now - state.get("jsearch", 0) < float(src.get("every_minutes", 180)) * 60:
        return
    import httpx
    with httpx.Client(timeout=40) as client:
        n = 0
        try:
            for job in jsearch(client, src, key, log):
                n += 1
                yield job
        except Exception as e:  # noqa: BLE001
            log(f"[jsearch] failed: {e}")
        log(f"[jsearch] {n} listings received")
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    state["jsearch"] = now
    json.dump(state, open(state_path, "w"))


_LAST_RUN: dict = {}


# --------------------------------------------------------------------------- pacing and back-off
import threading as _threading
import time as _time

# Systems that throttle by caller: at most N requests at a time and at least S seconds between requests.
LIMITS = {"workable": (2, 0.6), "recruitee": (2, 0.4), "workday": (6, 0.08), "smartrecruiters": (4, 0.2), "bamboohr": (3, 0.3),
          "breezy": (3, 0.3), "pinpoint": (3, 0.3), "rippling": (3, 0.3)}
COOLDOWN_SECONDS = 900          # a system that answers "429 Too Many Requests" is left alone for 15 minutes
STRIKE_LIMIT, STRIKE_PAUSE = 3, 86400   # a board failing 3 times in a row (403/404/not JSON) is checked once a day
_SEM, _NEXT, _LOCK = {}, {}, _threading.Lock()
COOLDOWN: dict = {}              # system -> time it may be asked again
STRIKES: dict = {}               # company key -> [failures in a row, paused until]


class _Paced:
    """Wraps the shared HTTP client so requests to one job system are spaced out."""

    def __init__(self, client, system):
        self.client, self.system = client, system
        n, self.gap = LIMITS[system]
        with _LOCK:
            self.sem = _SEM.setdefault(system, _threading.Semaphore(n))

    def _do(self, fn, *a, **k):
        with self.sem:
            with _LOCK:
                now = _time.time()
                start = max(now, _NEXT.get(self.system, 0))
                _NEXT[self.system] = start + self.gap
            if start > now:
                _time.sleep(start - now)
            r = fn(*a, **k)
        if getattr(r, "status_code", 0) == 429:
            COOLDOWN[self.system] = _time.time() + COOLDOWN_SECONDS
        return r

    def get(self, *a, **k):
        return self._do(self.client.get, *a, **k)

    def post(self, *a, **k):
        return self._do(self.client.post, *a, **k)


def _ckey(c):
    return (c.get("ats"), c.get("slug") or c.get("url") or f"{c.get('host', '')}/{c.get('site', '')}")


READ_OK: dict = {}


def _fetch_company(client, c, seen, wanted):
    """All postings of one company as a list; a failing board is reported and returns what it had.
    Only postings that match your roles and are new keep their full text: the rest are kept as title + link,
    which is all the pipeline needs to skip them (big boards would otherwise fill the server's memory)."""
    fn = FETCHERS.get(c["ats"])
    out = []
    now, key = _time.time(), _ckey(c)
    if COOLDOWN.get(c["ats"], 0) > now or (STRIKES.get(key) or [0, 0])[1] > now:
        return out                                   # this system asked us to slow down / this board is paused
    if c["ats"] in LIMITS:
        client = _Paced(client, c["ats"])
    try:
        from fetchers_more import READERS as _MORE
        if c["ats"] in _MORE:
            gen = _MORE[c["ats"]](client, c, seen, wanted)
        elif c["ats"] == "eightfold":
            gen = eightfold(client, c, seen, wanted)
        elif c["ats"] == "workday":
            gen = workday(client, c, seen, wanted)
        elif c["ats"] in ("greenhouse", "smartrecruiters"):
            gen = fn(client, c["slug"], c["name"], seen, wanted)
        else:
            gen = (recruitee(client, c["slug"], c["name"], c.get("careers_page")) if c["ats"] == "recruitee"
                   else fn(client, c["slug"], c["name"]))
        for job in gen:
            if (wanted and not wanted(job["title"])) or (seen and seen(job["url"])):
                job = dict(company=job["company"], title=job["title"], url=job["url"],
                           location=job.get("location", ""), posted_at=job.get("posted_at"), description="")
            out.append(job)
        STRIKES.pop(key, None)
        READ_OK[c["name"]] = now                     # this board was read in full (used to tell new postings apart)
    except Exception as e:  # noqa: BLE001 - log and move on
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status == 429:
            first = COOLDOWN.get(c["ats"], 0) <= _time.time() + COOLDOWN_SECONDS - 5
            COOLDOWN[c["ats"]] = _time.time() + COOLDOWN_SECONDS
            if first:
                print(f"[fetch] {c['ats']} asked us to slow down (429); pausing it for {COOLDOWN_SECONDS // 60} min")
        elif status in (401, 403, 404, 410) or isinstance(e, ValueError):
            s = STRIKES.setdefault(key, [0, 0])
            s[0] += 1
            if s[0] >= STRIKE_LIMIT:
                s[1] = _time.time() + STRIKE_PAUSE
                print(f"[fetch] {c['name']} ({c['ats']}) failed {s[0]} times in a row ({status or 'not a job list'}); "
                      "checking it once a day from now")
        else:
            print(f"[fetch] {c['name']} ({c['ats']}) failed: {e}")
    return out


def fetch_all(companies: list[dict], seen=None, wanted=None, workers: int = 16):
    """companies: [{name, ats, slug}]. Boards are read in parallel (`workers` at a time), so hundreds of companies
    fit in one cycle. Only a few boards' results are held in memory at once. A failing board never stops the
    others. `seen` runs in worker threads: pass a function that is safe to call from any thread (e.g. a set's
    __contains__), not a database call."""
    import time
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    import httpx  # imported here so the filtering/cap logic is testable without network deps

    todo = []
    for c in companies:
        from fetchers_more import READERS as _MORE
        if c["ats"] not in FETCHERS and c["ats"] not in ("eightfold", "workday") and c["ats"] not in _MORE:
            continue
        gap = float(c.get("every_seconds", 0))
        if gap and time.time() - _LAST_RUN.get(c["name"], 0) < gap:
            continue                               # this site is checked less often (it rate-limits)
        _LAST_RUN[c["name"]] = time.time()
        todo.append(c)
    by = {}                                           # interleave job systems so a paced one never blocks the rest
    for c in todo:
        by.setdefault(c["ats"], []).append(c)
    todo = [c for group in __import__("itertools").zip_longest(*by.values()) for c in group if c]
    if not todo:
        return
    limits = httpx.Limits(max_connections=workers * 2, max_keepalive_connections=workers)
    with httpx.Client(headers=UA, timeout=httpx.Timeout(15, connect=8), follow_redirects=True,
                      limits=limits) as client:
        with ThreadPoolExecutor(workers) as ex:
            queue, pending, n = list(todo), set(), 0
            while queue or pending:
                while queue and len(pending) < workers * 2:       # bounded: results never pile up
                    pending.add(ex.submit(_fetch_company, client, queue.pop(0), seen, wanted))
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    n += 1
                    if n % 100 == 0:
                        print(f"[fetch] {n}/{len(todo)} companies read", flush=True)
                    yield from fut.result()
