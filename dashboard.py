"""Local dashboard for the job agent.

    python dashboard.py            ->  open http://127.0.0.1:8000

Run it from the same folder where you run pipeline.py: both read and write jobs.db.

Safety defaults:
  * Listens on 127.0.0.1 only (this computer). It refuses to listen on a network address
    unless DASHBOARD_PASSWORD is set, and you should put HTTPS in front of it if you do.
  * Every form carries a CSRF token, and requests with an unexpected Host header are refused.
  * No JavaScript at all, and a strict Content-Security-Policy.
  * Portal passwords are stored encrypted (see vault.py) and shown only after you unlock the
    vault and press Reveal.
"""
from __future__ import annotations

import csv
import hmac
import io
import json
import re
import os
import secrets
import shutil
import sqlite3
import sys
import time
from datetime import datetime

import yaml
from flask import (Flask, Response, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session)
from werkzeug.utils import secure_filename

from db import ACTIVE, Tracker
from vault import Vault, VaultError

USER_STATUSES = ["notified", "applied", "interview", "offer", "rejected_by_employer", "withdrawn", "skipped"]
LABELS = {
    "notified": "New match",
    "applied": "Applied",
    "interview": "Interview",
    "offer": "Offer",
    "rejected_by_employer": "Rejected by employer",
    "withdrawn": "Withdrawn",
    "skipped": "Skipped",
    "needs_sponsorship_check": "On hold: sponsorship unclear",
    "wrong_level": "Experience level doesn't fit",
    "location_unclear": "Location unclear (not clearly US)",
    "low_score": "Low match score",
    "capped": "Over the 2-per-company limit",
    "bumped": "Replaced by a better match",
    "rejected_sponsorship": "Rules out sponsorship",
    "review_queue": "Needs review",
    "new": "Unprocessed",
    "too_old": "Older posting",
    "other_role": "Not a target role",
    "jobsite_only": "Only on job sites",
    "outside_us": "Outside the US",
    "posting_closed": "Posting closed",
    "link_wrong": "Link opens a different job",
    "duplicate": "Duplicate",
}
RESUME_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt"}


def _load_or_create_secret(path: str) -> str:
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    secret = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret)
    return secret


def _csv_cell(v) -> str:
    """Stop spreadsheet apps from running text like '=HYPERLINK(...)' that came from a job posting."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def create_app(db_path="jobs.db", data_dir="data", cfg_path="config.yaml", allowed_hosts=None):
    app = Flask(__name__)
    os.makedirs(data_dir, exist_ok=True)
    cfg = {}
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
    max_per_company = cfg.get("max_per_company", 2)
    resume_file = cfg.get("resume_file", "profile/resume.docx")

    Tracker(db_path, max_per_company).db.close()  # creates any missing tables
    app.config.update(
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_HTTPONLY=True,
    )
    app.secret_key = _load_or_create_secret(os.path.join(data_dir, "secret_key"))
    vault = Vault()
    hosts = set(h.strip() for h in (allowed_hosts or os.environ.get(
        "DASHBOARD_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")))

    # ------------------------------------------------------------ plumbing
    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(db_path, timeout=30)
            g.db.row_factory = sqlite3.Row
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]

    app.jinja_env.globals.update(csrf_token=csrf_token, labels=LABELS, user_statuses=USER_STATUSES,
                                 max_per_company=max_per_company)
    app.jinja_env.filters["fdate"] = lambda ts: (
        datetime.fromtimestamp(ts).strftime("%b %d, %Y") if ts else "")
    from dates import describe as _describe_date
    app.jinja_env.filters["posted"] = _describe_date
    import links as _links
    from locations import where_with_description as _where
    app.jinja_env.globals["loc_unclear"] = lambda loc, desc=None: _where(loc, desc) == "unclear"
    app.jinja_env.globals["open_url"] = lambda j: _links.open_url(j["url"], j["company"], j["title"],
                                                                   (j["employer_site"] if "employer_site" in j.keys() else "") or "")
    app.jinja_env.globals["unconfirmed"] = lambda j: _links.kind(
        j["url"], j["company"], (j["employer_site"] if "employer_site" in j.keys() else "") or "") == "jobsite"
    app.jinja_env.globals.update(link_kind=_links.kind, link_host=_links.host, careers_search=_links.careers_search)
    app.jinja_env.filters["fromjson"] = lambda t: (json.loads(t) if t else None)
    app.jinja_env.filters["label"] = lambda s: LABELS.get(s, s)
    # Only http(s) links become clickable; a 'javascript:' URL from a feed is never rendered as a link.
    app.jinja_env.filters["safe_url"] = lambda u: u if isinstance(u, str) and u.startswith(("http://", "https://")) else None

    @app.before_request
    def guard():
        if request.host.split(":")[0] not in hosts:
            abort(400)
        password = os.environ.get("DASHBOARD_PASSWORD")
        if password:
            auth = request.authorization
            if not auth or not hmac.compare_digest((auth.password or "").encode(), password.encode()):
                return Response("Sign in required.", 401, {"WWW-Authenticate": 'Basic realm="Job dashboard"'})
        if request.path.startswith("/api/"):
            # The browser extension: same password, plus a custom header that other websites cannot send.
            if request.headers.get("X-JobAgent") != "1":
                abort(403)
            return None
        if request.method == "POST":
            sent = request.form.get("csrf", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(403)

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def safe_next(default: str) -> str:
        nxt = request.form.get("next") or request.args.get("next") or ""
        return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else default

    def job_or_404(job_id: int):
        row = get_db().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            abort(404)
        return row

    def mark_applied(job_id: int, db=None):
        """Status 'applied' and the date it was sent."""
        db = db or get_db()
        db.execute("UPDATE jobs SET status='applied', updated=? WHERE id=?", (time.time(), job_id))
        db.execute(
            "INSERT INTO applications(job_id, applied_at, updated) VALUES (?,?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET applied_at=COALESCE(applications.applied_at, excluded.applied_at), "
            "updated=excluded.updated", (job_id, time.time(), time.time()))
        db.commit()

    def active_count(company_norm: str) -> int:
        marks = ",".join("?" * len(ACTIVE))
        return get_db().execute(
            f"SELECT COUNT(*) FROM jobs WHERE company_norm=? AND status IN ({marks})",
            (company_norm, *ACTIVE)).fetchone()[0]

    def resume_dir(job_id: int) -> str:
        return os.path.join(data_dir, "resumes", str(job_id))

    # ------------------------------------------------------------ today's best matches
    def ranked(hours=24, limit=25):
        from rank import priority
        db = get_db()
        marks = ",".join("?" * 2)
        sql = f"SELECT * FROM jobs WHERE status IN ({marks})"
        params = ["notified", "review_queue"]
        if hours:
            sql += " AND first_seen >= ?"
            params.append(time.time() - hours * 3600)
        rows = []
        for r in db.execute(sql, params).fetchall():
            p, why = priority(r)
            rows.append((p, why, r))
        rows.sort(key=lambda x: -x[0])
        return rows[:limit]

    @app.get("/today")
    def today():
        """Jobs the companies POSTED today/yesterday (by their own posting date), best first, no 25 cap."""
        from datetime import timedelta, timezone as _tz
        from rank import priority
        win = request.args.get("when", "2d")
        win = win if win in ("today", "yesterday", "2d", "3d") else "2d"
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        start = {"today": midnight, "yesterday": midnight - timedelta(days=1), "2d": midnight - timedelta(days=1),
                 "3d": midnight - timedelta(days=2)}[win]
        end = midnight if win == "yesterday" else midnight + timedelta(days=1)
        iso = lambda dt: dt.astimezone(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        db = get_db()
        rows = []
        for r in db.execute("SELECT * FROM jobs WHERE status IN ('notified','review_queue') "
                            "AND posted_at >= ? AND posted_at < ?", (iso(start), iso(end))).fetchall():
            p, why = priority(r)
            rows.append((p, why, r))
        rows.sort(key=lambda x: -x[0])
        undated = db.execute("SELECT * FROM jobs WHERE status IN ('notified','review_queue') AND posted_at IS NULL "
                             "AND first_seen >= ? ORDER BY score DESC LIMIT 100", (start.timestamp(),)).fetchall()
        return render_template("today.html", rows=rows[:500], when=win, total=len(rows), undated=undated)

    @app.get("/insights")
    def insights():
        """Where the openings are, which keywords you miss most, and how your applications are going."""
        from collections import Counter
        from dates import cutoff
        db = get_db()
        counts = {r["status"]: r["n"] for r in db.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
        found = sum(n for s, n in counts.items() if s != "other_role")
        funnel = [("Target-role postings found", found),
                  ("New matches (passed every filter)", sum(counts.get(s, 0) for s in
                                                            ("notified", "applied", "interview", "offer",
                                                             "rejected_by_employer", "withdrawn"))),
                  ("Applied", sum(counts.get(s, 0) for s in ("applied", "interview", "offer", "rejected_by_employer"))),
                  ("Interviews", counts.get("interview", 0) + counts.get("offer", 0)),
                  ("Offers", counts.get("offer", 0))]
        since = cutoff(30)
        companies = db.execute(
            "SELECT MIN(company) company, COUNT(*) n, MAX(COALESCE(posted_at, '')) last FROM jobs "
            "WHERE status != 'other_role' AND COALESCE(posted_at, strftime('%Y-%m-%dT%H:%M:%SZ', first_seen, "
            "'unixepoch')) >= ? GROUP BY company_norm ORDER BY n DESC, last DESC LIMIT 20", (since,)).fetchall()
        gaps = Counter()
        n_match = 0
        for r in db.execute("SELECT gaps FROM jobs WHERE status IN ('notified','applied','interview','offer')"):
            n_match += 1
            gaps.update(s for s in (r["gaps"] or "").split(", ") if s)
        days = db.execute(
            "SELECT substr(posted_at, 1, 10) d, COUNT(*) n FROM jobs WHERE status != 'other_role' AND posted_at >= ? "
            "GROUP BY d ORDER BY d DESC", (cutoff(14),)).fetchall()
        spons = Counter(r["sponsorship"] or "unclear" for r in
                        db.execute("SELECT sponsorship FROM jobs WHERE status IN ('notified','applied','interview','offer')"))
        return render_template("insights.html", funnel=funnel, companies=companies, gaps=gaps.most_common(15),
                               n_match=n_match, days=days, max_day=max([r["n"] for r in days] or [1]),
                               spons=spons)

    # ------------------------------------------------------------ API for the browser extension
    def _norm_url(u: str) -> str:
        from urllib.parse import urlparse
        p = urlparse(u or "")
        path = re.sub(r"/(apply|application|applications|login|signin)/?$", "", (p.path or "").rstrip("/"), flags=re.I)
        return f"{(p.hostname or '').lower().removeprefix('www.')}{path.lower()}"

    def match_job(tab_url: str):
        """The job whose posting is open in the browser tab (None if unsure)."""
        from urllib.parse import urlparse
        t = _norm_url(tab_url)
        th = (urlparse(tab_url or "").hostname or "").lower().removeprefix("www.")
        frag = urlparse(tab_url or "").fragment or ""
        best, best_score = None, 0
        for r in get_db().execute("SELECT * FROM jobs WHERE status IN ('notified','review_queue','applied',"
                                  "'interview','needs_sponsorship_check','location_unclear','capped','too_old',"
                                  "'jobsite_only') ORDER BY first_seen DESC LIMIT 5000"):
            j = _norm_url(r["url"])
            jh = j.split("/")[0]
            score = 0
            if j and (t == j or t.startswith(j + "/") or j.startswith(t + "/")):
                score = 3
            elif jh == th:
                ids = re.findall(r"\d{4,}|[0-9a-f]{8}-[0-9a-f-]{27}", (r["url"] or "").lower())
                if ids and any(i in (tab_url or "").lower() for i in ids):
                    score = 2
            if frag and len(frag) > 3 and r["url"] and frag in r["url"]:
                score = max(score, 2)
            if score > best_score:
                best, best_score = r, score
        return best

    def _b64(path):
        import base64
        return base64.b64encode(open(path, "rb").read()).decode() if path and os.path.exists(path) else None

    @app.get("/api/jobs/recent")
    def api_recent():
        rows = get_db().execute("SELECT id, title, company, url, status FROM jobs WHERE status IN "
                                "('notified','review_queue','applied') ORDER BY (status='applied'), first_seen DESC "
                                "LIMIT 60").fetchall()
        return {"jobs": [dict(r) for r in rows]}

    @app.get("/api/autofill")
    def api_autofill():
        """Everything the extension needs to fill an application form for one job."""
        import answers
        job = None
        if request.args.get("id", "").isdigit():
            job = get_db().execute("SELECT * FROM jobs WHERE id=?", (int(request.args["id"]),)).fetchone()
        elif request.args.get("url"):
            job = match_job(request.args["url"])
        if job is None:
            return {"job": None}
        jid = job["id"]
        folder = resume_dir(jid)
        st = tailored_struct(jid, job)               # builds the tailored resume + cover letter if missing
        if not job["resume_file"]:
            from tailor import tailor
            path, res = tailor(dict(job), jid, master_path, data_dir)
            tr = Tracker(db_path, max_per_company)
            try:
                tr.set_ats(jid, res, os.path.basename(path))
            finally:
                tr.db.close()
            st = tailored_struct(jid, job)
        job = job_or_404(jid)
        std = answers.load_standard(st)
        stdd = {q: a for q, a in std}
        name = (st.get("name") or "").split()
        contact = [x.strip() for x in (st.get("contact") or "").split("|") if x.strip()]
        city = stdd.get("Current location") or next((x for x in contact if "@" not in x and not re.search(r"\d{3}", x)), "")
        cur = (st.get("experience") or [{}])[0]
        profile = dict(
            full_name=st.get("name", ""), first_name=name[0] if name else "", last_name=name[-1] if len(name) > 1 else "",
            email=stdd.get("Email", ""), phone=stdd.get("Phone", ""), location=city,
            city=city.split(",")[0].strip(), state=(city.split(",")[1].strip() if "," in city else ""),
            country="United States", linkedin=stdd.get("LinkedIn profile URL", ""),
            github=stdd.get("GitHub / portfolio URL", ""), website=stdd.get("Personal website", ""),
            current_company=cur.get("company", ""), current_title=cur.get("title", ""),
        )
        cover_txt = os.path.join(folder, "cover_letter.txt")
        files = os.listdir(folder) if os.path.isdir(folder) else []
        cover_file = next((f for f in files if f.endswith("_Cover_Letter.docx")), None)
        resume_path = os.path.join(folder, job["resume_file"]) if job["resume_file"] else None
        return dict(
            job=dict(id=jid, title=job["title"], company=job["company"], url=job["url"], status=job["status"]),
            profile=profile, standard=std,
            job_answers=answers.load_job(folder, st, dict(job)),
            cover_letter_text=open(cover_txt).read() if os.path.exists(cover_txt) else "",
            resume=dict(name=job["resume_file"], b64=_b64(resume_path)) if resume_path else None,
            cover=dict(name=cover_file, b64=_b64(os.path.join(folder, cover_file))) if cover_file else None,
        )

    @app.post("/api/jobs/<int:job_id>/applied")
    def api_applied(job_id):
        job_or_404(job_id)
        mark_applied(job_id)
        return {"ok": True}

    # ------------------------------------------------------------ overview
    @app.get("/")
    def overview():
        db = get_db()
        counts = {r["status"]: r["n"] for r in db.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
        attention = db.execute(
            "SELECT * FROM jobs WHERE status='notified' ORDER BY score DESC, first_seen DESC LIMIT 10").fetchall()
        marks = ",".join("?" * len(ACTIVE))
        at_cap = db.execute(
            f"SELECT MIN(company) company, COUNT(*) n FROM jobs WHERE status IN ({marks}) "
            f"GROUP BY company_norm HAVING n >= ? ORDER BY company", (*ACTIVE, max_per_company)).fetchall()
        recent = db.execute(
            "SELECT j.id, j.company, j.title, a.applied_at FROM applications a JOIN jobs j ON j.id=a.job_id "
            "WHERE a.applied_at IS NOT NULL ORDER BY a.applied_at DESC LIMIT 5").fetchall()
        due = db.execute(
            "SELECT j.id, j.company, j.title, a.follow_up FROM applications a JOIN jobs j ON j.id=a.job_id "
            "WHERE a.follow_up IS NOT NULL AND a.follow_up != '' AND j.status IN ('applied','interview') "
            "ORDER BY a.follow_up LIMIT 5").fetchall()
        from dates import cutoff
        fresh = db.execute(
            "SELECT * FROM jobs WHERE status IN ('notified','review_queue') AND posted_at >= ? "
            "ORDER BY posted_at DESC LIMIT 15", (cutoff(1),)).fetchall()
        return render_template("overview.html", top=ranked(0, 10), fresh=fresh, counts=counts, attention=attention, at_cap=at_cap,
                               recent=recent, due=due, today=datetime.now().strftime("%Y-%m-%d"))

    # ------------------------------------------------------------ jobs
    @app.get("/jobs")
    def jobs():
        db = get_db()
        status = request.args.get("status", "notified")
        q = request.args.get("q", "").strip()
        sort = request.args.get("sort", "score")
        try:
            min_score = float(request.args.get("min_score") or 0)
        except ValueError:
            min_score = 0.0
        where, params = [], []
        if status != "all":
            where.append("j.status=?")
            params.append(status)
        if q:
            where.append("(j.company LIKE ? OR j.title LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if min_score:
            where.append("COALESCE(j.score,0) >= ?")
            params.append(min_score)
        order = {"score": "j.score DESC, j.first_seen DESC", "newest": "j.first_seen DESC",
                 "posted": "COALESCE(j.posted_at, strftime('%Y-%m-%dT%H:%M:%SZ', j.first_seen, 'unixepoch')) DESC",
                 "company": "j.company_norm ASC, j.score DESC"}.get(sort, "j.score DESC")
        sql = ("SELECT j.*, a.applied_at FROM jobs j LEFT JOIN applications a ON a.job_id=j.id "
               + ("WHERE " + " AND ".join(where) if where else "") + f" ORDER BY {order} LIMIT 300")
        rows = db.execute(sql, params).fetchall()
        counts = {r["status"]: r["n"] for r in db.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
        return render_template("jobs.html", rows=rows, counts=counts, status=status, q=q, sort=sort,
                               min_score=min_score or "")

    @app.get("/jobs/<int:job_id>")
    def job_detail(job_id):
        db = get_db()
        job = job_or_404(job_id)
        app_row = db.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone()
        folder = resume_dir(job_id)
        files = sorted(os.listdir(folder), reverse=True) if os.path.isdir(folder) else []
        portals = db.execute("SELECT id, name FROM portals ORDER BY name").fetchall()
        cover = next((f for f in files if f.endswith("_Cover_Letter.docx")), None)
        try:
            added = json.load(open(os.path.join(folder, "tailored.json"))).get("added_keywords", [])
        except (OSError, ValueError):
            added = []
        return render_template("job.html", job=job, app_row=app_row, files=files, portals=portals,
                               used=active_count(job["company_norm"]), cover=cover, added=added)

    @app.route("/jobs/<int:job_id>/cover/edit", methods=["GET", "POST"])
    def edit_cover(job_id):
        import cover_letter
        job = job_or_404(job_id)
        folder = resume_dir(job_id)
        js = os.path.join(folder, "tailored.json")
        if not os.path.exists(js):
            flash("Make the tailored resume first; the cover letter is built from it.", "error")
            return redirect(f"/jobs/{job_id}")
        resume = json.load(open(js))
        txt = os.path.join(folder, "cover_letter.txt")
        if request.method == "POST":
            if request.form.get("action") == "regenerate":
                cover_letter.make(resume, dict(job), folder)
                flash("Cover letter rebuilt from your resume.", "ok")
            else:
                body = request.form.get("body", "").replace("\r\n", "\n").strip()
                if not body:
                    flash("The letter is empty.", "error")
                    return redirect(f"/jobs/{job_id}/cover/edit")
                cover_letter.make(resume, dict(job), folder, body)
                flash("Cover letter saved.", "ok")
            return redirect(f"/jobs/{job_id}/cover/edit")
        if not os.path.exists(txt):
            cover_letter.make(resume, dict(job), folder)
        body = open(txt).read()
        return render_template("edit_cover.html", job=job, body=body,
                               file=cover_letter.file_name(resume, dict(job)))

    # ------------------------------------------------------------ master resume + instant tailoring
    master_path = cfg.get("master_resume", "profile/master_resume.json")

    def load_master():
        try:
            return json.load(open(master_path))
        except (OSError, ValueError):
            return None

    @app.get("/resume")
    def resume_page():
        m = load_master()
        info = None
        if m:
            info = dict(name=m["name"], jobs=len(m["experience"]),
                        bullets=sum(len(j["bullets"]) for j in m["experience"]),
                        skills=sum(len(v) for v in m["skills"].values()), groups=list(m["skills"]),
                        experience=[(j["company"], j["title"], j["start"], j["end"], len(j["bullets"]))
                                    for j in m["experience"]],
                        updated=os.path.getmtime(master_path), has_docx=os.path.exists(resume_file))
        return render_template("resume.html", info=info)

    @app.post("/resume/upload")
    def resume_upload():
        f = request.files.get("resume")
        if not f or not f.filename.lower().endswith(".docx"):
            flash("Choose your resume as a Word (.docx) file.", "error")
            return redirect("/resume")
        import tempfile
        from build_master_resume import build
        with tempfile.TemporaryDirectory() as tmp:
            src, out = os.path.join(tmp, "resume.docx"), os.path.join(tmp, "master.json")
            f.save(src)
            try:
                data = build(src, out)
            except Exception as e:  # noqa: BLE001
                flash(f"Could not read that resume, nothing was changed. {e}", "error")
                return redirect("/resume")
            os.makedirs(os.path.dirname(resume_file) or ".", exist_ok=True)
            if os.path.exists(resume_file):                      # keep the previous one
                bdir = os.path.join(os.path.dirname(resume_file) or ".", "backups")
                os.makedirs(bdir, exist_ok=True)
                shutil.copyfile(resume_file, os.path.join(bdir, f"{time.strftime('%Y%m%d-%H%M%S')}_resume.docx"))
            shutil.copyfile(src, resume_file)
            shutil.copyfile(out, master_path)
        flash(f"Master resume saved: {len(data['experience'])} jobs, "
              f"{sum(len(j['bullets']) for j in data['experience'])} bullets, "
              f"{sum(len(v) for v in data['skills'].values())} skills, {len(data.get('projects') or [])} projects. "
              "The agent is now rebuilding the resumes, cover letters and match scores of your current matches "
              "(jobs you already applied to keep the resume you sent). This takes a few minutes.", "ok")
        return redirect("/resume")

    @app.get("/resume/master.docx")
    def resume_download():
        if not os.path.exists(resume_file):
            abort(404)
        return send_from_directory(os.path.abspath(os.path.dirname(resume_file) or "."),
                                   os.path.basename(resume_file), as_attachment=True)

    @app.route("/tailor", methods=["GET", "POST"])
    def tailor_now():
        result = None
        form = dict(title="", company="", description="")
        if request.method == "POST":
            form = {k: request.form.get(k, "").strip() for k in form}
            if len(form["description"]) < 40:
                flash("Paste the job description first.", "error")
            else:
                try:
                    from ats import report_text  # noqa: F401
                    from tailor import tailor
                    stamp = f"adhoc-{int(time.time())}"
                    job = dict(title=form["title"] or "Software Engineer", company=form["company"] or "Company",
                               description=form["description"])
                    path, res = tailor(job, stamp, master_path, data_dir)
                    from scoring import load_profile, score_job
                    score, matched, gaps = score_job(job, load_profile(master_path))
                    result = dict(res=res, folder=stamp, file=os.path.basename(path), match=score, matched=matched,
                                  gaps=gaps)
                except Exception as e:  # noqa: BLE001
                    flash(f"Could not build the resume: {e}", "error")
        return render_template("tailor.html", form=form, result=result, has_master=load_master() is not None)

    @app.get("/adhoc/<folder>/<name>")
    def adhoc_download(folder, name):
        import re as _re
        if not _re.fullmatch(r"adhoc-\d+", folder):
            abort(404)
        return send_from_directory(os.path.abspath(os.path.join(data_dir, "resumes", folder)), name,
                                   as_attachment=True)

    # ------------------------------------------------------------ edit the tailored resume
    def tailored_struct(job_id, job):
        path = os.path.join(resume_dir(job_id), "tailored.json")
        if not os.path.exists(path):
            from tailor import tailor
            tailor(dict(job), job_id, master_path, data_dir)
        return json.load(open(path))

    def save_results(job_id, job, st, path, res):
        """After any rebuild: store the ATS check and re-score the match against THIS job's resume."""
        import suggest
        from scoring import find_skills, score_job
        score, matched, gaps = score_job(dict(job), set(find_skills(suggest._text(st))))
        tr = Tracker(db_path, max_per_company)
        try:
            tr.set_ats(job_id, res, os.path.basename(path))
            tr.set_match(job_id, score, matched, gaps)
        finally:
            tr.db.close()
        return score

    @app.get("/jobs/<int:job_id>/resume/edit")
    def edit_resume(job_id):
        job = job_or_404(job_id)
        try:
            st = tailored_struct(job_id, job)
        except Exception as e:  # noqa: BLE001
            flash(f"Could not open the resume: {e}", "error")
            return redirect(f"/jobs/{job_id}")
        lines = lambda txt: max(4, txt.count("\n") + 2)
        skills_text = "\n".join(f"{g}: {', '.join(v)}" for g, v in st["skills"].items())
        import suggest
        return render_template("edit_resume.html", job=job, st=st, skills_text=skills_text,
                               edu_text="\n".join(st["education"]), lines=lines,
                               report=(job["ats_report"] and json.loads(job["ats_report"])),
                               drafts=suggest.drafts(st, dict(job)))

    @app.route("/jobs/<int:job_id>/answers", methods=["GET", "POST"])
    def job_answers_page(job_id):
        """Ready-to-paste answers for application forms: standard ones (all jobs) + ones for this posting."""
        import answers
        job = job_or_404(job_id)
        st = tailored_struct(job_id, job)
        folder = resume_dir(job_id)
        if request.method == "POST":
            f = request.form
            if f.get("action") == "rebuild":
                p = os.path.join(folder, "answers.json")
                if os.path.exists(p):
                    os.remove(p)
                flash("Answers for this job rebuilt from your resume.", "ok")
            else:
                std = [[f.get(f"sq{i}", "").strip(), f.get(f"sa{i}", "").strip()] for i in range(int(f.get("n_std", 0)))]
                answers.save_standard([x for x in std if x[0]])
                jb = [[f.get(f"jq{i}", "").strip(), f.get(f"ja{i}", "").strip()] for i in range(int(f.get("n_job", 0)))]
                answers.save_job(folder, [x for x in jb if x[0]])
                flash("Answers saved. Standard answers apply to every job.", "ok")
            return redirect(f"/jobs/{job_id}/answers")
        return render_template("answers.html", job=job, std=answers.load_standard(st),
                               jobq=answers.load_job(folder, st, dict(job)))

    @app.post("/jobs/<int:job_id>/resume/suggest")
    def add_suggestions(job_id):
        """Put the ticked draft skills/bullets into THIS job's tailored resume (never the master)."""
        import suggest
        from tailor import rebuild
        job = job_or_404(job_id)
        st = tailored_struct(job_id, job)
        skills = [s for s in request.form.getlist("skill") if s in suggest.DRAFTS]
        bullets = []
        for d in suggest.drafts(st, dict(job)):
            for i, b in enumerate(d["bullets"]):
                if request.form.get(f"bullet_{d['skill']}_{i}"):
                    bullets.append((d["skill"], b))
        if not skills and not bullets:
            flash("Tick at least one skill or bullet to add.", "error")
            return redirect(f"/jobs/{job_id}/resume/edit")
        before = job["score"] or 0
        suggest.apply(st, skills, bullets)
        st["added_keywords"] = sorted(set(st.get("added_keywords", [])) | set(skills) | {s for s, _ in bullets})
        path, res = rebuild(st, dict(job), job_id, master_path, data_dir)
        score = save_results(job_id, job, st, path, res)
        flash(f"Added to this job's resume and saved: {', '.join(st['added_keywords'])}. "
              f"Match {before:.0f} → {score:.0f} · ATS match {res['score']:.0f}/100. Review the wording below.", "ok")
        return redirect(f"/jobs/{job_id}/resume/edit")

    @app.post("/jobs/<int:job_id>/resume/edit")
    def save_resume(job_id):
        job = job_or_404(job_id)
        f = request.form
        try:
            from build_master_resume import split_top_level
            from tailor import rebuild
            st = tailored_struct(job_id, job)
            st["name"] = f.get("name", st["name"]).strip() or st["name"]
            st["contact"] = f.get("contact", "").strip()
            st["summary"] = " ".join(f.get("summary", "").split())
            skills = {}
            for line in f.get("skills", "").splitlines():
                if ":" in line:
                    g, items = line.split(":", 1)
                    if g.strip():
                        skills[g.strip()] = split_top_level(items)
            st["skills"] = skills
            for i, j in enumerate(st["experience"]):
                for k in ("title", "company", "location", "start", "end"):
                    j[k] = f.get(f"job{i}_{k}", j[k]).strip()
                j["bullets"] = [b.strip() for b in f.get(f"job{i}_bullets", "").splitlines() if b.strip()]
            st["education"] = [l.strip() for l in f.get("education", "").splitlines() if l.strip()]
            path, res = rebuild(st, dict(job), job_id, master_path, data_dir)
            score = save_results(job_id, job, st, path, res)
            flash(f"Saved. Your resume was rebuilt. Match {score:.0f} · ATS match {res['score']:.0f}/100.", "ok")
        except Exception as e:  # noqa: BLE001
            flash(f"Could not save: {e}", "error")
        return redirect(f"/jobs/{job_id}/resume/edit")

    @app.post("/jobs/<int:job_id>/resume/tailor")
    def tailor_resume(job_id):
        job = job_or_404(job_id)
        try:
            from tailor import tailor
            path, res = tailor(dict(job), job_id, cfg.get("master_resume", "profile/master_resume.json"), data_dir)
            save_results(job_id, job, json.load(open(os.path.join(resume_dir(job_id), "tailored.json"))), path, res)
            flash(f"Tailored resume ready. ATS match: {res['score']:.0f}/100.", "ok")
        except Exception as e:  # noqa: BLE001
            flash(f"Could not build the resume: {e}", "error")
            return redirect(f"/jobs/{job_id}")
        return redirect(f"/jobs/{job_id}/resume/edit" if request.form.get("next") == "edit" else f"/jobs/{job_id}")

    @app.post("/jobs/<int:job_id>/status")
    def set_status(job_id):
        db = get_db()
        job = job_or_404(job_id)
        new = request.form.get("status", "")
        back = safe_next(f"/jobs/{job_id}")
        if new not in USER_STATUSES:
            flash("Pick a status first.", "error")
            return redirect(back)
        if new == "notified" and job["status"] not in ACTIVE:
            # Approving a held / skipped job: it must win a slot under the per-company limit.
            tracker = Tracker(db_path, max_per_company)
            try:
                granted, bumped = tracker.claim_slot(job_id)
            finally:
                tracker.db.close()
            if granted:
                msg = "Approved. It now counts toward the company limit."
                if bumped:
                    msg += f" It replaced a lower-scoring match (job #{bumped})."
                try:                                   # approved jobs get their tailored resume right away
                    from tailor import tailor
                    path, res = tailor(dict(job), job_id, cfg.get("master_resume", "profile/master_resume.json"), data_dir)
                    tr = Tracker(db_path, max_per_company)
                    try:
                        tr.set_ats(job_id, res, os.path.basename(path))
                    finally:
                        tr.db.close()
                    msg += f" Tailored resume made (ATS match {res['score']:.0f}/100)."
                except Exception:  # noqa: BLE001
                    pass
                flash(msg, "ok")
            else:
                flash(f"Not approved: {job['company']} already has {max_per_company} positions in progress "
                      "and this one scores lower.", "error")
            return redirect(back)
        db.execute("UPDATE jobs SET status=?, updated=? WHERE id=?", (new, time.time(), job_id))
        if new == "applied":
            mark_applied(job_id, db)
        db.commit()
        if new in ACTIVE and active_count(job["company_norm"]) > max_per_company:
            flash(f"Heads-up: you now have more than {max_per_company} positions in progress at {job['company']}.",
                  "warn")
        else:
            flash(f"Marked as: {LABELS[new]}.", "ok")
        return redirect(back)

    @app.post("/jobs/<int:job_id>/notes")
    def save_notes(job_id):
        db = get_db()
        job_or_404(job_id)
        follow_up = request.form.get("follow_up", "").strip()
        if follow_up:
            try:
                datetime.strptime(follow_up, "%Y-%m-%d")
            except ValueError:
                flash("Follow-up date must look like 2026-11-15.", "error")
                return redirect(f"/jobs/{job_id}")
        portal_id = request.form.get("portal_id", "").strip()
        portal_id = int(portal_id) if portal_id.isdigit() and db.execute(
            "SELECT 1 FROM portals WHERE id=?", (int(portal_id),)).fetchone() else None
        db.execute(
            "INSERT INTO applications(job_id, notes, follow_up, portal_id, updated) VALUES (?,?,?,?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET notes=excluded.notes, follow_up=excluded.follow_up, "
            "portal_id=excluded.portal_id, updated=excluded.updated",
            (job_id, request.form.get("notes", "")[:5000], follow_up, portal_id, time.time()))
        db.commit()
        flash("Saved.", "ok")
        return redirect(f"/jobs/{job_id}")

    # ------------------------------------------------------------ resumes per application
    @app.post("/jobs/<int:job_id>/resume")
    def upload_resume(job_id):
        job_or_404(job_id)
        f = request.files.get("resume")
        name = secure_filename(f.filename) if f and f.filename else ""
        if not name or os.path.splitext(name)[1].lower() not in RESUME_EXTENSIONS:
            flash("Upload a .pdf, .docx, .doc or .txt file.", "error")
            return redirect(f"/jobs/{job_id}")
        os.makedirs(resume_dir(job_id), exist_ok=True)
        f.save(os.path.join(resume_dir(job_id), f"{time.strftime('%Y%m%d-%H%M%S')}_{name}"))
        flash("Resume saved for this application.", "ok")
        return redirect(f"/jobs/{job_id}")

    @app.post("/jobs/<int:job_id>/resume/current")
    def attach_current_resume(job_id):
        job_or_404(job_id)
        if not os.path.exists(resume_file):
            flash(f"Could not find {resume_file}.", "error")
            return redirect(f"/jobs/{job_id}")
        os.makedirs(resume_dir(job_id), exist_ok=True)
        ext = os.path.splitext(resume_file)[1]
        shutil.copyfile(resume_file, os.path.join(resume_dir(job_id), f"{time.strftime('%Y%m%d-%H%M%S')}_Resume{ext}"))
        flash("Copied your current resume to this application.", "ok")
        return redirect(f"/jobs/{job_id}")

    @app.get("/files/<int:job_id>/<path:name>")
    def download(job_id, name):
        job_or_404(job_id)
        return send_from_directory(os.path.abspath(resume_dir(job_id)), name, as_attachment=True)

    # ------------------------------------------------------------ portals + vault
    @app.get("/portals")
    def portals():
        db = get_db()
        rows = db.execute("SELECT id, name, url, username, notes, (password_enc IS NOT NULL) has_pw "
                          "FROM portals ORDER BY name").fetchall()
        vault.touch()
        return render_template("portals.html", rows=rows, initialized=vault.initialized(db),
                               unlocked=vault.is_unlocked())

    @app.post("/vault/setup")
    def vault_setup():
        try:
            vault.setup(get_db(), request.form.get("master", ""), request.form.get("confirm", ""))
            flash("Vault created and unlocked. Do not lose this master password: it cannot be recovered.", "ok")
        except VaultError as e:
            flash(str(e), "error")
        return redirect("/portals")

    @app.post("/vault/unlock")
    def vault_unlock():
        try:
            if vault.unlock(get_db(), request.form.get("master", "")):
                flash("Vault unlocked for 15 minutes of inactivity.", "ok")
            else:
                flash("Wrong master password.", "error")
        except VaultError as e:
            flash(str(e), "error")
        return redirect("/portals")

    @app.post("/vault/lock")
    def vault_lock():
        vault.lock()
        flash("Vault locked.", "ok")
        return redirect("/portals")

    @app.post("/portals/add")
    def portal_add():
        name = request.form.get("name", "").strip()
        url = request.form.get("url", "").strip()
        password = request.form.get("password", "")
        if not name:
            flash("Give the portal a name, e.g. 'Workday - Acme'.", "error")
            return redirect("/portals")
        if url and not url.startswith(("http://", "https://")):
            flash("The link must start with http:// or https://", "error")
            return redirect("/portals")
        try:
            token = vault.encrypt(password) if password else None
        except VaultError as e:
            flash(str(e), "error")
            return redirect("/portals")
        db = get_db()
        db.execute("INSERT INTO portals(name, url, username, password_enc, notes, created) VALUES (?,?,?,?,?,?)",
                   (name, url, request.form.get("username", "").strip(), token,
                    request.form.get("notes", "")[:2000], time.time()))
        db.commit()
        vault.touch()
        flash(f"Saved {name}.", "ok")
        return redirect("/portals")

    @app.post("/portals/<int:portal_id>/reveal")
    def portal_reveal(portal_id):
        row = get_db().execute("SELECT * FROM portals WHERE id=?", (portal_id,)).fetchone()
        if row is None:
            abort(404)
        try:
            password = vault.decrypt(row["password_enc"]) if row["password_enc"] else ""
        except VaultError as e:
            flash(str(e), "error")
            return redirect("/portals")
        vault.touch()
        return render_template("reveal.html", portal=row, password=password)

    @app.post("/portals/<int:portal_id>/delete")
    def portal_delete(portal_id):
        db = get_db()
        db.execute("DELETE FROM portals WHERE id=?", (portal_id,))
        db.execute("UPDATE applications SET portal_id=NULL WHERE portal_id=?", (portal_id,))
        db.commit()
        flash("Deleted.", "ok")
        return redirect("/portals")

    # ------------------------------------------------------------ export
    @app.get("/export.csv")
    def export():
        rows = get_db().execute(
            "SELECT j.company, j.title, j.location, j.url, j.score, j.sponsorship, j.status, j.first_seen, "
            "a.applied_at, a.follow_up, a.notes FROM jobs j LEFT JOIN applications a ON a.job_id=j.id "
            "ORDER BY j.first_seen DESC").fetchall()
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["company", "title", "location", "link", "score", "sponsorship", "status",
                    "first_seen", "applied_on", "follow_up", "notes"])
        for r in rows:
            fmt = lambda ts: datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
            w.writerow([_csv_cell(v) for v in (r["company"], r["title"], r["location"], r["url"], r["score"],
                                               r["sponsorship"], LABELS.get(r["status"], r["status"]),
                                               fmt(r["first_seen"]), fmt(r["applied_at"]), r["follow_up"], r["notes"])])
        return Response(out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=jobs.csv"})

    return app


if __name__ == "__main__":
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8000"))
    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("DASHBOARD_PASSWORD"):
        sys.exit("Refusing to listen on a network address without DASHBOARD_PASSWORD set.")
    print(f"Dashboard running at http://{host}:{port}  (Ctrl+C to stop)")
    create_app().run(host=host, port=port, debug=False)
