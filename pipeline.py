"""One polling cycle: fetch -> role filter -> dedupe -> US only -> level -> sponsorship -> score -> company cap -> notify.

Scoring is skill coverage against your master resume (scoring.py); an LLM pass is layered
on top later for jobs that clear it.
"""
from __future__ import annotations

import json
import re
import os
import sys
import time

import yaml

from db import Tracker
from dates import cutoff, to_iso
from fetchers import fetch_aggregators, fetch_all
from h1b_history import H1BHistory
from locations import classify_location, us_in_description, where_with_description  # noqa: F401
from roles import level_check, title_ok  # noqa: F401  (title_ok is re-exported for the tests)
from scoring import load_profile, score_job
from sponsorship import classify, context_sentence


def load_env(path=".env"):
    """Read KEY=value lines from a local .env file into the environment (so keys survive closing the terminal)."""
    import os
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def notify(job: dict, bumped_id=None, to_addr=None, attachment=None):
    from notifier import send_job_alert
    try:
        send_job_alert(job, to_addr, bumped_id, attachment=attachment)
    except Exception as e:  # noqa: BLE001 - a mail problem must never stop the agent
        print(f"[warn] email alert failed: {e}")


def make_resume(job: dict, job_id: int, cfg: dict, tracker=None):
    """Tailored resume for one job (see tailor.py). Never blocks the alert: on failure returns None."""
    try:
        from tailor import tailor
        path, res = tailor(job, job_id, cfg.get("master_resume", "profile/master_resume.json"))
        job["ats_score"] = res["score"]
        if tracker is not None:
            tracker.set_ats(job_id, res, os.path.basename(path))
        print(f"[resume] {job['company']} | {job['title']} | ATS check {res['score']} -> {path}")
        return path
    except Exception as e:  # noqa: BLE001
        print(f"[warn] could not tailor resume for {job.get('company')}: {e}")
        return None


def rescore_stored(tracker: Tracker, resume_skills: set) -> int:
    """Jobs saved earlier (held ones, or from older versions) may have no score: fill them in."""
    rows = tracker.db.execute(
        "SELECT id, title, description, score, sponsorship, sponsorship_evidence FROM jobs "
        "WHERE score IS NULL OR (COALESCE(sponsorship,'unclear')='unclear' AND sponsorship_evidence IS NULL)").fetchall()
    for r in rows:
        score, matched, gaps = score_job(dict(r), resume_skills)
        quote = context_sentence(r["description"])
        ev = r["sponsorship_evidence"] or (f'Posting says: "{quote}"' if quote else
                                           "The posting never mentions visas or sponsorship.")
        tracker.db.execute("UPDATE jobs SET score=COALESCE(score, ?), matched=?, gaps=?, "
                           "sponsorship=COALESCE(sponsorship,'unclear'), sponsorship_evidence=? WHERE id=?",
                           (score, ", ".join(matched), ", ".join(gaps), ev, r["id"]))
    tracker.db.commit()
    return len(rows)


def normalize_dates(tracker: Tracker) -> int:
    """Older rows stored each source's own date format: convert them once (relative ones from when first seen)."""
    rows = tracker.db.execute("SELECT id, posted_at, first_seen FROM jobs WHERE posted_at IS NOT NULL AND "
                              "posted_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*Z'").fetchall()
    for r in rows:
        tracker.db.execute("UPDATE jobs SET posted_at=? WHERE id=?", (to_iso(r["posted_at"], r["first_seen"]), r["id"]))
    tracker.db.commit()
    return len(rows)


def demote_old(tracker: Tracker, cfg: dict) -> int:
    """New matches whose posting date is older than max_posting_age_days leave the New match list
    (only ones you have not acted on). Frees their company slots for fresh postings."""
    max_age = cfg.get("max_posting_age_days")
    if not max_age:
        return 0
    cur = tracker.db.execute(
        "UPDATE jobs SET status='too_old', updated=? WHERE status IN "
        "('notified','review_queue','needs_sponsorship_check','location_unclear','capped','new') "
        "AND posted_at IS NOT NULL AND posted_at < ?", (time.time(), cutoff(max_age)))
    tracker.db.commit()
    return cur.rowcount


def hide_other_roles(tracker: Tracker, cfg: dict) -> int:
    """After narrowing your target roles: jobs you have not acted on whose title no longer fits
    leave the lists (status 'other_role'). Applied / interview / offer are never touched."""
    rows = tracker.db.execute(
        "SELECT id, title FROM jobs WHERE status IN ('notified','review_queue','needs_sponsorship_check',"
        "'location_unclear','capped','low_score','wrong_level','too_old','new')").fetchall()
    n = 0
    for r in rows:
        if not title_ok(r["title"], cfg):
            tracker.db.execute("UPDATE jobs SET status='other_role', updated=? WHERE id=?", (time.time(), r["id"]))
            n += 1
    tracker.db.commit()
    return n


def release_location_unclear(tracker: Tracker, cfg: dict) -> int:
    """With location.unclear = allow, jobs held earlier for an unclear location join the new matches."""
    if (cfg.get("location") or {}).get("unclear", "hold") != "allow":
        return 0
    rows = tracker.db.execute("SELECT * FROM jobs WHERE status='location_unclear' ORDER BY score DESC").fetchall()
    max_age = cfg.get("max_posting_age_days")
    for r in rows:
        if max_age and r["posted_at"] and r["posted_at"] < cutoff(max_age):
            tracker.set_status(r["id"], "too_old")
            continue
        if (r["score"] or 0) < cfg.get("min_score", 0):
            tracker.set_status(r["id"], "low_score")
            continue
        granted, _ = tracker.claim_slot(r["id"])
        if granted:
            make_resume(dict(r), r["id"], cfg, tracker)
    return len(rows)


def refresh_after_master_change(tracker: Tracker, cfg: dict, data_dir: str = "data") -> int:
    """After you upload a new master resume, rebuild the tailored resume, cover letter and match score of every
    current match whose resume is older than the master. Jobs you applied to (or later) keep what you sent."""
    import json
    master = cfg.get("master_resume", "profile/master_resume.json")
    try:
        version = os.path.getmtime(master)
    except OSError:
        return 0
    from scoring import find_skills
    from suggest import _text
    from tailor import _claude_ready
    claude_on, claude_left = _claude_ready(), 8          # existing resumes get the Claude version, 8 per check
    n = 0
    for r in tracker.db.execute("SELECT * FROM jobs WHERE status IN ('notified','review_queue') "
                                "ORDER BY score DESC").fetchall():
        js = os.path.join(data_dir, "resumes", str(r["id"]), "tailored.json")
        old_report = r["ats_report"] and '"v": 3' not in r["ats_report"]    # scored by an earlier ATS check
        needs_claude = False
        if os.path.exists(js) and claude_on and claude_left > 0:
            try:
                needs_claude = "rewritten" not in json.load(open(js))       # made before Claude was added
            except (OSError, ValueError):
                pass
        if os.path.exists(js) and os.path.getmtime(js) >= version and not old_report and not needs_claude:
            continue                                    # already built from the current master
        if needs_claude:
            claude_left -= 1
        if not os.path.exists(js) and not r["resume_file"]:
            continue                                    # never had a resume: the normal flow makes one
        path = make_resume(dict(r), r["id"], cfg, tracker)
        if not path:
            continue
        try:
            st = json.load(open(os.path.join(os.path.dirname(path), "tailored.json")))
            score, matched, gaps = score_job(dict(r), set(find_skills(_text(st))))
            tracker.set_match(r["id"], score, matched, gaps)
        except (OSError, ValueError):
            pass
        n += 1
    if n:
        print(f"[resume] rebuilt {n} tailored resumes and their ATS match")
    return n


def resolve_existing(tracker: Tracker, cfg: dict, companies: list, limit: int = 20,
                     state_path: str = "data/resolved.json") -> int:
    """Matches saved earlier with a job-site link: find the company's own posting, or move them to
    'Only on job sites'. A few per run, so a check never takes long."""
    import json
    if not cfg.get("company_source_only", True):
        return 0
    from links import is_recruiter, kind
    from resolver import resolve
    try:
        tried = set(json.load(open(state_path)))
    except (OSError, ValueError):
        tried = set()
    n = 0
    for r in tracker.db.execute("SELECT * FROM jobs WHERE status IN ('notified','review_queue') "
                                "ORDER BY score DESC").fetchall():
        if n >= limit:
            break
        if r["id"] in tried or kind(r["url"], r["company"], r["employer_site"] or "") != "jobsite":
            continue                                   # only links to known job-search sites are traced
        tried.add(r["id"])
        n += 1
        own_url = resolve(dict(r), companies, cfg)
        if own_url and not tracker.seen(own_url):
            tracker.db.execute("UPDATE jobs SET url=?, updated=? WHERE id=?", (own_url, time.time(), r["id"]))
        elif cfg.get("unconfirmed_jobs", "flag") != "flag" or is_recruiter(r["company"], r["description"] or ""):
            tracker.db.execute("UPDATE jobs SET status='jobsite_only', updated=? WHERE id=?", (time.time(), r["id"]))
        # else: stays listed, flagged "Not confirmed on company site"
        tracker.db.commit()
    if n:
        os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
        json.dump(sorted(tried), open(state_path, "w"))
    return n


def recheck_locations(tracker: Tracker, cfg: dict) -> int:
    """Matches already listed whose location turns out to be outside the US leave the lists."""
    if (cfg.get("location") or {}).get("country", "US") != "US":
        return 0
    hold = (cfg.get("location") or {}).get("unclear", "hold") != "allow"
    n = 0
    for r in tracker.db.execute("SELECT id, location, description, status FROM jobs WHERE status IN "
                                "('notified','review_queue','capped','location_unclear')").fetchall():
        where = where_with_description(r["location"], r["description"])
        new = "outside_us" if where == "non_us" else ("location_unclear" if where == "unclear" and hold else None)
        if new and new != r["status"]:
            tracker.db.execute("UPDATE jobs SET status=?, updated=? WHERE id=?", (new, time.time(), r["id"]))
            n += 1
    if n:
        tracker.db.commit()
        print(f"[location] {n} listed jobs are not clearly in the US and were moved out of New match")
    return n


def release_unconfirmed(tracker: Tracker, cfg: dict) -> int:
    """Recent jobs from real employers that were held as 'Only on job sites' join the new matches,
    flagged 'Not confirmed on company site'. Recruiters / job sites and non-US jobs stay out."""
    if cfg.get("unconfirmed_jobs", "flag") != "flag":
        return 0
    from links import is_recruiter
    from locations import _NON_US
    max_age = cfg.get("max_posting_age_days") or 3
    since = time.time() - max_age * 86400
    n = 0
    for r in tracker.db.execute("SELECT * FROM jobs WHERE status='jobsite_only' ORDER BY score DESC").fetchall():
        if r["posted_at"] and r["posted_at"] < cutoff(max_age):
            continue
        if not r["posted_at"] and (r["first_seen"] or 0) < since:
            continue
        if (is_recruiter(r["company"], r["description"] or "") or not title_ok(r["title"], cfg)
                or looks_like_news(r["title"]) or _NON_US.search((r["title"] or "").lower())
                or where_with_description(r["location"], r["description"]) == "non_us"):
            continue
        granted, _ = tracker.claim_slot(r["id"])
        if granted:
            make_resume(dict(r), r["id"], cfg, tracker)
            n += 1
    if n:
        print(f"[unconfirmed] {n} jobs not yet found on the company's own site listed with a flag", flush=True)
    return n


def release_low_score(tracker: Tracker, cfg: dict) -> int:
    """After min_score is lowered, recent jobs held only for a low match score join the new matches."""
    floor = cfg.get("min_score", 0)
    max_age = cfg.get("max_posting_age_days")
    rows = tracker.db.execute("SELECT * FROM jobs WHERE status='low_score' AND coalesce(score, 0) >= ? "
                              "ORDER BY score DESC", (floor,)).fetchall()
    n = 0
    for r in rows:
        if max_age and r["posted_at"] and r["posted_at"] < cutoff(max_age):
            continue
        granted, _ = tracker.claim_slot(r["id"])
        if granted:
            make_resume(dict(r), r["id"], cfg, tracker)
            n += 1
    return n


def release_held(tracker: Tracker, cfg: dict, h1b=None) -> int:
    """Jobs held earlier only because sponsorship was unclear: re-check the H-1B history, then list them with
    the new matches (flagged in the dashboard) and build their resumes. No email for this backlog."""
    if cfg.get("hold_unclear_sponsorship", False):
        return 0
    rows = tracker.db.execute("SELECT * FROM jobs WHERE status='needs_sponsorship_check' ORDER BY score DESC").fetchall()
    for r in rows:
        if h1b:
            hist = h1b.lookup(r["company"])
            if hist["ok"]:
                tracker.db.execute("UPDATE jobs SET sponsorship='likely_sponsors', sponsorship_evidence=? WHERE id=?",
                                   (hist["evidence"], r["id"]))
                tracker.db.commit()
        if (r["score"] or 0) < cfg.get("min_score", 0):
            tracker.set_status(r["id"], "low_score")
            continue
        granted, _ = tracker.claim_slot(r["id"])
        if granted:
            make_resume(dict(r), r["id"], cfg, tracker)
    return len(rows)


HOT_PATH = "data/hiring_companies.json"
HOT_DAYS = 7


def load_hot(path=HOT_PATH) -> dict:
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}


def save_hot(hot: dict, path=HOT_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    json.dump(hot, open(path, "w"))


HOT: dict = {}   # company name -> last time its own board showed a title matching your roles


SKIPPED: set = set()   # URLs already judged outside the US: not stored, but not re-read every cycle either


_ATS_SUFFIX = re.compile(r"\s*\((workday|smartrecruiters|greenhouse|lever|ashby|workable|recruitee|bamboohr|breezy|"
                         r"pinpoint|rippling|oracle|icims|eightfold|careers|careers_page)\)\s*$", re.I)


BOARDS_FILE = "data/boards_read.json"


def load_boards_read() -> set:
    try:
        return set(json.load(open(BOARDS_FILE)))
    except (OSError, ValueError):
        return set()


def save_boards_read(names) -> None:
    """Remember every board read in full, so next time an undated posting there is known to be new."""
    have = load_boards_read()
    if set(names) <= have:
        return
    os.makedirs(os.path.dirname(BOARDS_FILE), exist_ok=True)
    tmp = BOARDS_FILE + ".tmp"
    json.dump(sorted(have | set(names)), open(tmp, "w"))
    os.replace(tmp, BOARDS_FILE)


_NEWS = re.compile(r"\b(joins|joined|announces?|announced|launches|launched|partners? with|partner network|"
                   r"welcomes|acquires|acquired|named|wins|celebrates|press release|webinar|blog|podcast|"
                   r"case study|how (we|to)|introducing|recap)\b", re.I)


def looks_like_news(title: str) -> bool:
    """A news item / blog post that a careers page scraper picked up, not a job title."""
    t = title or ""
    return bool(_NEWS.search(t)) or len(t.split()) > 14 or bool(re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", t))


def clean_company(name: str) -> str:
    """'Aecom2 (smartrecruiters)' -> 'Aecom', 'Allcareers Quanta' -> 'Quanta', 'Pear Vc' -> 'Pear VC'."""
    n = _ATS_SUFFIX.sub("", name or "").strip()
    n = re.sub(r"^(all ?careers|careers|jobs)[\s\-]+", "", n, flags=re.I).strip() or n
    n = re.sub(r"(?<=[A-Za-z]{5})[2-9]$", "", n).strip() or n          # board slugs like 'aecom2'
    n = re.sub(r",?\s+(inc|llc|ltd|corp|corporation|co)\.?$", "", n, flags=re.I).strip() or n
    if n.islower() or re.match(r"^[a-z0-9]+\.(com|io|ai)\b", n):
        n = re.sub(r"\.(com|io|ai)$", "", n, flags=re.I)
        n = n[:1].upper() + n[1:]
    n = re.sub(r"\b(Vc|Ai|Hq|Io)\b", lambda m: m.group(1).upper(), n)
    return n


def clean_title(title: str) -> str:
    return re.sub(r"^\s*(job\s+)?title\s*[:\-]?\s+", "", title or "", flags=re.I).strip() or title


def tidy_listed(tracker: Tracker) -> int:
    """Fix company names / titles already stored and drop copies of the same job (same company and title)."""
    from db import normalize_company
    from links import kind
    rows = tracker.db.execute("SELECT id, company, title, status, url, employer_site FROM jobs WHERE status IN "
                              "('notified','review_queue','capped','location_unclear','applied') ORDER BY first_seen").fetchall()
    # applied first, then copies on the company's own site, so the job-site copy is the one dropped
    rows = sorted(rows, key=lambda r: (r["status"] != "applied",
                                       kind(r["url"], r["company"], r["employer_site"] or "") == "jobsite"))
    keep, dupes = {}, 0
    for r in rows:
        if r["status"] != "applied" and looks_like_news(r["title"]):
            tracker.db.execute("UPDATE jobs SET status='skipped', updated=? WHERE id=?", (time.time(), r["id"]))
            continue
        c, t = clean_company(r["company"]), clean_title(r["title"])
        if (c, t) != (r["company"], r["title"]):
            tracker.db.execute("UPDATE jobs SET company=?, company_norm=?, title=? WHERE id=?",
                               (c, normalize_company(c), t, r["id"]))
        key = (normalize_company(c), t.lower())
        if key in keep and r["status"] != "applied":
            tracker.db.execute("UPDATE jobs SET status='duplicate', updated=? WHERE id=?", (time.time(), r["id"]))
            dupes += 1
        else:
            keep.setdefault(key, r["id"])
    tracker.db.commit()
    return dupes


def run_once(cfg: dict, companies: list[dict], tracker: Tracker, use_aggregators: bool = True, samples=None) -> dict:
    stats = dict(fetched=0, title_ok=0, outside_us=0, location_unclear=0, wrong_level=0, new=0, no_sponsor=0,
                 unclear=0, history_ok=0, low_score=0, capped=0, notified=0)
    loc_cfg = cfg.get("location", {})
    resume_skills = load_profile(cfg.get("master_resume", "profile/master_resume.json"))
    h1b = None
    if cfg["min_sponsorship"] == "sponsors_or_history":
        h1b = H1BHistory.open(cfg.get("h1b_history"))
        if h1b is None:
            print("[warn] H-1B index not found (see h1b_history.py): postings without sponsorship wording stay on hold")
    rescore_stored(tracker, resume_skills)
    refresh_after_master_change(tracker, cfg)
    normalize_dates(tracker)
    recheck_locations(tracker, cfg)
    tidy_listed(tracker)
    hide_other_roles(tracker, cfg)
    demote_old(tracker, cfg)
    release_location_unclear(tracker, cfg)
    release_low_score(tracker, cfg)
    release_unconfirmed(tracker, cfg)
    resolve_existing(tracker, cfg, companies)
    if release_held(tracker, cfg, h1b):
        print("[release] jobs that were on hold for unclear sponsorship are now listed with the new matches")
    candidates = []
    import itertools
    links, link_names = [], {}
    known = tracker.seen_urls() | SKIPPED
    from links import is_recruiter
    flag_unconfirmed = cfg.get("unconfirmed_jobs", "flag") == "flag"   # list real employers' jobs not yet found on their site
    boards = load_boards_read()                       # companies whose board the agent had already read before
    for job in itertools.chain(fetch_all(companies, known.__contains__, lambda t: title_ok(t, cfg)), fetch_aggregators(cfg) if use_aggregators else ()):
        stats["fetched"] += 1
        job["posted_at"] = to_iso(job.get("posted_at"), time.time())
        if not job["posted_at"] and job.get("source") != "aggregator" and job["company"] not in boards:
            job["date_unknown_first_read"] = True     # no date, and already up the first time we read this board
        if job.get("source") != "aggregator" and title_ok(job["title"], cfg):
            HOT[job["company"]] = time.time()          # this company is hiring for your roles: fast lane
        job["company"], job["title"] = clean_company(job["company"]), clean_title(job["title"])
        for l in job.get("links") or ():
            links.append(l)
        if not title_ok(job["title"], cfg) or looks_like_news(job["title"]) or job["url"] in SKIPPED or tracker.seen(job["url"]):
            continue
        if job.get("source") == "aggregator" and tracker.seen_posting(job["company"], job["title"]):
            continue                                   # already found on the company's own board
        stats["title_ok"] += 1
        job["score"], job["matched"], job["gaps"] = score_job(job, resume_skills)   # held jobs get a score too
        if loc_cfg.get("country", "US") == "US":
            where = where_with_description(job.get("location"), job.get("description"))
            if where == "non_us":
                stats["outside_us"] += 1           # not stored: nothing to review
                SKIPPED.add(job["url"])
                if samples is not None and len(samples) < 25:
                    samples.append(f"{job['company']} | {job['title']} | {job.get('location')}")
                continue
            if where == "unclear":
                policy = loc_cfg.get("unclear", "hold")
                if policy == "drop":
                    stats["outside_us"] += 1
                    SKIPPED.add(job["url"])
                    continue
                if policy == "hold":
                    tracker.add(job, status="location_unclear")   # visible in the dashboard to approve
                    stats["location_unclear"] += 1
                    continue
        max_age = cfg.get("max_posting_age_days")
        if job.get("date_unknown_first_read"):
            tracker.add(job, status="too_old")      # no posting date and it was already up: not a new posting
            stats["too_old"] = stats.get("too_old", 0) + 1
            continue
        if not job.get("posted_at") and job.get("source") != "aggregator":
            job["posted_at"] = to_iso(time.time())  # no date given, but it appeared since our last read: new today
        if max_age and job.get("posted_at") and job["posted_at"] < cutoff(max_age):
            tracker.add(job, status="too_old")      # posted before the window: kept for reference, never alerted
            stats["too_old"] = stats.get("too_old", 0) + 1
            continue
        fits, years = level_check(job["description"], cfg)
        if not fits:
            tracker.add(job, status="wrong_level")   # asks for far more / far fewer years than you have
            stats["wrong_level"] += 1
            continue
        verdict, evidence = classify(job["description"])
        job["sponsorship"], job["sponsorship_evidence"] = verdict, evidence
        if verdict == "does_not_sponsor":
            tracker.add(job, status="rejected_sponsorship")
            stats["no_sponsor"] += 1
            continue
        if verdict == "unclear":
            stats["unclear"] += 1
            mode = cfg["min_sponsorship"]
            if mode != "unclear_ok":
                hist = h1b.lookup(job["company"]) if (mode == "sponsors_or_history" and h1b) else None
                if hist and hist["ok"]:
                    job["sponsorship"], job["sponsorship_evidence"] = "likely_sponsors", hist["evidence"]
                    stats["history_ok"] += 1
                else:
                    quote = context_sentence(job["description"])
                    job["sponsorship_evidence"] = (
                        f'Posting says: "{quote}"' if quote else
                        "The posting never mentions visas or sponsorship.")
                    if cfg.get("hold_unclear_sponsorship", False):
                        tracker.add(job, status="needs_sponsorship_check")
                        continue
                    # otherwise listed with the new matches, flagged "Sponsorship unclear", with a resume
        if job["score"] < cfg.get("min_score", 0):
            tracker.add(job, status="low_score")
            stats["low_score"] += 1
            continue
        if job.get("source") == "aggregator" and cfg.get("company_source_only", True):
            from links import kind
            if kind(job["url"], job["company"], job.get("employer_site", "")) != "company":
                from resolver import resolve
                own_url = resolve(job, companies, cfg)
                if own_url and tracker.seen(own_url):
                    continue                               # already listed from the company's own board
                if own_url:
                    job["url"] = own_url                   # the company's own posting
                elif not flag_unconfirmed or is_recruiter(job["company"], job.get("description", "")):
                    tracker.add(job, status="jobsite_only")   # recruiter / job site, or not on the company's site
                    stats["jobsite_only"] = stats.get("jobsite_only", 0) + 1
                    continue
        stats["new"] += 1
        candidates.append(job)

    # Batch by company, best first, so a burst of postings never yields 3 alerts for one employer.
    import fetchers as _f
    if _f.READ_OK:
        save_boards_read(_f.READ_OK.keys())
    candidates.sort(key=lambda j: j["score"], reverse=True)
    for job in candidates:
        job_id = tracker.add(job, status="new")
        if not job_id:
            continue
        granted, bumped = tracker.claim_slot(job_id)
        if granted:
            stats["notified"] += 1
            resume = make_resume(job, job_id, cfg, tracker)
            cover = None
            if resume:
                import glob as _g
                cover = next(iter(_g.glob(os.path.join(os.path.dirname(resume), "*_Cover_Letter.docx"))), None)
            notify(job, bumped, cfg.get("alert_email"), attachment=[resume, cover])
        else:
            stats["capped"] += 1
    if links:
        try:
            from learn import learn
            stats["boards_learned"] = len(learn(links, companies, cfg))
        except Exception as e:  # noqa: BLE001
            print(f"[learn] skipped: {e}")
    return stats


def fast_companies(cfg: dict, companies: list[dict], hot: dict = None, now: float = None) -> list[dict]:
    """Companies on the fast lane (config.yaml: fast_lane.companies).
    "hiring": companies whose own board showed a title matching your roles in the last HOT_DAYS days
    (every company is still checked on each full run, every poll_minutes). "all": every company.
    A list of names: just those."""
    from db import normalize_company
    names = (cfg.get("fast_lane") or {}).get("companies", [])
    if names == "all":
        return list(companies)
    if names == "hiring":
        now = now or time.time()
        hot = HOT if hot is None else hot
        return [c for c in companies if now - hot.get(c["name"], 0) < HOT_DAYS * 86400]
    want = {normalize_company(n) for n in names}
    return [c for c in companies if normalize_company(c["name"]) in want]


def load_companies():
    from learn import merge_into
    companies = yaml.safe_load(open("companies.yaml"))["companies"]
    merge_into(companies)
    try:                                   # big employers with their own careers sites (config.yaml: extra_companies)
        extra = (yaml.safe_load(open("config.yaml")) or {}).get("extra_companies") or []
    except (OSError, ValueError):
        extra = []
    have = {(c["name"], c["ats"]) for c in companies}
    companies += [e for e in extra if (e["name"], e["ats"]) not in have]
    return companies


if __name__ == "__main__":
    load_env()
    cfg = yaml.safe_load(open("config.yaml"))
    companies = load_companies()
    tracker = Tracker("jobs.db", cfg["max_per_company"])
    HOT.update(load_hot())
    once = "--once" in sys.argv
    fast_secs = int((cfg.get("fast_lane") or {}).get("seconds", 60))
    last_full, cycles = 0.0, 0
    while True:
        now = time.time()
        fast = fast_companies(cfg, companies)
        if once or now - last_full >= cfg["poll_minutes"] * 60 or not fast:
            companies = load_companies()               # picks up companies added by verify/find_careers runs
            samples = [] if "--show-skipped" in sys.argv else None
            t0 = time.time()
            stats = run_once(cfg, companies, tracker, samples=samples)
            save_hot(HOT)
            fast = fast_companies(cfg, companies)
            print(time.strftime("%F %T"), f"[full] {len(companies)} companies in {time.time() - t0:.0f}s; "
                  f"{len(fast)} are hiring for your roles (checked every {fast_secs}s).", stats, flush=True)
            if samples:
                print("Skipped as outside the US (sample):")
                print("\n".join("  " + s for s in samples))
            last_full = now
            try:
                import linkcheck
                linkcheck.check_listed(tracker, cfg)       # closed postings / wrong links leave New match
            except Exception as e:  # noqa: BLE001
                print(f"[links] check skipped: {e}", flush=True)
        else:                                          # quick check of the companies hiring for your roles
            t0 = time.time()
            stats = run_once(cfg, fast, tracker, use_aggregators=False)
            cycles += 1
            if cycles % 10 == 0:
                save_hot(HOT)
            print(time.strftime("%F %T"), f"[fast] {len(fast)} companies in {time.time() - t0:.0f}s: "
                  f"{stats['title_ok']} unseen matching titles, {stats['new']} new, {stats['notified']} alerted",
                  flush=True)
        try:
            import digest
            digest.maybe_send(cfg)                      # end-of-day summary, once, after daily_summary.hour
        except Exception as e:  # noqa: BLE001
            print(f"[digest] skipped: {e}")
        if once:
            break
        time.sleep(fast_secs if fast else cfg["poll_minutes"] * 60)
