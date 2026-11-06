"""Grow the watch list automatically from the employer links that listing services return.

Every JSearch listing carries apply links. When one points at a Greenhouse/Lever/Ashby board we do not
watch yet, the board is probed once; if it has an opening for your roles it joins the watch list
(data/learned_boards.json) and is checked directly (and on the fast lane) from then on.
"""
import json
import os

from discover_boards import scan_board, slug_from_url, pretty_name

PATH = "data/learned_boards.json"


def workday_from_url(url: str):
    """Workday job link -> {host, tenant, site} or None (https://nvidia.wd5.myworkdayjobs.com/en-US/Site/job/...)."""
    import re
    from urllib.parse import urlparse
    try:
        u = urlparse(url or "")
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if not re.fullmatch(r"[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com", host):
        return None
    parts = [p for p in u.path.split("/") if p]
    if parts and re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", parts[0]):
        parts = parts[1:]
    if not parts or parts[0] in ("job", "details", "login"):
        return None
    return dict(host=host, tenant=host.split(".")[0], site=parts[0])


def scan_workday(client, w, cfg):
    from roles import title_ok
    try:
        r = client.post(f"https://{w['host']}/wday/cxs/{w['tenant']}/{w['site']}/jobs",
                        json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "engineer"})
        if r.status_code != 200:
            return None
        posts = (r.json() or {}).get("jobPostings") or []
    except Exception:  # noqa: BLE001
        return None
    return {"jobs": len(posts), "hits": sum(1 for p in posts if title_ok(p.get("title", ""), cfg))}


def load(path=PATH):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {"boards": [], "rejected": []}


def merge_into(companies, path=PATH):
    """Append learned boards missing from `companies` (in place). Returns how many were added."""
    n = 0
    have_k = {_key(c) for c in companies}
    for b in load(path)["boards"]:
        if _key(b) not in have_k:
            companies.append(dict(b))
            have_k.add(_key(b))
            n += 1
    return n


def _key(c):
    return (c.get("ats"), c.get("slug") or c.get("url") or f"{c.get('host', '')}/{c.get('site', '')}")


def learn(links, companies, cfg, path=PATH, client=None, limit=40, names=None):
    """Probe unseen job boards found in `links` (any system the agent can read). Adds working ones to `companies`
    and the learned file. Returns the names added."""
    from find_careers import boards_in, validate
    state = load(path)
    known = {_key(c) for c in companies} | {_key(b) for b in state["boards"]}
    known |= {tuple(r) for r in state["rejected"]}
    todo = []
    for url in links:
        for ats, fields in boards_in("", url or ""):
            k = _key(dict(ats=ats, **fields))
            if k not in known and (ats, fields) not in todo:
                todo.append((ats, fields))
                known.add(k)
    todo = todo[:limit]
    if not todo:
        return []
    own = client is None
    if own:
        import httpx
        client = httpx.Client(headers={"User-Agent": "job-agent/0.1 (personal job search)"}, timeout=15,
                              follow_redirects=True)
    added = []
    try:
        for ats, fields in todo:
            if validate(client, ats, fields, cfg):
                label = fields.get("slug") or fields.get("tenant") or (fields.get("host") or "").split(".")[0]
                entry = dict(name=(names or {}).get(label) or pretty_name(label), ats=ats, **fields)
                state["boards"].append(entry)
                companies.append(dict(entry))
                added.append(entry["name"])
            else:
                state["rejected"].append(list(_key(dict(ats=ats, **fields))))
    finally:
        if own:
            client.close()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    json.dump(state, open(path, "w"))
    return added
