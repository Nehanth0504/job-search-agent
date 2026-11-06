"""Readers for more job systems. Each yields the usual job dicts (company, title, url, location, posted_at,
description) and fetches the full text only for postings that match your roles and are new (`seen`/`wanted`).

  bamboohr   {slug}            https://{slug}.bamboohr.com/careers
  breezy     {slug}            https://{slug}.breezy.hr
  pinpoint   {slug}            https://{slug}.pinpointhq.com
  rippling   {slug}            https://ats.rippling.com/{slug}/jobs
  oracle     {host, site}      https://{host}/hcmUI/CandidateExperience/en/sites/{site}   (Oracle Cloud Recruiting)
  icims      {host}            https://{host}/jobs   (e.g. careers-acme.icims.com)
  amazon     -                 https://www.amazon.jobs  (Amazon / AWS own careers site, searched for your roles)
  successfactors {host, site}  https://{host}/{site}/search  (SAP SuccessFactors career sites, e.g. EY)

Built from the systems' public job-board formats. A format that changes makes that one company report
'[fetch] X failed' in the log; the others carry on.
"""
import html as _html
import re
from urllib.parse import quote

from fetchers import _locations, _text

ROLE_KEYWORDS = ["forward deployed", "machine learning engineer", "AI engineer"]   # for systems that need a search


def _skip(job, seen, wanted):
    return (seen and seen(job["url"])) or (wanted and not wanted(job["title"]))


def bamboohr(client, c, seen=None, wanted=None):
    slug = c["slug"]
    r = client.get(f"https://{slug}.bamboohr.com/careers/list")
    r.raise_for_status()
    for j in (r.json() or {}).get("result") or []:
        loc = j.get("location") or {}
        ats_loc = j.get("atsLocation") or {}
        job = dict(company=c["name"], title=j.get("jobOpeningName", ""),
                   url=f"https://{slug}.bamboohr.com/careers/{j.get('id')}",
                   location=_locations(", ".join(x for x in (loc.get("city"), loc.get("state")) if x),
                                       ats_loc.get("country"), "Remote" if j.get("isRemote") else ""),
                   posted_at=None, description="")
        if not _skip(job, seen, wanted):
            d = client.get(f"https://{slug}.bamboohr.com/careers/{j.get('id')}/detail")
            if d.status_code == 200:
                op = ((d.json() or {}).get("result") or {}).get("jobOpening") or {}
                job["description"] = _text(op.get("description", ""))
                job["posted_at"] = op.get("datePosted")
        yield job


def breezy(client, c, seen=None, wanted=None):
    slug = c["slug"]
    r = client.get(f"https://{slug}.breezy.hr/json")
    r.raise_for_status()
    for j in r.json() or []:
        loc = j.get("location") or {}
        state = (loc.get("state") or {}).get("name") if isinstance(loc.get("state"), dict) else loc.get("state")
        country = (loc.get("country") or {}).get("name") if isinstance(loc.get("country"), dict) else loc.get("country")
        job = dict(company=c["name"], title=j.get("name", ""), url=j.get("url") or f"https://{slug}.breezy.hr/p/{j.get('friendly_id')}",
                   location=_locations(loc.get("name"), ", ".join(x for x in (loc.get("city"), state) if x), country,
                                       "Remote" if loc.get("is_remote") else ""),
                   posted_at=j.get("published_date"), description="")
        if not _skip(job, seen, wanted):
            d = client.get(job["url"])
            if d.status_code == 200:
                m = re.search(r'class="description"[^>]*>(.*?)</div>\s*</div>', d.text, re.S)
                job["description"] = _text(m.group(1) if m else d.text)[:20000]
        yield job


def pinpoint(client, c, seen=None, wanted=None):
    slug = c["slug"]
    r = client.get(f"https://{slug}.pinpointhq.com/postings.json")
    r.raise_for_status()
    for j in (r.json() or {}).get("data") or []:
        loc = j.get("location") or {}
        yield dict(company=c["name"], title=j.get("title", ""),
                   url=j.get("url") or f"https://{slug}.pinpointhq.com/postings/{j.get('id')}",
                   location=_locations(loc.get("name"), ", ".join(x for x in (loc.get("city"), loc.get("province")) if x),
                                       "Remote" if str(j.get("workplace_type", "")).lower() == "remote" else ""),
                   posted_at=j.get("published_at") or j.get("created_at"),
                   description=_text(" ".join(str(j.get(k) or "") for k in
                                              ("description", "key_responsibilities", "skills_knowledge_expertise"))))


def rippling(client, c, seen=None, wanted=None):
    slug = c["slug"]
    r = client.get(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs")
    r.raise_for_status()
    data = r.json()
    for j in (data if isinstance(data, list) else (data or {}).get("items") or []):
        wl = j.get("workLocation") or {}
        yield dict(company=c["name"], title=j.get("name", ""),
                   url=j.get("url") or f"https://ats.rippling.com/{slug}/jobs/{j.get('uuid')}",
                   location=_locations(wl.get("label") if isinstance(wl, dict) else wl), posted_at=None,
                   description="")


def oracle(client, c, seen=None, wanted=None):
    host, site = c["host"], c["site"]
    api = f"https://{host}/hcmRestApi/resources/latest"
    done, fetched = set(), 0
    for kw in c.get("queries") or ROLE_KEYWORDS:
        finder = (f'findReqs;siteNumber={site},facetsList=NONE,limit=50,keyword="{kw}",sortBy=POSTING_DATES_DESC')
        r = client.get(f"{api}/recruitingCEJobRequisitions",
                       params={"onlyData": "true", "expand": "requisitionList.secondaryLocations", "finder": finder})
        if r.status_code == 429:
            return
        r.raise_for_status()
        for block in (r.json() or {}).get("items") or []:
            for j in block.get("requisitionList") or []:
                jid = j.get("Id")
                if not jid or jid in done:
                    continue
                done.add(jid)
                extra = [s.get("Name") for s in j.get("secondaryLocations") or [] if isinstance(s, dict)]
                job = dict(company=c["name"], title=j.get("Title", ""),
                           url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}",
                           location=_locations(j.get("PrimaryLocation"), extra, j.get("PrimaryLocationCountry")),
                           posted_at=j.get("PostedDate"), description=_text(j.get("ShortDescriptionStr", "")))
                if not _skip(job, seen, wanted) and fetched < 20:
                    fetched += 1
                    try:
                        d = client.get(f"{api}/recruitingCEJobRequisitionDetails",
                                       params={"onlyData": "true", "expand": "all",
                                               "finder": f'ById;Id="{jid}",siteNumber={site}'})
                        if d.status_code == 200:
                            it = ((d.json() or {}).get("items") or [{}])[0]
                            job["description"] = _text(" ".join(str(it.get(k) or "") for k in (
                                "ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")))
                    except Exception:  # noqa: BLE001
                        pass
                yield job


_ICIMS_LINK = re.compile(r'href="(https?://[^"]+/jobs/(\d+)/[^"/]+/job)[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
_ICIMS_LOC = re.compile(r"(?:Job Locations?|Location)\s*</[^>]+>\s*(?:<[^>]+>\s*)*([^<]{3,120})", re.I)


def icims(client, c, seen=None, wanted=None):
    host = c["host"]
    done, fetched = set(), 0
    for kw in c.get("queries") or ROLE_KEYWORDS:
        r = client.get(f"https://{host}/jobs/search?ss=1&searchKeyword={quote(kw)}&in_iframe=1")
        r.raise_for_status()
        for url, jid, inner in _ICIMS_LINK.findall(r.text):
            if jid in done:
                continue
            title = _html.unescape(re.sub(r"<[^>]+>", " ", inner)).strip()
            title = " ".join(title.split())
            if not title or title.lower() in ("apply", "view job", "learn more"):
                continue
            done.add(jid)
            job = dict(company=c["name"], title=title, url=url.split("?")[0], location="", posted_at=None,
                       description="")
            if not _skip(job, seen, wanted) and fetched < 20:
                fetched += 1
                try:
                    d = client.get(job["url"] + "?in_iframe=1")
                    if d.status_code == 200:
                        m = _ICIMS_LOC.search(d.text)
                        job["location"] = " ".join(_html.unescape(m.group(1)).split()) if m else ""
                        p = re.search(r"Posted Date\s*</[^>]+>\s*(?:<[^>]+>\s*)*([^<]{3,40})", d.text, re.I)
                        job["posted_at"] = p.group(1).strip() if p else None
                        job["description"] = _text(d.text)[:20000]
                except Exception:  # noqa: BLE001
                    pass
            yield job


def amazon(client, c, seen=None, wanted=None):
    """amazon.jobs search (US). Each result already carries its full text and posting date."""
    done = set()
    for kw in c.get("queries") or ["forward deployed"]:
        for offset in (0, 100):
            r = client.get("https://www.amazon.jobs/en/search.json", params={
                "base_query": kw, "country": "USA", "result_limit": 100, "offset": offset, "sort": "recent"})
            r.raise_for_status()
            jobs = r.json().get("jobs") or []
            for j in jobs:
                path = j.get("job_path") or ""
                if not path or path in done:
                    continue
                done.add(path)
                desc = " ".join(_text(j.get(k) or "") for k in ("description", "basic_qualifications",
                                                                 "preferred_qualifications"))
                yield dict(company=c["name"], title=(j.get("title") or "").strip(),
                           url="https://www.amazon.jobs" + path,
                           location=j.get("normalized_location") or j.get("location") or "",
                           posted_at=j.get("posted_date"), description=desc[:20000])
            if len(jobs) < 100:
                break


_SF_ROW = re.compile(r'<a[^>]+href="(/[^"]*?/job/[^"]+?/\d+/?)"[^>]*class="[^"]*jobTitle-link[^"]*"[^>]*>(.*?)</a>'
                     r'|<a[^>]+class="[^"]*jobTitle-link[^"]*"[^>]+href="(/[^"]*?/job/[^"]+?/\d+/?)"[^>]*>(.*?)</a>', re.S)
_SF_DATE = re.compile(r'class="[^"]*jobDate[^"]*"[^>]*>\s*([^<]+?)\s*<', re.S)
_SF_LOC = re.compile(r'class="[^"]*jobLocation[^"]*"[^>]*>\s*([^<]+?)\s*<', re.S)


def successfactors(client, c, seen=None, wanted=None):
    """SAP SuccessFactors career site (Career Site Builder), searched for your roles in the US."""
    host, site = c["host"], c.get("site", "")
    base = f"https://{host}/{site}".rstrip("/")
    done, fetched = set(), 0
    for kw in c.get("queries") or ["forward deployed"]:
        r = client.get(f"{base}/search/", params={"q": kw, "locationsearch": "United States"})
        r.raise_for_status()
        page = r.text
        rows = _SF_ROW.findall(page)
        dates, locs = _SF_DATE.findall(page), _SF_LOC.findall(page)
        for i, m in enumerate(rows):
            path, inner = (m[0], m[1]) if m[0] else (m[2], m[3])
            if path in done:
                continue
            done.add(path)
            title = " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", inner)).split())
            job = dict(company=c["name"], title=title, url=f"https://{host}{path}",
                       location=_html.unescape(locs[i]) if i < len(locs) else "",
                       posted_at=dates[i].strip() if i < len(dates) else None, description="")
            if not _skip(job, seen, wanted) and fetched < 20:
                fetched += 1
                try:
                    d = client.get(job["url"])
                    if d.status_code == 200:
                        m2 = re.search(r'class="[^"]*jobdescription[^"]*"[^>]*>(.*?)</span>\s*</span>', d.text, re.S | re.I)
                        job["description"] = _text(m2.group(1) if m2 else d.text)[:20000]
                except Exception:  # noqa: BLE001
                    pass
            yield job


READERS = {"amazon": amazon, "successfactors": successfactors, "bamboohr": bamboohr, "breezy": breezy, "pinpoint": pinpoint, "rippling": rippling,
           "oracle": oracle, "icims": icims}
LIST_URLS = {   # cheap "does this board exist and have jobs" checks for slug-based systems
    "bamboohr": "https://{}.bamboohr.com/careers/list",
    "breezy": "https://{}.breezy.hr/json",
    "pinpoint": "https://{}.pinpointhq.com/postings.json",
    "rippling": "https://api.rippling.com/platform/api/ats/v1/board/{}/jobs",
}


def has_jobs(ats: str, data) -> bool:
    if ats == "bamboohr":
        return isinstance(data, dict) and bool(data.get("result"))
    if ats == "breezy":
        return isinstance(data, list) and len(data) > 0
    if ats == "pinpoint":
        return isinstance(data, dict) and bool(data.get("data"))
    if ats == "rippling":
        return bool(data if isinstance(data, list) else (data or {}).get("items"))
    return False


# --------------------------------------------------------------------------- a company's own careers page
_A = re.compile(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', re.S | re.I)
_LD = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)


def _ld_postings(page: str):
    """JobPosting objects embedded in a page (the schema.org format Google for Jobs reads)."""
    import json
    out = []
    for block in _LD.findall(page or ""):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                if d.get("@type") == "JobPosting" or (isinstance(d.get("@type"), list) and "JobPosting" in d["@type"]):
                    out.append(d)
                stack.extend(v for k, v in d.items() if k in ("@graph", "itemListElement", "item") for v in
                             (v if isinstance(v, list) else [v]))
    return out


def _ld_location(p):
    locs = p.get("jobLocation") or []
    locs = locs if isinstance(locs, list) else [locs]
    out = []
    for l in locs:
        a = (l or {}).get("address") or {}
        if isinstance(a, dict):
            out.append(", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion"),
                                              a.get("addressCountry") if isinstance(a.get("addressCountry"), str)
                                              else (a.get("addressCountry") or {}).get("name")) if x))
    if str(p.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        out.append("Remote")
    return _locations(out)


def careerpage(client, c, seen=None, wanted=None):
    """Read a company's own careers page: job links whose text matches your roles, plus any JobPosting data."""
    from urllib.parse import urljoin
    base = c["url"]
    r = client.get(base)
    r.raise_for_status()
    page = r.text[:1_500_000]
    found = {}
    for p in _ld_postings(page):                                   # structured postings on the list page itself
        u = urljoin(str(r.url), p.get("url") or base)
        found[u] = dict(title=_html.unescape(str(p.get("title", ""))).strip(), ld=p)
    for href, inner in _A.findall(page):
        title = " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", inner)).split())
        if 6 <= len(title) <= 160:
            u = urljoin(str(r.url), _html.unescape(href))
            if u.startswith("http") and u not in found:
                found[u] = dict(title=title, ld=None)
    fetched = 0
    for u, f in found.items():
        if not (wanted and wanted(f["title"])):
            continue                                               # only links that look like your role
        job = dict(company=c["name"], title=f["title"], url=u, location="", posted_at=None, description="")
        p = f["ld"]
        if p is None and not (seen and seen(u)) and fetched < 10:
            fetched += 1
            try:
                d = client.get(u)
                if d.status_code == 200:
                    lds = _ld_postings(d.text)
                    p = lds[0] if lds else None
                    if not p:
                        job["description"] = _text(d.text)[:20000]
            except Exception:  # noqa: BLE001
                pass
        if p:
            job["title"] = _html.unescape(str(p.get("title") or job["title"])).strip()
            job["location"] = _ld_location(p)
            job["posted_at"] = p.get("datePosted")
            job["description"] = _text(str(p.get("description", "")))
        yield job


READERS["careerpage"] = careerpage
CAREER_WORDS = re.compile(r"\b(open (?:positions|roles)|job openings|current openings|join (?:our|the) team|"
                          r"careers?|we(?:'| a)re hiring|view (?:all )?jobs|search jobs)\b", re.I)


def looks_like_careers_page(page: str) -> bool:
    return bool(CAREER_WORDS.search(page or "")) and page.count("<a ") >= 5
