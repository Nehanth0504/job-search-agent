"""Trace a job found on a job-search site back to the company's own posting.

1. Company already watched: look for the same title on its own job board.
2. Otherwise: open the company's website, find the job system it uses (or its careers page), look for the title
   there, and start watching that board.
Returns the company's own URL for the job, or None when the job cannot be found at the source (usually closed).
"""
import json
import os
import re

from db import normalize_company

DROP = {"remote", "hybrid", "onsite", "on", "site", "us", "usa", "united", "states", "the", "and", "of", "a", "-"}


def _words(title: str):
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", (title or "").lower())
    return [w for w in re.findall(r"[a-z0-9+#]+", t) if w not in DROP]


def same_title(a: str, b: str) -> bool:
    """'Remote AI Forward Deployed Engineer Enterprise' ~ 'AI Forward Deployed Engineer - Enterprise'."""
    wa, wb = set(_words(a)), set(_words(b))
    if not wa or not wb:
        return False
    small, big = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return len(small & big) / len(small) >= 0.85 and len(wa & wb) / len(wa | wb) >= 0.6


_FILLER = {"the", "usa", "us", "america", "americas", "opco", "services", "service", "north", "global", "worldwide",
           "operations", "intl", "international", "u", "s", "na"}


def company_key(name: str) -> tuple:
    """'HOME DEPOT USA INC' and 'The Home Depot' -> ('home', 'depot'); 'OPENAI OPCO LLC' and 'OpenAI' -> ('openai',)."""
    n = normalize_company(re.sub(r"\(.*?\)", " ", name or ""))
    toks = tuple(w for w in n.split() if w not in _FILLER)
    return toks or tuple(n.split())


def same_company(a: str, b: str) -> bool:
    ka, kb = company_key(a), company_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    short, long_ = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    return long_[:len(short)] == short and len("".join(short)) >= 5   # 'salesforce' ~ 'salesforce com'


def _host(url):
    from links import host
    h = host(url or "")
    return h[4:] if h.startswith("www.") else h


def _search(client, c, title):
    from fetchers import _fetch_company
    for j in _fetch_company(client, c, None, lambda t: same_title(t, title)):
        if same_title(j["title"], title) and j.get("url"):
            return j["url"]
    return None


def resolve(job: dict, companies: list, cfg: dict, client=None, learned_path: str = "data/learned_boards.json"):
    """The company's own URL for this job, or None. May add the company's board to `companies` (watched)."""
    own = client is None
    if own:
        import httpx
        client = httpx.Client(headers={"User-Agent": "Mozilla/5.0 (job-agent personal job search)"},
                              timeout=12, follow_redirects=True)
    try:
        name = normalize_company(job["company"])
        for c in [c for c in companies if same_company(c["name"], job["company"])][:3]:
            url = _search(client, c, job["title"])
            if url:
                return url
        from find_careers import find
        domains = [d for d in [_host(job.get("employer_site"))] if d]
        entry = find(job["company"], client, cfg, domains=domains)
        if not entry:
            return None
        if not any(normalize_company(c["name"]) == name and c.get("ats") == entry.get("ats") for c in companies):
            entry = dict(entry, name=job["company"])
            companies.append(entry)                    # watched from now on
            try:
                from learn import load
                state = load(learned_path)
                state["boards"].append(entry)
                os.makedirs(os.path.dirname(learned_path) or ".", exist_ok=True)
                json.dump(state, open(learned_path, "w"))
            except Exception:  # noqa: BLE001
                pass
        return _search(client, entry, job["title"])
    except Exception as e:  # noqa: BLE001
        print(f"[resolve] {job.get('company')}: {e}")
        return None
    finally:
        if own:
            client.close()
