"""Rewrite a tailored resume for one posting with Claude, the way a resume writer targets an ATS.

What Claude is asked to do (and what it may not do):
  * headline = the posting's job title (how recruiters search an ATS), e.g. "Forward Deployed Engineer"
  * a 3-4 sentence summary written for this role, using the posting's own key terms where your facts support them
  * for each job: pick your most relevant bullets and rephrase them in the posting's wording
    (strong action verb, what you built, with which tools, the result)
  * NEVER invent an employer, title, date, number, tool or achievement

Every rewritten line is checked before it is used:
  * any number in it must appear in the bullet(s) it was written from
  * any skill / tool in it must already be somewhere in your master resume
A line that fails a check is replaced by your original bullet, so nothing untrue reaches a resume.

Needs ANTHROPIC_API_KEY in the server's .env. Settings in config.yaml under `claude:`. Without a key, or past the
daily limit, the agent falls back to the plain (pick and reorder) tailoring.
"""
from __future__ import annotations

import datetime
import json
import os
import re

from scoring import find_skills

API = "https://api.anthropic.com/v1/messages"
USAGE_FILE = "data/claude_usage.json"
CANDIDATES = [14, 10, 8, 6]          # bullets offered to Claude for the newest, 2nd, 3rd ... job

SYSTEM = """You are an expert technical resume writer who tailors resumes to pass applicant tracking systems \
(Workday, Greenhouse, Lever, iCIMS, Taleo) and recruiter keyword searches, while staying strictly truthful.

Hard rules:
- Use ONLY facts in the candidate's material. Never add a tool, technology, number, metric, employer, title, \
client, domain or achievement that is not stated there.
- You may rephrase, merge two related bullets, reorder, and use the posting's exact wording for something the \
candidate really did (e.g. the material says "built REST APIs for partner integrations" and the posting says \
"customer integrations" -> "Built REST APIs for customer integrations" is fine only if partners were customers).
- Every number in a rewritten bullet must appear in the source bullets you cite in "from".
- Each bullet: starts with a strong past-tense action verb (current job may use past tense too), no first person, \
one sentence, 18-35 words, shows what was built, with which tools, and the outcome when the source states one.
- Use the posting's key terms verbatim where truthful; write an acronym once with its full form if the posting does.
- Summary: 3-4 sentences, no first person, opens with years of experience and the target role's focus, \
includes the posting's most important terms the candidate genuinely has.
- Headline: the posting's job title as written, without locations, team names in brackets or requisition numbers.

Reply with JSON only, no other text."""


def _usage_ok(limit: int) -> bool:
    today = datetime.date.today().isoformat()
    try:
        u = json.load(open(USAGE_FILE))
    except (OSError, ValueError):
        u = {}
    return u.get(today, 0) < limit


def _count_use():
    today = datetime.date.today().isoformat()
    try:
        u = json.load(open(USAGE_FILE))
    except (OSError, ValueError):
        u = {}
    u = {today: u.get(today, 0) + 1}
    os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
    json.dump(u, open(USAGE_FILE, "w"))


def settings() -> dict:
    try:
        import yaml
        c = (yaml.safe_load(open("config.yaml")) or {}).get("claude") or {}
    except (OSError, ValueError, ImportError):
        c = {}
    return {"enabled": c.get("enabled", True), "model": c.get("model", "claude-sonnet-5-5"),
            "max_per_day": int(c.get("max_per_day", 25))}


def available() -> bool:
    s = settings()
    return bool(s["enabled"] and os.environ.get("ANTHROPIC_API_KEY") and _usage_ok(s["max_per_day"]))


def _call(model: str, prompt: str) -> str:
    import httpx
    r = httpx.post(API, timeout=httpx.Timeout(120, connect=10), headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
        "content-type": "application/json"},
        json={"model": model, "max_tokens": 4000, "system": SYSTEM,
              "messages": [{"role": "user", "content": prompt}]})
    if r.status_code != 200:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:200]}")
    return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


def _hard(text: str) -> set:
    """Tools / technologies named in the text (soft skills like 'customer-facing' are not checked)."""
    from ats import SOFT_IN_VOCAB
    return set(find_skills(text)) - SOFT_IN_VOCAB


def _numbers(text: str) -> set:
    return set(re.findall(r"\d+(?:[.,]\d+)?", text or ""))


def _ok(new: str, sources: list[str], owned: set) -> bool:
    if not new or len(new.split()) < 6 or len(new.split()) > 45:
        return False
    if not _numbers(new) <= _numbers(" ".join(sources)):
        return False                                   # a number that isn't in the source
    return _hard(new) <= owned               # a tool / skill you don't list anywhere


def _offer(bullets, chosen, n, w):
    """The bullets offered for one job: the ones already chosen first, then the next most relevant."""
    from tailor import _bullet_score
    rest = sorted((b for b in bullets if b not in chosen), key=lambda b: -_bullet_score(b, w))
    return (list(chosen) + rest)[:max(n, len(chosen))]


def rewrite(struct: dict, master: dict, job: dict, owned: set, call=None) -> dict:
    """Return a copy of `struct` (from tailor.select) rewritten for `job`. Raises on API trouble."""
    from tailor import _weights
    s = settings()
    w = _weights(job)
    offered = []
    for i, (mj, tj) in enumerate(zip(master["experience"], struct["experience"])):
        offered.append(_offer(mj["bullets"], tj["bullets"], CANDIDATES[min(i, len(CANDIDATES) - 1)], w))
    proj = [p.get("bullets", []) for p in struct.get("projects") or []]

    lines = [f"POSTING TITLE: {job['title']}", f"COMPANY: {job.get('company', '')}",
             "POSTING TEXT:", (job.get("description") or "")[:9000], "",
             "CANDIDATE MATERIAL (the only facts you may use)",
             f"Current summary: {master.get('summary', '')}",
             "Skills: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in master.get("skills", {}).items())]
    for i, (tj, bl) in enumerate(zip(struct["experience"], offered)):
        lines.append(f"\nJOB {i}: {tj.get('title', '')} at {tj.get('company', '')} ({tj.get('start', '')} - {tj.get('end', '')}) "
                     f"- write at most {len(tj['bullets'])} bullets")
        lines += [f"  [{i}.{k}] {b}" for k, b in enumerate(bl)]
    for i, (p, bl) in enumerate(zip(struct.get("projects") or [], proj)):
        lines.append(f"\nPROJECT {i}: {p.get('name', '')} - write at most {len(bl)} bullets")
        lines += [f"  [p{i}.{k}] {b}" for k, b in enumerate(bl)]
    lines.append('\nReturn JSON: {"headline": "...", "summary": "...", '
                 '"jobs": [{"job": 0, "bullets": [{"from": ["0.3", "0.7"], "text": "..."}]}], '
                 '"projects": [{"project": 0, "bullets": [{"from": ["p0.1"], "text": "..."}]}]}. '
                 "Order each job's bullets most relevant to the posting first.")

    raw = (call or (lambda p: _call(s["model"], p)))("\n".join(lines))
    _count_use()
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0) if m else raw)

    def src(ids):
        out = []
        for x in ids or []:
            x = str(x)
            if x.startswith("p") and "." in x:
                a, b = x[1:].split(".", 1)
                pool = proj
            elif "." in x:
                a, b = x.split(".", 1)
                pool = offered
            else:
                continue
            try:
                out.append(pool[int(a)][int(b)])
            except (ValueError, IndexError):
                pass
        return out

    new = json.loads(json.dumps(struct))
    kept = rejected = 0
    for item in data.get("jobs", []):
        i = item.get("job")
        if not isinstance(i, int) or not 0 <= i < len(new["experience"]):
            continue
        cap = len(struct["experience"][i]["bullets"])
        out = []
        for b in item.get("bullets", [])[:cap]:
            sources = src(b.get("from"))
            if sources and _ok(b.get("text", ""), sources, owned):
                out.append(b["text"].strip())
                kept += 1
            elif sources:
                out.append(sources[0])                  # failed a check: your original bullet instead
                rejected += 1
        if out:
            new["experience"][i]["bullets"] = list(dict.fromkeys(out))
    for item in data.get("projects", []):
        i = item.get("project")
        if not isinstance(i, int) or not 0 <= i < len(new.get("projects") or []):
            continue
        out = []
        for b in item.get("bullets", [])[:len(proj[i])]:
            sources = src(b.get("from"))
            if sources and _ok(b.get("text", ""), sources, owned):
                out.append(b["text"].strip())
            elif sources:
                out.append(sources[0])
        if out:
            new["projects"][i]["bullets"] = list(dict.fromkeys(out))

    all_text = master.get("summary", "") + " " + " ".join(b for bl in offered for b in bl)
    summ = (data.get("summary") or "").strip()
    if summ and _numbers(summ) <= _numbers(all_text) | _numbers(master.get("summary", "")) \
            and _hard(summ) <= owned and 30 <= len(summ.split()) <= 110:
        new["summary"] = summ
    head = re.sub(r"\s+", " ", (data.get("headline") or "")).strip()
    if head and len(head) <= 80 and _hard(head) <= owned | _hard(job["title"]):
        new["headline"] = head
    new["rewritten"] = {"by": s["model"], "kept": kept, "replaced_with_original": rejected}
    return new
