import io
import os
import sqlite3

import pytest

from dashboard import create_app
from db import Tracker


def make_job(n, company="Acme AI", score=80.0, status="new", url=None, sponsorship="sponsors"):
    return dict(url=url or f"https://jobs.example/{company}/{n}".replace(" ", "-"), company=company,
                title=f"ML Engineer {n}", location="Remote", description="Python RAG LLM role.",
                sponsorship=sponsorship, sponsorship_evidence="visa sponsorship is available",
                matched=["python", "rag"], gaps=["kubernetes"], score=score)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    db_path, data = str(tmp_path / "jobs.db"), str(tmp_path / "data")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("max_per_company: 2\n")
    app = create_app(db_path, data, str(cfg))
    app.testing = True
    t = Tracker(db_path, 2)
    return dict(app=app, client=app.test_client(), tracker=t, db_path=db_path, data=data, tmp=tmp_path)


def token(client):
    client.get("/portals")   # any page that renders a form creates the session's CSRF token
    with client.session_transaction() as s:
        return s["csrf"]


def post(client, path, **data):
    data["csrf"] = token(client)
    return client.post(path, data=data, follow_redirects=True)


def add_notified(t, n, company="Acme AI", score=80.0):
    jid = t.add(make_job(n, company, score))
    assert t.claim_slot(jid)[0]
    return jid


def test_pages_render_empty_and_with_data(env):
    c, t = env["client"], env["tracker"]
    for path in ("/", "/jobs", "/portals", "/export.csv"):
        assert c.get(path).status_code == 200
    jid = add_notified(t, 1)
    assert b"ML Engineer 1" in c.get("/").data
    r = c.get("/jobs")
    assert b"ML Engineer 1" in r.data and b"Acme AI" in r.data
    d = c.get(f"/jobs/{jid}")
    assert d.status_code == 200 and b"visa sponsorship is available" in d.data and b"kubernetes" in d.data
    assert c.get("/jobs/99999").status_code == 404
    assert b"ML Engineer 1" not in c.get("/jobs?status=applied").data
    assert b"ML Engineer 1" in c.get("/jobs?q=engineer&min_score=50&sort=newest").data
    assert b"ML Engineer 1" not in c.get("/jobs?min_score=95").data


def test_status_change_creates_application_and_counts_toward_cap(env):
    c, t = env["client"], env["tracker"]
    a, b = add_notified(t, 1), add_notified(t, 2)
    r = post(c, f"/jobs/{a}/status", status="applied")
    assert b"Marked as: Applied" in r.data
    db = sqlite3.connect(env["db_path"])
    assert db.execute("SELECT applied_at FROM applications WHERE job_id=?", (a,)).fetchone()[0] is not None
    assert db.execute("SELECT status FROM jobs WHERE id=?", (a,)).fetchone()[0] == "applied"
    # an applied job keeps its slot, so a third higher-scoring job cannot bump it
    third = t.add(make_job(3, score=99))
    assert t.claim_slot(third)[0] is True        # bumps the other 'notified' job (b), not the applied one
    assert db.execute("SELECT status FROM jobs WHERE id=?", (a,)).fetchone()[0] == "applied"
    assert db.execute("SELECT status FROM jobs WHERE id=?", (b,)).fetchone()[0] == "bumped"


def test_approving_held_job_respects_company_limit(env):
    c, t = env["client"], env["tracker"]
    add_notified(t, 1, score=90)
    add_notified(t, 2, score=85)
    held = t.add(make_job(3, score=60), status="needs_sponsorship_check")
    r = post(c, f"/jobs/{held}/status", status="notified")
    assert b"Not approved" in r.data
    assert sqlite3.connect(env["db_path"]).execute("SELECT status FROM jobs WHERE id=?", (held,)).fetchone()[0] == "capped"
    other = t.add(make_job(4, company="Other Co", score=60), status="needs_sponsorship_check")
    assert b"Approved" in post(c, f"/jobs/{other}/status", status="notified").data


def test_invalid_status_and_open_redirect_are_ignored(env):
    c, t = env["client"], env["tracker"]
    jid = add_notified(t, 1)
    assert b"Pick a status" in post(c, f"/jobs/{jid}/status", status="hacked").data
    r = c.post(f"/jobs/{jid}/status", data=dict(csrf=token(c), status="applied", next="//evil.example/x"))
    assert r.headers["Location"] == f"/jobs/{jid}"


def test_csrf_and_host_protection(env):
    c, t = env["client"], env["tracker"]
    jid = add_notified(t, 1)
    assert c.post(f"/jobs/{jid}/status", data=dict(status="applied")).status_code == 403
    assert c.post(f"/jobs/{jid}/status", data=dict(status="applied", csrf="wrong")).status_code == 403
    assert c.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_basic_auth_when_dashboard_password_set(env, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret-pass")
    c = env["app"].test_client()
    assert c.get("/").status_code == 401
    import base64
    ok = {"Authorization": "Basic " + base64.b64encode(b"me:s3cret-pass").decode()}
    bad = {"Authorization": "Basic " + base64.b64encode(b"me:nope").decode()}
    assert c.get("/", headers=ok).status_code == 200
    assert c.get("/", headers=bad).status_code == 401


def test_notes_and_followup_validation(env):
    c, t = env["client"], env["tracker"]
    jid = add_notified(t, 1)
    assert b"Follow-up date must" in post(c, f"/jobs/{jid}/notes", notes="x", follow_up="tomorrow").data
    assert b"Saved." in post(c, f"/jobs/{jid}/notes", notes="Recruiter: Sam", follow_up="2026-11-20", portal_id="").data
    assert b"Recruiter: Sam" in c.get(f"/jobs/{jid}").data
    assert b"2026-11-20" not in c.get("/").data          # follow-ups are listed for applied jobs only
    post(c, f"/jobs/{jid}/status", status="applied")
    assert b"2026-11-20" in c.get("/").data


def test_resume_upload_download_and_safety(env, monkeypatch):
    c, t = env["client"], env["tracker"]
    jid = add_notified(t, 1)
    good = post(c, f"/jobs/{jid}/resume") if False else c.post(
        f"/jobs/{jid}/resume", data=dict(csrf=token(c), resume=(io.BytesIO(b"%PDF-1.4 fake"), "My Resume.pdf")),
        content_type="multipart/form-data", follow_redirects=True)
    assert b"Resume saved" in good.data
    folder = os.path.join(env["data"], "resumes", str(jid))
    saved = os.listdir(folder)
    assert len(saved) == 1 and saved[0].endswith("My_Resume.pdf")
    dl = c.get(f"/files/{jid}/{saved[0]}")
    assert dl.status_code == 200 and dl.data.startswith(b"%PDF") and "attachment" in dl.headers["Content-Disposition"]
    # wrong type is refused
    bad = c.post(f"/jobs/{jid}/resume", data=dict(csrf=token(c), resume=(io.BytesIO(b"MZ"), "virus.exe")),
                 content_type="multipart/form-data", follow_redirects=True)
    assert b"Upload a .pdf" in bad.data and len(os.listdir(folder)) == 1
    # traversal and other jobs' files are not reachable
    assert c.get(f"/files/{jid}/../../secret_key").status_code == 404
    assert c.get(f"/files/{jid}/%2e%2e/%2e%2e/secret_key").status_code == 404
    other = add_notified(t, 2, company="Other Co")
    assert c.get(f"/files/{other}/{saved[0]}").status_code == 404


def test_attach_current_resume(env, monkeypatch):
    c, t = env["client"], env["tracker"]
    jid = add_notified(t, 1)
    monkeypatch.chdir(env["tmp"])
    assert b"Could not find" in post(c, f"/jobs/{jid}/resume/current").data
    os.makedirs("profile")
    open("profile/resume.docx", "wb").write(b"docx-bytes")
    assert b"Copied your current resume" in post(c, f"/jobs/{jid}/resume/current").data
    files = os.listdir(os.path.join(env["data"], "resumes", str(jid)))
    assert len(files) == 1 and files[0].endswith("_Resume.docx")


def test_vault_setup_unlock_reveal_and_no_plaintext(env):
    c, db_path = env["client"], env["db_path"]
    # cannot add while no vault exists
    assert b"vault is locked" in post(c, "/portals/add", name="Workday - Acme", username="me", password="pw123").data.lower()
    assert b"do not match" in post(c, "/vault/setup", master="long-enough-pass", confirm="different-pass-1").data
    assert b"at least 10" in post(c, "/vault/setup", master="short", confirm="short").data
    assert b"Vault created" in post(c, "/vault/setup", master="correct horse battery", confirm="correct horse battery").data
    assert b"Saved Workday" in post(c, "/portals/add", name="Workday - Acme", url="https://acme.wd1.example/login",
                                    username="neha@example.com", password="S3cr3t!Portal#Pw", notes="n").data
    # the secret appears nowhere in the database file, in any form
    raw = open(db_path, "rb").read() + (open(db_path + "-wal", "rb").read() if os.path.exists(db_path + "-wal") else b"")
    assert b"S3cr3t!Portal#Pw" not in raw
    stored = sqlite3.connect(db_path).execute("SELECT password_enc FROM portals").fetchone()[0]
    assert stored and "S3cr3t" not in stored
    # page never shows the password until Reveal
    assert b"S3cr3t!Portal#Pw" not in c.get("/portals").data
    pid = sqlite3.connect(db_path).execute("SELECT id FROM portals").fetchone()[0]
    rev = post(c, f"/portals/{pid}/reveal")
    assert b"S3cr3t!Portal#Pw" in rev.data and rev.headers["Cache-Control"] == "no-store"
    # locking blocks reveal
    post(c, "/vault/lock")
    assert b"S3cr3t!Portal#Pw" not in post(c, f"/portals/{pid}/reveal").data
    assert b"Wrong master password" in post(c, "/vault/unlock", master="not the password").data
    assert b"Vault unlocked" in post(c, "/vault/unlock", master="correct horse battery").data
    assert b"S3cr3t!Portal#Pw" in post(c, f"/portals/{pid}/reveal").data


def test_vault_rate_limit_and_idle_lock(env, monkeypatch):
    import vault as vault_mod
    c = env["client"]
    post(c, "/vault/setup", master="correct horse battery", confirm="correct horse battery")
    post(c, "/vault/lock")
    for _ in range(5):
        post(c, "/vault/unlock", master="wrong-guess-here")
    # sixth try is refused even with the right password
    assert b"Too many wrong attempts" in post(c, "/vault/unlock", master="correct horse battery").data
    # idle timeout locks it
    v = vault_mod.Vault()
    db = sqlite3.connect(env["db_path"])
    v._blocked_until = 0
    assert v.unlock(db, "correct horse battery") and v.is_unlocked()
    v._last -= vault_mod.IDLE_SECONDS + 1
    assert not v.is_unlocked()


def test_portal_validation_and_delete(env):
    c, db_path = env["client"], env["db_path"]
    post(c, "/vault/setup", master="correct horse battery", confirm="correct horse battery")
    assert b"must start with http" in post(c, "/portals/add", name="X", url="javascript:alert(1)").data
    assert b"Give the portal a name" in post(c, "/portals/add", name="", url="").data
    post(c, "/portals/add", name="Lever - Foo", url="https://jobs.lever.co/foo", username="u", password="p")
    pid = sqlite3.connect(db_path).execute("SELECT id FROM portals").fetchone()[0]
    assert b"Deleted" in post(c, f"/portals/{pid}/delete").data
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM portals").fetchone()[0] == 0


def test_untrusted_job_data_is_escaped_and_links_are_safe(env):
    c, t = env["client"], env["tracker"]
    evil = make_job(1, url="javascript:alert(1)")
    evil["title"] = "<script>alert(1)</script> ML Engineer"
    evil["description"] = "<img src=x onerror=alert(1)>"
    jid = t.add(evil)
    t.claim_slot(jid)
    for path in ("/", "/jobs", f"/jobs/{jid}"):
        body = c.get(path).data
        assert b"<script>alert(1)</script>" not in body and b"<img src=x" not in body
        assert b'href="javascript:' not in body
    assert "default-src 'none'" in c.get("/").headers["Content-Security-Policy"]


def test_csv_export_neutralises_formulas(env):
    c, t = env["client"], env["tracker"]
    j = make_job(1)
    j["title"] = "=HYPERLINK(\"http://evil\",\"click\")"
    jid = t.add(j)
    t.claim_slot(jid)
    body = c.get("/export.csv").data.decode()
    assert "'=HYPERLINK" in body and ",=HYPERLINK" not in body and body.startswith("company,title")


def test_ats_score_is_shown_and_tailor_route_works(tmp_path):
    import os, re
    from db import Tracker
    from dashboard import create_app
    db = str(tmp_path / "jobs.db")
    t = Tracker(db, 2)
    jid = t.add(dict(url="https://x/1", company="Acme", title="AI Engineer", location="Remote - US",
                     description="Python, FastAPI, RAG, Docker", score=80, sponsorship="sponsors",
                     matched=["python"], gaps=[]), status="notified")
    t.db.close()
    app = create_app(db, str(tmp_path / "data"), "config.yaml")
    c = app.test_client()
    h = {"Host": "127.0.0.1"}
    page = c.get(f"/jobs/{jid}", headers=h).get_data(as_text=True)
    tok = re.search(r'name="csrf" value="(\w+)"', page).group(1)
    assert "Create tailored resume" in page
    assert c.post(f"/jobs/{jid}/resume/tailor", data={"csrf": tok}, headers=h).status_code == 302
    page = c.get(f"/jobs/{jid}", headers=h).get_data(as_text=True)
    assert "ATS match" in page and "Hard skills" in page and "Download resume" in page
    assert os.path.isdir(tmp_path / "data" / "resumes" / str(jid))
    assert "Resume ATS" in c.get("/jobs?status=all", headers=h).get_data(as_text=True)


def test_master_resume_upload_and_instant_tailor(tmp_path, monkeypatch):
    import io, os, re, shutil
    monkeypatch.chdir(tmp_path)
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profile", "resume.docx")
    os.makedirs("profile"); shutil.copy(src, "profile/resume.docx")
    shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"), "config.yaml")
    from dashboard import create_app
    c = create_app("jobs.db", "data", "config.yaml").test_client()
    h = {"Host": "127.0.0.1"}
    tok = re.search(r'name="csrf" value="(\w+)"', c.get("/resume", headers=h).get_data(as_text=True)).group(1)
    r = c.post("/resume/upload", data={"csrf": tok, "resume": (io.BytesIO(open(src, "rb").read()), "r.docx")},
               headers=h, content_type="multipart/form-data", follow_redirects=True)
    assert "Master resume saved" in r.get_data(as_text=True) and os.path.exists("profile/master_resume.json")
    bad = c.post("/resume/upload", data={"csrf": tok, "resume": (io.BytesIO(b"not a docx"), "r.docx")},
                 headers=h, content_type="multipart/form-data", follow_redirects=True)
    assert "nothing was changed" in bad.get_data(as_text=True)
    r = c.post("/tailor", data={"csrf": tok, "title": "AI Engineer", "company": "Acme",
                                "description": "We need Python, FastAPI, LLM, RAG and Kubernetes experience. " * 3},
               headers=h)
    page = r.get_data(as_text=True)
    assert "Download resume" in page and "kubernetes" in page


def test_edit_tailored_resume(tmp_path):
    import os, re
    from db import Tracker
    from dashboard import create_app
    db = str(tmp_path / "jobs.db")
    t = Tracker(db, 2)
    jid = t.add(dict(url="https://x/e", company="Acme", title="AI Engineer", location="Remote - US",
                     description="Python FastAPI RAG Docker " * 5, score=80, sponsorship="sponsors"), status="notified")
    t.db.close()
    c = create_app(db, str(tmp_path / "data"), "config.yaml").test_client()
    h = {"Host": "127.0.0.1"}
    tok = re.search(r'name="csrf" value="(\w+)"', c.get(f"/jobs/{jid}", headers=h).get_data(as_text=True)).group(1)
    r = c.post(f"/jobs/{jid}/resume/tailor", data={"csrf": tok, "next": "edit"}, headers=h)
    assert r.status_code == 302 and r.headers["Location"].endswith("/resume/edit")
    page = c.get(f"/jobs/{jid}/resume/edit", headers=h).get_data(as_text=True)
    assert "Save &amp; rebuild" in page and 'name="job0_bullets"' in page
    form = {"csrf": tok, "name": "Alex Rivera", "contact": "me@x.com | (555) 010-0100",
            "summary": "My edited summary.", "skills": "Languages: Python, SQL",
            "job0_title": "Engineer", "job0_company": "Centene Corporation", "job0_location": "Remote",
            "job0_start": "August 2025", "job0_end": "Current", "job0_bullets": "Edited bullet one\nEdited bullet two",
            "education": "MS Computer Science"}
    assert c.post(f"/jobs/{jid}/resume/edit", data=form, headers=h).status_code == 302
    import docx
    folder = tmp_path / "data" / "resumes" / str(jid)
    doc = [f for f in os.listdir(folder) if f.endswith(".docx") and "Cover_Letter" not in f][0]
    text = "\n".join(p.text for p in docx.Document(str(folder / doc)).paragraphs)
    assert "My edited summary." in text and "Edited bullet two" in text


def test_today_ranks_best_first(tmp_path):
    import time
    from db import Tracker
    from dashboard import create_app
    from dates import to_iso
    db = str(tmp_path / "jobs.db")
    t = Tracker(db, 50)
    now = to_iso(time.time() - 60)
    base = dict(location="Remote - US", description="x", posted_at=now)
    t.add(dict(base, url="https://x/a", company="A", title="Weak", score=55, sponsorship="likely_sponsors",
               matched=["python"], gaps=["go", "rust", "java"]), status="notified")
    t.add(dict(base, url="https://x/b", company="B", title="Strong", score=92, sponsorship="sponsors",
               matched=["python", "llm", "rag"], gaps=[]), status="notified")
    t.add(dict(base, url="https://x/c", company="C", title="Held", score=99, sponsorship="unclear"),
          status="needs_sponsorship_check")
    t.add(dict(base, url="https://x/o", company="O", title="SixMonthsOld", score=99, sponsorship="sponsors",
               posted_at=to_iso(time.time() - 180 * 86400)), status="notified")
    for i in range(30):
        t.add(dict(base, url=f"https://x/m{i}", company=f"M{i}", title=f"Many{i}", score=70, sponsorship="sponsors"),
              status="notified")
    t.db.close()
    c = create_app(db, str(tmp_path / "data"), "config.yaml").test_client()
    page = c.get("/today", headers={"Host": "127.0.0.1"}).get_data(as_text=True)
    assert page.index("Strong") < page.index("Weak") and "Held" not in page
    assert "SixMonthsOld" not in page                        # only posted today / yesterday
    assert page.count('class="t" href="/jobs/') == 32        # no 25-row cap
    assert "Best new matches" in c.get("/", headers={"Host": "127.0.0.1"}).get_data(as_text=True)


def test_cover_letter_made_with_resume_and_editable(tmp_path):
    import os, re, json, docx
    from db import Tracker
    from dashboard import create_app
    db = str(tmp_path / "jobs.db")
    t = Tracker(db, 2)
    jid = t.add(dict(url="https://x/c", company="Acme", title="AI Engineer", location="Remote - US",
                     description="LLM, RAG, prompt engineering, Python, React", score=90, sponsorship="sponsors"),
                status="notified")
    t.db.close()
    c = create_app(db, str(tmp_path / "data"), "config.yaml").test_client()
    h = {"Host": "127.0.0.1"}
    tok = re.search(r'name="csrf" value="(\w+)"', c.get(f"/jobs/{jid}", headers=h).get_data(as_text=True)).group(1)
    c.post(f"/jobs/{jid}/resume/tailor", data={"csrf": tok}, headers=h)
    folder = tmp_path / "data" / "resumes" / str(jid)
    letter = [f for f in os.listdir(folder) if f.endswith("_Cover_Letter.docx")]
    assert letter, os.listdir(folder)
    page = c.get(f"/jobs/{jid}", headers=h).get_data(as_text=True)
    assert "Download cover letter" in page
    # every experience sentence in the letter is a bullet from the resume, in first person
    resume = json.load(open(folder / "tailored.json"))
    bullets = {b.strip().rstrip(".").lower() for j in resume["experience"] for b in j["bullets"]}
    body = open(folder / "cover_letter.txt").read()
    assert "Acme" in body and "AI Engineer" in body
    claims = re.findall(r"I ([a-z][^.]*)\.", body.split("\n\n")[1])
    assert claims and all(any(b.startswith(cl.lower()[:60]) for b in bullets) for cl in claims)
    # edit and save
    edit = c.get(f"/jobs/{jid}/cover/edit", headers=h).get_data(as_text=True)
    assert "Save cover letter" in edit
    c.post(f"/jobs/{jid}/cover/edit", data={"csrf": tok, "action": "save", "body": "Para one.\n\nPara two."}, headers=h)
    text = "\n".join(p.text for p in docx.Document(str(folder / letter[0])).paragraphs)
    assert "Para two." in text and "Dear Acme Hiring Team," in text


def test_add_missing_keywords_to_one_job_resume(tmp_path):
    import os, re, json, docx
    from db import Tracker
    from dashboard import create_app
    db = str(tmp_path / "jobs.db")
    t = Tracker(db, 2)
    jid = t.add(dict(url="https://x/s", company="Acme", title="AI Engineer", location="Remote - US",
                     description="LangChain agents, Kubernetes, Python, RAG", score=80, sponsorship="sponsors",
                     gaps=["langchain/agents", "kubernetes"]), status="notified")
    t.db.close()
    c = create_app(db, str(tmp_path / "data"), "config.yaml").test_client()
    h = {"Host": "127.0.0.1"}
    tok = re.search(r'name="csrf" value="(\w+)"', c.get(f"/jobs/{jid}", headers=h).get_data(as_text=True)).group(1)
    c.post(f"/jobs/{jid}/resume/tailor", data={"csrf": tok}, headers=h)
    page = c.get(f"/jobs/{jid}/resume/edit", headers=h).get_data(as_text=True)
    assert "Add missing keywords" in page and "LangChain, AI Agents" in page
    master_before = open("profile/master_resume.json").read()
    r = c.post(f"/jobs/{jid}/resume/suggest", headers=h,
               data={"csrf": tok, "skill": ["langchain/agents"], "bullet_langchain/agents_0": "1"})
    assert r.status_code == 302
    folder = tmp_path / "data" / "resumes" / str(jid)
    st = json.load(open(folder / "tailored.json"))
    assert any("LangChain" in x for v in st["skills"].values() for x in v)
    assert any("LangChain" in b for b in st["experience"][0]["bullets"])
    assert not any("Kubernetes" in x for v in st["skills"].values() for x in v)      # not ticked, not added
    doc = [f for f in os.listdir(folder) if f.endswith(".docx") and "Cover_Letter" not in f][0]
    assert "LangChain" in "\n".join(p.text for p in docx.Document(str(folder / doc)).paragraphs)
    assert open("profile/master_resume.json").read() == master_before                # master untouched


def test_added_keywords_are_recognised_and_rescore(tmp_path):
    import re
    from db import Tracker
    from dashboard import create_app
    db = str(tmp_path / "jobs.db")
    t = Tracker(db, 2)
    jid = t.add(dict(url="https://x/r", company="Acme", title="AI Engineer", location="Remote - US",
                     description="LangChain agents, Kubernetes, Kubernetes, Python, RAG", score=50, sponsorship="sponsors",
                     gaps=["langchain/agents", "kubernetes"]), status="notified")
    t.db.close()
    c = create_app(db, str(tmp_path / "data"), "config.yaml").test_client()
    h = {"Host": "127.0.0.1"}
    tok = re.search(r'name="csrf" value="(\w+)"', c.get(f"/jobs/{jid}", headers=h).get_data(as_text=True)).group(1)
    c.post(f"/jobs/{jid}/resume/tailor", data={"csrf": tok}, headers=h)
    t = Tracker(db, 2)
    before = t.db.execute("SELECT score, gaps FROM jobs WHERE id=?", (jid,)).fetchone()
    assert "kubernetes" in before["gaps"]
    c.post(f"/jobs/{jid}/resume/suggest", headers=h,
           data={"csrf": tok, "skill": ["langchain/agents", "kubernetes"], "bullet_kubernetes_0": "1"})
    after = t.db.execute("SELECT score, matched, gaps FROM jobs WHERE id=?", (jid,)).fetchone()
    assert after["score"] > before["score"]
    assert "kubernetes" in after["matched"] and "kubernetes" not in after["gaps"]
    page = c.get(f"/jobs/{jid}", headers=h).get_data(as_text=True)
    assert "kubernetes &middot; added" in page and "Saved in this job" in page


def test_application_answers_page(tmp_path, monkeypatch):
    import re, json, answers
    from db import Tracker
    from dashboard import create_app
    monkeypatch.setattr(answers, "STANDARD_PATH", str(tmp_path / "answers.json"))
    db = str(tmp_path / "jobs.db")
    t = Tracker(db, 2)
    jid = t.add(dict(url="https://x/q", company="Acme", title="Forward Deployed Engineer", location="Remote - US",
                     description="LLM, RAG, Python, customer-facing", score=90, sponsorship="sponsors"), status="notified")
    t.db.close()
    c = create_app(db, str(tmp_path / "data"), "config.yaml").test_client()
    h = {"Host": "127.0.0.1"}
    page = c.get(f"/jobs/{jid}/answers", headers=h).get_data(as_text=True)
    assert "Why do you want to work at Acme?" in page and "authorized to work in the United States" in page
    tok = re.search(r'name="csrf" value="(\w+)"', page).group(1)
    form = {"csrf": tok, "action": "save", "n_job": "1", "jq0": "Why Acme?", "ja0": "Because.", "n_std": "1",
            "sq0": "Desired salary", "sa0": "150000"}
    assert c.post(f"/jobs/{jid}/answers", data=form, headers=h).status_code == 302
    page = c.get(f"/jobs/{jid}/answers", headers=h).get_data(as_text=True)
    assert "Because." in page and "150000" in page
    assert json.load(open(tmp_path / "answers.json")) == [["Desired salary", "150000"]]


def test_extension_api(tmp_path, monkeypatch):
    import base64, answers
    from db import Tracker
    from dashboard import create_app
    monkeypatch.setattr(answers, "STANDARD_PATH", str(tmp_path / "answers.json"))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    db = str(tmp_path / "jobs.db")
    t = Tracker(db, 2)
    jid = t.add(dict(url="https://job-boards.greenhouse.io/acme/jobs/4012345", company="Acme",
                     title="Forward Deployed Engineer", location="Remote - US",
                     description="Python, LLM, RAG", score=90, sponsorship="sponsors"), status="notified")
    t.db.close()
    c = create_app(db, str(tmp_path / "data"), "config.yaml").test_client()
    auth = {"Host": "127.0.0.1", "Authorization": "Basic " + base64.b64encode(b"me:pw").decode()}
    assert c.get("/api/jobs/recent", headers=auth).status_code == 403          # no extension header
    assert c.get("/api/jobs/recent", headers={"Host": "127.0.0.1", "X-JobAgent": "1"}).status_code == 401
    h = dict(auth, **{"X-JobAgent": "1"})
    assert c.get("/api/jobs/recent", headers=h).get_json()["jobs"][0]["id"] == jid
    d = c.get("/api/autofill", query_string={"url": "https://job-boards.greenhouse.io/acme/jobs/4012345?gh_src=x#app"},
              headers=h).get_json()
    assert d["job"]["id"] == jid and d["profile"]["first_name"] and "@" in d["profile"]["email"]
    assert d["resume"]["name"].endswith(".docx") and base64.b64decode(d["resume"]["b64"])[:2] == b"PK"
    assert d["cover"] and d["cover_letter_text"] and any("sponsorship" in q for q, _ in d["standard"])
    assert c.get("/api/autofill", query_string={"url": "https://example.com/other"}, headers=h).get_json()["job"] is None
    assert c.post(f"/api/jobs/{jid}/applied", headers=h).get_json()["ok"]
    t = Tracker(db, 2)
    assert t.db.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "applied"
