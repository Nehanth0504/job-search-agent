import json
import time
import os
import tempfile

import yaml

from db import Tracker, normalize_company
from pipeline import title_ok
from sponsorship import classify

CFG = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "config.yaml")))
# The tests cover both roles and the "hold" location policy, whatever config.yaml is set to today.
CFG["roles"] = dict(CFG["roles"], only="all")
CFG["location"] = dict(CFG.get("location") or {}, unclear="hold")


def test_sponsorship_negative_wins():
    v, _ = classify("We sponsor visas for many roles, but this role requires U.S. citizenship.")
    assert v == "does_not_sponsor"
    assert classify("We are unable to provide visa sponsorship at this time.")[0] == "does_not_sponsor"
    assert classify("Candidates must be able to work without sponsorship.")[0] == "does_not_sponsor"
    assert classify("Active Secret clearance required.")[0] == "does_not_sponsor"


def test_sponsorship_positive_and_unclear():
    assert classify("H-1B sponsorship is available for this role.")[0] == "sponsors"
    assert classify("We offer visa support and relocation.")[0] == "sponsors"
    assert classify("Build ranking models in PyTorch.")[0] == "unclear"


def test_company_normalization():
    assert normalize_company("Google LLC") == "google"
    assert normalize_company("Alphabet Inc.") == "google"
    assert normalize_company("Meta Platforms, Inc.") == "meta"
    assert normalize_company("Acme Technologies Ltd") == "acme"


def test_title_filter():
    assert title_ok("Senior Machine Learning Engineer", CFG)
    assert title_ok("Software Engineer, AI Platform", CFG)
    assert not title_ok("Applied Scientist, LLM", CFG)            # scientist roles are not your target
    assert not title_ok("Software Engineer, Payments", CFG)       # no AI/ML signal
    assert not title_ok("Machine Learning Intern", CFG)
    assert not title_ok("Engineering Manager, ML", CFG)


def _job(n, company="Google LLC", score=50):
    return dict(url=f"https://x/{company}/{n}", company=company, title="ML Engineer",
                description="", sponsorship="sponsors", score=score)


def test_cap_max_two_and_bump():
    t = Tracker(os.path.join(tempfile.mkdtemp(), "t.db"), max_per_company=2)
    ids = [t.add(_job(i, score=s)) for i, s in enumerate([60, 70, 50])]
    assert t.claim_slot(ids[0]) == (True, None)
    assert t.claim_slot(ids[1]) == (True, None)
    # third, lower than both -> capped, never notified
    assert t.claim_slot(ids[2]) == (False, None)
    # a fourth that beats the weakest notified one bumps it (score 60 -> bumped)
    i4 = t.add(_job(4, score=90))
    assert t.claim_slot(i4) == (True, ids[0])
    # alias of same company shares the cap
    i5 = t.add(_job(5, company="Alphabet Inc.", score=20))
    assert t.claim_slot(i5) == (False, None)


def test_applied_jobs_are_never_bumped():
    t = Tracker(os.path.join(tempfile.mkdtemp(), "t.db"), max_per_company=2)
    a, b = t.add(_job(1, score=40)), t.add(_job(2, score=45))
    t.claim_slot(a); t.claim_slot(b)
    t.set_status(a, "applied"); t.set_status(b, "applied")
    c = t.add(_job(3, score=99))
    assert t.claim_slot(c) == (False, None)


def test_email_message_and_fallback(monkeypatch):
    import notifier

    job = dict(url="https://x/1", company="Acme", title="ML Engineer", location="Remote",
               score=82.0, sponsorship="sponsors", sponsorship_evidence="visa sponsorship is available")
    m = notifier.build_message(job, "me@example.com", "bot@example.com")
    assert m["Subject"] == "[82] ML Engineer @ Acme"
    assert "visa sponsorship is available" in m.get_content()

    # no credentials -> console fallback, no SMTP attempted
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    assert notifier.send_job_alert(job, "me@example.com") is False

    # with credentials -> logs in and sends through the (fake) SMTP server
    sent = []

    class FakeSMTP:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): sent.append(("login", u))
        def send_message(self, msg): sent.append(("send", msg["To"]))

    monkeypatch.setenv("GMAIL_USER", "bot@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    assert notifier.send_job_alert(job, "me@example.com", smtp_factory=FakeSMTP) is True
    assert sent == [("login", "bot@example.com"), ("send", "me@example.com")]


def test_do_not_sponsor_variants_rejected():
    for t in ["We do not sponsor work visas.", "Company will not sponsor H-1B candidates.",
              "We are not currently sponsoring visas.", "No H1B for this position.",
              "H-1B is not available for this role.", "Not open to visa sponsorship."]:
        assert classify(t)[0] == "does_not_sponsor", t
    assert classify("We provide H-1B transfer and visa sponsorship.")[0] == "sponsors"


def test_title_filter_roles_and_word_boundaries():
    assert title_ok("Forward Deployed Engineer", CFG)
    assert title_ok("Forward-Deployed Software Engineer, AI", CFG)
    assert title_ok("FDE - Healthcare", CFG)
    assert not title_ok("Forward Deployed Engineer Intern", CFG)
    assert not title_ok("Software Engineer, Retail Platform", CFG)   # 'ai' inside 'retail'
    assert not title_ok("HTML Software Engineer", CFG)               # 'ml' inside 'html'
    assert title_ok("AI/ML Software Engineer", CFG)


def test_resume_scoring_and_gaps():
    from scoring import load_profile, score_job
    skills = load_profile(os.path.join(os.path.dirname(__file__), "profile", "master_resume.json"))
    assert {"python", "fastapi", "rag", "llm", "docker"} <= skills
    assert "kubernetes" not in skills and "pytorch" not in skills

    good = dict(title="Machine Learning Engineer",
                description="Build RAG and LLM features in Python with FastAPI, Docker, AWS and PostgreSQL.")
    s, matched, gaps = score_job(good, skills)
    assert s >= 80 and "rag" in matched and gaps == []

    stretch = dict(title="Machine Learning Engineer",
                   description="PyTorch distributed training on Kubernetes with CUDA GPUs and Spark; some Python.")
    s2, matched2, gaps2 = score_job(stretch, skills)
    assert s2 < s and {"pytorch", "kubernetes", "gpu/cuda", "spark"} <= set(gaps2)


def _write_lca_csv(path):
    import csv
    cols = ["CASE_NUMBER", "CASE_STATUS", "VISA_CLASS", "EMPLOYER_NAME", "JOB_TITLE",
            "SOC_CODE", "TOTAL_WORKER_POSITIONS", "CHANGE_EMPLOYER"]
    rows = []
    for i in range(30):   # 30 genuine certified H-1B tech positions, 6 of them transfers
        rows.append([f"Z{i}", "Certified", "H-1B", "ZETA INC.", "Software Engineer", "15-1252", "1", "1" if i < 6 else "0"])
    rows.append(["Z0", "Certified", "H-1B", "ZETA INC.", "Software Engineer", "15-1252", "1", "1"])     # duplicate case
    rows += [[f"W{i}", "Certified - Withdrawn", "H-1B", "ZETA INC.", "Software Engineer", "15-1252", "1", "0"] for i in range(5)]
    rows += [[f"D{i}", "Denied", "H-1B", "ZETA INC.", "Software Engineer", "15-1252", "1", "0"] for i in range(5)]
    rows += [[f"V{i}", "Certified", "H-1B1", "ZETA INC.", "Software Engineer", "15-1252", "1", "0"] for i in range(3)]
    rows += [[f"T{i}", "Certified", "H-1B", "Tiny Co", "Software Engineer", "15-1252", "1", "1"] for i in range(2)]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)


def test_h1b_index_counts_only_real_certified_h1b(tmp_path):
    from h1b_history import H1BHistory, build_index
    src, out = tmp_path / "lca.csv", str(tmp_path / "idx.db")
    _write_lca_csv(src)
    assert build_index([str(src)], out) == 2
    h = H1BHistory(out)
    z = h.lookup("Zeta")
    assert (z["certified"], z["tech"], z["transfers"]) == (30, 30, 6) and z["ok"]
    assert not h.lookup("Tiny Co")["ok"]            # only 2 filings
    assert not h.lookup("Unknown Corp")["ok"]       # no history -> never auto-passed


def test_pipeline_history_policy(tmp_path, monkeypatch):
    import pipeline
    from h1b_history import build_index

    src, idx = tmp_path / "lca.csv", str(tmp_path / "idx.db")
    _write_lca_csv(src)
    build_index([str(src)], idx)

    def post(company, url, desc):
        return dict(company=company, title="Machine Learning Engineer", url=url, location="Remote - US",
                    posted_at=None, description=desc)

    fake = [post("Zeta", "https://z/1", "Python, FastAPI, RAG, LLM."),                     # silent, strong history
            post("Tiny Co", "https://t/1", "Python, FastAPI, RAG, LLM."),                  # silent, weak history
            post("Zeta", "https://z/2", "Python, RAG. We do not sponsor visas."),          # explicit no beats history
            post("Nobody", "https://n/1", "Python, FastAPI, RAG, LLM.")]                   # silent, unknown
    monkeypatch.setattr(pipeline, "fetch_all", lambda companies, seen=None, wanted=None: iter(fake))

    cfg = dict(CFG, master_resume=os.path.join(os.path.dirname(__file__), "profile", "master_resume.json"),
               min_sponsorship="sponsors_or_history",
               h1b_history=dict(data_file=idx, min_certified=25, min_tech=5, min_transfers=3))
    t = Tracker(str(tmp_path / "t.db"), cfg["max_per_company"])
    stats = pipeline.run_once(cfg, [], t)

    status = {r["url"]: (r["status"], r["sponsorship"]) for r in t.db.execute("SELECT url,status,sponsorship FROM jobs")}
    assert status["https://z/1"] == ("notified", "likely_sponsors")
    assert status["https://t/1"] == ("notified", "unclear")          # listed, flagged, resume made
    assert status["https://z/2"][0] == "rejected_sponsorship"
    assert status["https://n/1"] == ("notified", "unclear")
    assert stats["history_ok"] == 1 and stats["notified"] == 3

    # holding mode + explicit-only: unclear postings are held, history ignored
    t2 = Tracker(str(tmp_path / "t2.db"), 2)
    pipeline.run_once(dict(cfg, min_sponsorship="sponsors", hold_unclear_sponsorship=True), [], t2)
    assert t2.db.execute("SELECT COUNT(*) FROM jobs WHERE status='notified'").fetchone()[0] == 0
    held = t2.db.execute("SELECT COUNT(*) FROM jobs WHERE status='needs_sponsorship_check'").fetchone()[0]
    assert held == 3

    # switching holding off releases the backlog on the next run, with resumes
    monkeypatch.setattr(pipeline, "fetch_all", lambda companies, seen=None, wanted=None: iter([]))
    pipeline.run_once(cfg, [], t2, use_aggregators=False)
    rows = t2.db.execute("SELECT url, status, sponsorship, resume_file FROM jobs WHERE url != 'https://z/2'").fetchall()
    assert all(r["status"] == "notified" and r["resume_file"] for r in rows)
    assert {r["url"]: r["sponsorship"] for r in rows}["https://z/1"] == "likely_sponsors"


def test_alert_email_flags_inferred_sponsorship():
    import notifier
    job = dict(url="https://x/1", company="Zeta", title="ML Engineer", location="Remote", score=90.0,
               sponsorship="likely_sponsors", sponsorship_evidence="Filing history (not stated in the posting): ...")
    body = notifier.build_message(job, "me@example.com", "bot@example.com").get_content()
    assert "NOT stated in the posting" in body and "Confirm with the recruiter" in body


def test_target_titles_in_and_out():
    from roles import title_ok as t
    wanted = [
        "Machine Learning Engineer", "Senior Machine Learning Engineer", "Software Engineer, AI/ML",
        "Software Engineer - Machine Learning Platform", "AI Engineer", "Senior AI Engineer",
        "Applied AI Engineer, Beneficial Deployments (Lab Automation)", "LLM Engineer",
        "Software Engineer, Generative AI", "MLOps Engineer", "Backend Engineer, AI Products",
        "Full Stack Engineer - AI", "Python Developer (ML)", "Software Engineer III, Machine Learning",
        "Forward Deployed Engineer", "Forward Deployed AI Engineer", "Senior Forward-Deployed Engineer, Healthcare",
        "Forward Deployed Software Engineer", "FDE - Financial Services",
    ]
    unwanted = [
        "Staff Machine Learning Engineer", "Principal AI Engineer", "Distinguished Engineer, ML",
        "Machine Learning Intern", "Junior ML Engineer", "Software Engineer I, Machine Learning",
        "Associate Software Engineer, AI", "New Grad Software Engineer, AI", "Entry Level AI Engineer",
        "Research Scientist, NLP", "Applied Scientist", "Research Engineer, Machine Learning", "AI Researcher",
        "Data Scientist", "Data Engineer, AI", "Machine Learning Data Analyst",
        "Director of AI", "Engineering Manager, ML", "Head of Machine Learning", "VP Engineering, AI",
        "Technical Program Manager, AI", "Product Manager, AI", "AI Solutions Architect",
        "Sales Engineer, AI", "Hardware Engineer, ML Accelerators", "Mechanical Engineer", "QA Engineer, AI",
        "Software Engineer, Payments", "Software Engineer, Retail Platform", "HTML Software Engineer",
        "Forward Deployed Engineering Manager", "Staff Forward Deployed Engineer", "Forward Deployed Engineer Intern",
        "Recruiter, Machine Learning", "AI Product Designer",
    ]
    assert [x for x in wanted if not t(x, CFG)] == []
    assert [x for x in unwanted if t(x, CFG)] == []


def test_years_of_experience_parsing_and_level_band():
    from roles import level_check, required_years
    assert required_years("5+ years of experience in software engineering") == 5
    assert required_years("3-5 years of professional experience") == 3
    assert required_years("at least five years of relevant experience") == 5
    assert required_years("5+ years software engineering experience and 2+ years of ML experience") == 5
    assert required_years("8+ years of experience; 3+ years of experience with LLMs") == 8
    assert required_years("Experience: 4 years building APIs") == 4
    assert required_years("Preferred: 10 years of experience with Kubernetes. Required: 4 years of experience") == 4
    assert required_years("We were founded 12 years ago and offer 4 years vesting.") is None   # no 'experience'
    assert required_years("Build ML systems in Python.") is None

    ok = lambda text: level_check(text, CFG)[0]
    assert ok("5+ years of experience")            # your level
    assert ok("3-5 years of experience")
    assert ok("No years are mentioned here.")      # judged on title alone
    assert ok("1-2 years of experience")           # 5 years or fewer is fine
    assert ok("0-2 years of experience")
    assert ok("2+ years of experience")
    assert not ok("6 years of experience")         # more than 5
    assert not ok("7+ years of experience")
    assert not ok("10+ years of experience")


def test_pipeline_drops_wrong_role_and_wrong_level(tmp_path, monkeypatch):
    import pipeline
    def post(title, url, desc):
        return dict(company="Acme AI", title=title, url=url, location="Remote - US", posted_at=None,
                    description=desc + " We offer visa sponsorship.")
    fake = [post("Machine Learning Engineer", "https://a/ok", "5+ years of experience. Python, LLM, RAG."),
            post("Machine Learning Engineer", "https://a/senior", "9+ years of experience. Python, LLM."),
            post("Machine Learning Engineer", "https://a/junior", "0-2 years of experience. Python, LLM."),
            post("Staff Machine Learning Engineer", "https://a/staff", "5+ years of experience. Python."),
            post("Research Scientist, NLP", "https://a/research", "5+ years of experience. Python."),
            post("Machine Learning Engineer", "https://a/noyears", "Python, LLM, RAG, FastAPI.")]
    monkeypatch.setattr(pipeline, "fetch_all", lambda companies, seen=None, wanted=None: iter(fake))
    cfg = dict(CFG, master_resume=os.path.join(os.path.dirname(__file__), "profile", "master_resume.json"))
    t = Tracker(str(tmp_path / "t.db"), 5)
    stats = pipeline.run_once(cfg, [], t)
    status = {r["url"]: r["status"] for r in t.db.execute("SELECT url, status FROM jobs")}
    assert status["https://a/ok"] == "notified" and status["https://a/noyears"] == "notified"
    assert status["https://a/senior"] == "wrong_level" and status["https://a/junior"] == "notified"
    assert "https://a/staff" not in status and "https://a/research" not in status   # wrong role: never stored
    assert stats["wrong_level"] == 1 and stats["notified"] == 3


def test_sponsorship_negation_in_context():
    cases = {
        # must be REJECTED: the posting says no (several ways of saying it)
        "does_not_sponsor": [
            "We do not currently offer H-1B sponsorship.",
            "We cannot offer visa sponsorship.",
            "We cannot, at this time, offer sponsorship for this role.",
            "Visa sponsorship is not available for this position.",
            "Sponsorship is unavailable.",
            "Applicants must not require sponsorship now or in the future.",
            "We are unable to sponsor H-1B visas.",
            "Unfortunately we don't sponsor work visas.",
            "H-1B candidates will not be considered.",
            "We do not sponsor green cards or work visas.",
            "We are not able to offer employment sponsorship.",
            "No relocation. We do not offer visa sponsorship.",
            "This role requires US citizenship.",
            "Only US citizens will be considered.",
            "We are not in a position to offer sponsorship at this time.",
            "Sponsorship will not be provided.",
            "Must be eligible for a TS/SCI clearance.",
            "Subject to ITAR export controls.",
        ],
        # must be ACCEPTED: the posting says yes, including near-misses that contain negations
        "sponsors": [
            "We offer visa sponsorship.",
            "We provide H-1B sponsorship for eligible candidates.",
            "Visa sponsorship is available.",
            "We are able to sponsor work visas.",
            "Sponsorship for work visas is offered.",
            "No relocation assistance, but visa sponsorship is available.",
            "We do not sponsor green cards, but H-1B sponsorship is available.",
            "We do not discriminate on the basis of visa status. Visa sponsorship is available.",
            "We accept H-1B transfers and provide visa support.",
            "Sponsorship is available for the right candidate.",
            "Visa sponsorship and relocation assistance are available.",
        ],
        # no signal either way: must NOT be treated as a yes or a no
        "unclear": [
            "We are an equal opportunity employer. Protected veteran or military status will not affect hiring.",
            "Our military and veteran hiring program is growing; we do not discriminate.",
            "Build ranking models in PyTorch.",
            "Join our sponsored events and meetups. We do not require a cover letter.",
            "",
        ],
    }
    wrong = [(want, t, classify(t)[0]) for want, texts in cases.items() for t in texts if classify(t)[0] != want]
    assert wrong == []


def test_location_classifier():
    from locations import classify_location as c
    us = ["San Francisco, CA", "Remote - US", "Remote, United States", "United States", "USA", "Remote (USA)",
          "US-Remote", "U.S. Remote", "New York, NY", "New York City, NY | Seattle, WA", "Austin, Texas",
          "Palo Alto, California, United States", "Washington, DC", "Hybrid - Boston", "Seattle, WA / Remote",
          "San Francisco, CA | London, UK", "Remote - US or Canada", "Wilmington, DE", "Indianapolis, IN",
          "Denver, CO", "Atlanta, Georgia", "Portland, OR", "AUSTIN, TX", "Remote, US (Eastern Time)",
          "New Mexico", "Chicago | Toronto"]
    non_us = ["London, UK", "Toronto, Canada", "Remote - Canada", "Toronto, ON, CA", "Bengaluru, India",
              "Bangalore, IN", "Dublin, Ireland", "Remote - EMEA", "Berlin, Germany", "Berlin, DE", "Singapore",
              "Remote - Europe", "Sydney, Australia", "Mexico City, Mexico", "Tokyo, Japan", "Zurich",
              "Tbilisi, Georgia", "Vancouver, BC, Canada", "Remote - APAC", "Paris, France"]
    unclear = ["Remote", "", None, "Anywhere", "Global", "Worldwide", "Hybrid", "Remote - Americas",
               "North America"]
    assert [x for x in us if c(x) != "us"] == []
    assert [x for x in non_us if c(x) != "non_us"] == []
    assert [x for x in unclear if c(x) != "unclear"] == []


def test_description_can_resolve_a_bare_remote():
    from locations import us_in_description as d
    assert d("This is a remote (US) position.") and d("Remote - United States. Great team.")
    assert d("You must be located in the United States.") and d("We are a US-based company hiring now.")
    assert d("Candidates must be authorized to work in the United States.")
    assert not d("Join us for the future of AI. We are a global team.")
    assert not d("The United States Equal Employment Opportunity Commission enforces the law.")


def test_pipeline_us_only(tmp_path, monkeypatch):
    import pipeline
    desc = "5 years of experience. Python, LLM, RAG. Visa sponsorship is available."
    def post(url, location, extra=""):
        return dict(company="Acme AI", title="Machine Learning Engineer", url=url, location=location,
                    posted_at=None, description=desc + extra)
    fake = [post("https://a/us", "San Francisco, CA"),
            post("https://a/uk", "London, UK"),
            post("https://a/ca", "Toronto, ON, CA"),
            post("https://a/remote", "Remote"),
            post("https://a/remote-us-text", "Remote", " This is a remote (US) role."),
            post("https://a/multi", "Berlin, Germany | New York, NY")]
    monkeypatch.setattr(pipeline, "fetch_all", lambda companies, seen=None, wanted=None: iter(fake))
    base = dict(CFG, master_resume=os.path.join(os.path.dirname(__file__), "profile", "master_resume.json"))

    def run(location_cfg):
        t = Tracker(str(tmp_path / f"t{len(str(location_cfg))}{location_cfg.get('unclear','')}.db"), 9)
        stats = pipeline.run_once(dict(base, location=location_cfg), [], t)
        return stats, {r["url"]: r["status"] for r in t.db.execute("SELECT url, status FROM jobs")}

    stats, status = run({"country": "US", "unclear": "hold"})
    assert status["https://a/us"] == "notified" and status["https://a/multi"] == "notified"
    assert status["https://a/remote-us-text"] == "notified"          # resolved from the description
    assert status["https://a/remote"] == "location_unclear"          # held for review, not emailed
    assert "https://a/uk" not in status and "https://a/ca" not in status   # outside the US: never stored
    assert stats["outside_us"] == 2 and stats["location_unclear"] == 1 and stats["notified"] == 3

    _, status = run({"country": "US", "unclear": "keep"})
    assert status["https://a/remote"] == "notified"
    _, status = run({"country": "US", "unclear": "drop"})
    assert "https://a/remote" not in status


def test_fetchers_collect_every_location():
    import fetchers

    class Resp:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data

    class Client:
        def __init__(self, data): self.data = data
        def get(self, url, params=None): return Resp(self.data)

    gh = {"jobs": [{"title": "ML Engineer", "absolute_url": "https://g/1", "content": "&lt;p&gt;Python&lt;/p&gt;",
                    "location": {"name": "Remote"},
                    "offices": [{"name": "SF", "location": "San Francisco, CA"}, {"name": "NYC", "location": "New York, NY"}]}]}
    lv = [{"text": "AI Engineer", "hostedUrl": "https://l/1", "descriptionPlain": "Python", "country": "US",
           "categories": {"location": "Remote", "allLocations": ["Remote", "Austin, TX"]}}]
    ab = {"jobs": [{"title": "FDE", "jobUrl": "https://a/1", "descriptionHtml": "<p>Python</p>", "location": "Remote",
                    "secondaryLocations": [{"location": "Chicago, IL"}],
                    "address": {"postalAddress": {"addressCountry": "United States"}}}]}
    g = list(fetchers.greenhouse(Client(gh), "x", "Acme"))[0]
    l = list(fetchers.lever(Client(lv), "x", "Acme"))[0]
    a = list(fetchers.ashby(Client(ab), "x", "Acme"))[0]
    assert g["location"] == "Remote | San Francisco, CA | New York, NY"
    assert l["location"] == "Remote | Austin, TX | United States"
    assert a["location"] == "Remote | Chicago, IL | United States"
    # sparse records must not crash
    assert list(fetchers.greenhouse(Client({"jobs": [{"title": "t", "absolute_url": "u"}]}), "x", "Acme"))[0]["location"] == ""


# ---------------------------------------------------------------- resume tailoring
def test_tailored_resume_uses_only_master_facts(tmp_path):
    import json
    from tailor import tailor
    from ats import check
    master = json.load(open("profile/master_resume.json"))
    job = dict(title="Software Engineer, AI/ML", company="Acme AI",
               description="LLM apps in Python with FastAPI, RAG, vector search, Docker, Kubernetes, PyTorch.")
    path, res = tailor(job, 1, data_dir=str(tmp_path))
    import docx
    text = "\n".join(p.text for p in docx.Document(path).paragraphs)
    allowed = {b for j in master["experience"] for b in j["bullets"]}
    bullets = [p.text for p in docx.Document(path).paragraphs if p.style.name == "List Bullet"]
    assert bullets and all(b in allowed for b in bullets)          # verbatim, nothing invented
    for j in master["experience"]:
        assert j["company"] in text and j["start"] in text
    assert "kubernetes" in res["gaps"] and "pytorch" in res["gaps"]   # gaps reported, never added
    skills_text = " ".join(x for v in master["skills"].values() for x in v)
    assert ("Kubernetes" in text) == ("Kubernetes" in skills_text)   # only if you already list it
    assert res["format"] == 20                                       # readability: parses cleanly
    assert "Kubernetes" in res["missing_terms"] and "PyTorch" in res["missing_terms"]
    assert {p["name"] for p in res["parts"]} == {"Readability", "Hard skills", "Job title", "Experience",
                                                 "Education", "Soft skills"}
    assert sum(p["max"] for p in res["parts"]) == 100


def test_ats_flags_missing_sections(tmp_path):
    import json
    import docx
    from ats import check
    from tailor import load_profile_from
    master = json.load(open("profile/master_resume.json"))
    d = docx.Document(); d.add_paragraph("Just some text"); p = tmp_path / "x.docx"; d.save(str(p))
    res = check(str(p), dict(title="AI Engineer", description="python"), master, load_profile_from(master))
    assert res["score"] < 95 and not res["meets_target"]


def test_held_jobs_still_get_scored(tmp_path):
    from db import Tracker
    from pipeline import rescore_stored
    from scoring import load_profile
    t = Tracker(str(tmp_path / "j.db"), 2)
    t.add(dict(url="https://x/h", company="Cohere", title="Forward Deployed Engineer",
               description="Python, LLM, RAG, Kubernetes"), status="needs_sponsorship_check")
    assert rescore_stored(t, load_profile()) == 1
    r = t.db.execute("SELECT score, matched, gaps FROM jobs").fetchone()
    assert r["score"] and "python" in r["matched"] and "kubernetes" in r["gaps"]


def test_sponsorship_context_sentence():
    from sponsorship import context_sentence
    assert "visa" in context_sentence("We build things. Candidates must hold a valid visa today. Join us.")
    assert context_sentence("We build things. Join us.") is None


def test_company_board_discovery():
    import verify_companies as v
    assert v.slug_variants("Scale AI")[:3] == ["scaleai", "scale-ai", "scale_ai"]
    assert v.slug_variants("Weights & Biases")[0] == "weightsandbiases"
    boards = {("ashby", "cohere"), ("greenhouse", "scale-ai")}
    probe = lambda ats, slug: (ats, slug) in boards  # noqa: E731
    assert v.find_board("Cohere", probe) == dict(name="Cohere", ats="ashby", slug="cohere")
    assert v.find_board("Scale AI", probe)["slug"] == "scale-ai"
    assert v.find_board("Nope Inc", probe) is None
    assert v.is_board("greenhouse", {"jobs": []}) and not v.is_board("greenhouse", {"error": 1})
    merged, added = v.merge([dict(name="Cohere", ats="ashby", slug="cohere")],
                            [dict(name="Cohere", ats="ashby", slug="cohere"), dict(name="X", ats="lever", slug="x")])
    assert len(merged) == 2 and [a["name"] for a in added] == ["X"]


def test_board_discovery_helpers():
    import discover_boards as d
    assert d.slug_from_url("https://boards.greenhouse.io/anthropic/jobs/123") == ("greenhouse", "anthropic")
    assert d.slug_from_url("https://job-boards.greenhouse.io/databricks") == ("greenhouse", "databricks")
    assert d.slug_from_url("https://boards.greenhouse.io/embed/job_app?for=Acme&token=1") == ("greenhouse", "acme")
    assert d.slug_from_url("https://jobs.lever.co/mistral/abc-123") == ("lever", "mistral")
    assert d.slug_from_url("https://jobs.ashbyhq.com/cohere/xyz") == ("ashby", "cohere")
    assert d.slug_from_url("https://boards.greenhouse.io/embed/job_board/js?for=") is None
    assert d.slug_from_url("https://example.com/foo") is None
    rows = d.titles_and_places("greenhouse", {"jobs": [{"title": "AI Engineer", "location": {"name": "Austin, TX"}}]})
    assert rows == [("AI Engineer", "Austin, TX")]
    merged, added = d.merge([dict(name="Cohere", ats="ashby", slug="cohere")],
                            {("ashby", "cohere"): {"hits": 3}, ("lever", "acme-ai"): {"hits": 2},
                             ("lever", "empty"): {"hits": 0}})
    assert [a["slug"] for a in added] == ["acme-ai"] and added[0]["name"] == "Acme Ai"


def test_scan_board_counts_only_target_roles_in_us():
    import yaml
    import discover_boards as d

    class R:
        status_code = 200
        def json(self):
            return {"jobs": [{"title": "Software Engineer, AI/ML", "location": {"name": "New York, NY"}},
                             {"title": "Software Engineer, AI/ML", "location": {"name": "London, UK"}},
                             {"title": "Account Executive", "location": {"name": "Austin, TX"}}]}

    class C:
        def get(self, url):
            return R()

    cfg = CFG
    assert d.scan_board(C(), "greenhouse", "x", cfg) == {"jobs": 3, "hits": 1}


def test_crawl_finds_boards_of_every_system_and_reports_failures():
    import json
    import discover_boards as d

    class R:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text
        def json(self):
            return [dict(id="CC-X", **{"cdx-api": "https://x/api"})]

    lines = {"jobs.lever.co/*": ["https://jobs.lever.co/acme/1", "https://jobs.lever.co/acme/2"],
             "myworkdayjobs.com": ["https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x"],
             "bamboohr.com": ["https://beta.bamboohr.com/careers/7"]}

    class C:
        def get(self, url, params=None, timeout=None):
            if url.endswith("collinfo.json"):
                return R(200)
            if params["url"] == "icims.com":
                return R(400, "bad request")
            if params["page"] > 0:
                return R(400, "no more pages")
            return R(200, "\n".join(json.dumps({"url": u}) for u in lines.get(params["url"], [])))

    logs = []
    found = d.crawl_slugs(C(), pages=3, crawls=1, log=logs.append)
    kinds = {a for a, _ in found}
    assert {"lever", "workday", "bamboohr"} <= kinds and len(found) == 3       # acme counted once
    assert any("icims.com: answered 400" in l for l in logs)


def test_jsearch_conversion_and_throttle(tmp_path):
    import fetchers
    j = dict(job_apply_link="https://linkedin.com/jobs/1", job_title="Forward Deployed Engineer",
             employer_name="Acme", job_city="Austin", job_state="TX", job_country="US",
             job_description="<p>Python and LLMs</p>", job_is_remote=False,
             apply_options=[dict(apply_link="https://acme.com/careers/1", is_direct=True)])
    out = fetchers._jsearch_job(j)
    assert out["url"] == "https://acme.com/careers/1" and out["location"] == "Austin, TX, US"
    assert out["description"] == "Python and LLMs" and out["source"] == "aggregator"
    assert fetchers._jsearch_job(dict(job_title="x")) is None

    class R:
        def __init__(self, code, rows=()):
            self.status_code, self._rows = code, rows
        def json(self):
            return {"data": list(self._rows)}

    class C:
        calls = 0
        def get(self, url, headers=None, params=None):
            C.calls += 1
            return R(200, [j] * 10) if C.calls == 1 else R(429)

    logs = []
    jobs = list(fetchers.jsearch(C(), dict(queries=["a", "b"], pages_per_query=3), "k", logs.append))
    assert len(jobs) == 10 and any("allowance" in l for l in logs) and C.calls == 2   # stops at the first 429
    # not enabled / no key -> nothing happens
    assert list(fetchers.fetch_aggregators({}, str(tmp_path / "s.json"), log=lambda *_: None)) == []


def test_cross_source_duplicate(tmp_path):
    from db import Tracker
    t = Tracker(str(tmp_path / "j.db"), 2)
    t.add(dict(url="https://boards.greenhouse.io/acme/1", company="Acme, Inc.", title="AI Engineer",
               description="x"), status="notified")
    assert t.seen_posting("ACME", "ai engineer") and not t.seen_posting("Acme", "Data Scientist")


def test_fast_lane_selection_and_eightfold():
    import fetchers
    from pipeline import fast_companies
    cfg = {"fast_lane": {"companies": ["Scale AI", "microsoft"]}}
    cos = [dict(name="Scale AI", ats="greenhouse", slug="scaleai"), dict(name="Microsoft", ats="eightfold"),
           dict(name="Other", ats="lever", slug="o")]
    assert [c["name"] for c in fast_companies(cfg, cos)] == ["Scale AI", "Microsoft"]

    class R:
        status_code = 200
        def __init__(self, data):
            self._d = data
        def json(self):
            return self._d

    class C:
        def get(self, url, params=None):
            if url.endswith("/search"):
                if params["start"] == 0:
                    return R({"data": {"positions": [dict(id=7, name="AI Engineer", positionUrl="/careers/job/7",
                                                          locations=["Redmond, WA, US"], postedTs=1)]}})
                return R({"data": {"positions": []}})
            return R({"data": {"jobDescription": "<p>Build LLM apps</p>"}})

    c = dict(name="Microsoft", ats="eightfold", host="apply.careers.microsoft.com", domain="microsoft.com",
             queries=["ai"])
    jobs = list(fetchers.eightfold(C(), c))
    assert jobs[0]["url"] == "https://apply.careers.microsoft.com/careers/job/7"
    assert jobs[0]["description"] == "Build LLM apps" and jobs[0]["location"] == "Redmond, WA, US"
    # already-seen jobs do not trigger a detail request
    assert list(fetchers.eightfold(C(), c, seen=lambda u: True))[0]["description"] == ""


def test_greenhouse_downloads_text_only_for_wanted_new_postings():
    import fetchers

    class R:
        status_code = 200
        def __init__(self, d): self._d = d
        def raise_for_status(self): pass
        def json(self): return self._d

    calls = []
    class C:
        def get(self, url, params=None):
            calls.append(url)
            if url.endswith("/jobs"):
                return R({"jobs": [dict(id=1, title="AI Engineer", absolute_url="u1", location={"name": "NY"}),
                                   dict(id=2, title="Accountant", absolute_url="u2", location={"name": "NY"}),
                                   dict(id=3, title="AI Engineer II", absolute_url="u3", location={"name": "NY"})]})
            return R({"content": "<p>text</p>"})

    jobs = list(fetchers.greenhouse(C(), "x", "X", seen=lambda u: u == "u3", wanted=lambda t: "Engineer" in t))
    assert [j["description"] for j in jobs] == ["text", "", ""]
    assert sum(1 for c in calls if c.endswith("/jobs/1")) == 1 and len(calls) == 2


def test_load_env(tmp_path, monkeypatch):
    import os
    from pipeline import load_env
    f = tmp_path / ".env"
    f.write_text('# c\nJSEARCH_API_KEY="abc123"\nGMAIL_USER=me@x.com\n\nBAD LINE\n')
    monkeypatch.delenv("JSEARCH_API_KEY", raising=False)
    monkeypatch.setenv("GMAIL_USER", "keep@x.com")
    load_env(str(f))
    assert os.environ["JSEARCH_API_KEY"] == "abc123" and os.environ["GMAIL_USER"] == "keep@x.com"
    load_env(str(tmp_path / "missing"))     # no file: nothing happens


def test_eightfold_downloads_text_only_for_matching_titles_and_caps():
    import fetchers

    class R:
        status_code = 200
        def __init__(self, d): self._d = d
        def json(self): return self._d

    details = []
    class C:
        def get(self, url, params=None):
            if url.endswith("/search"):
                if params["start"]:
                    return R({"data": {"positions": []}})
                return R({"data": {"positions": [dict(id=i, name=("AI Engineer" if i % 2 else "Accountant"),
                                                      positionUrl=f"/careers/job/{i}") for i in range(10)]}})
            details.append(params["position_id"])
            return R({"data": {"jobDescription": "x"}})

    c = dict(name="M", ats="eightfold", host="h", domain="d", queries=["q"], max_details=2)
    jobs = list(fetchers.eightfold(C(), c, wanted=lambda t: "Engineer" in t))
    assert len(jobs) == 10 and details == [1, 3]          # only matching titles, and no more than the cap


def test_per_company_interval(monkeypatch):
    import sys
    import types
    import fetchers
    calls = []

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

    fake = types.SimpleNamespace(Client=FakeClient, Timeout=lambda *a, **k: None, Limits=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "httpx", fake)           # no real network or httpx needed
    monkeypatch.setattr(fetchers, "FETCHERS", {"lever": lambda c, slug, name: calls.append(name) or iter(())})
    fetchers._LAST_RUN.clear()
    co = [dict(name="Slow", ats="lever", slug="s", every_seconds=900)]
    list(fetchers.fetch_all(co))
    list(fetchers.fetch_all(co))
    assert calls == ["Slow"]                               # a second check within 15 minutes is skipped


def test_learn_boards(tmp_path):
    import learn

    class R:
        def __init__(s, code, data): s.status_code, s._d = code, data
        def json(s): return s._d

    class C:
        def get(s, url):
            if "acme" in url:
                return R(200, {"jobs": [{"title": "Machine Learning Engineer", "location": {"name": "Remote, US"}}]})
            return R(404, {})
    path = str(tmp_path / "l.json")
    comps = []
    cfg = CFG
    links = ["https://boards.greenhouse.io/acme/jobs/1", "https://boards.greenhouse.io/nope/jobs/2", "https://example.com/x"]
    added = learn.learn(links, comps, cfg, path=path, client=C())
    assert added == ["Acme"] and comps[0]["slug"] == "acme"
    assert learn.learn(links, comps, cfg, path=path, client=C()) == []      # not probed again
    fresh = []
    assert learn.merge_into(fresh, path) == 1 and fresh[0]["slug"] == "acme"


def test_workday(tmp_path):
    import fetchers, learn

    class R:
        def __init__(s, code, data): s.status_code, s._d, s.text = code, data, ""
        def json(s): return s._d

    class C:
        def post(s, url, json=None):
            return R(200, {"jobPostings": [
                {"title": "Machine Learning Engineer", "externalPath": "/job/US-CA/ML-Eng_R1", "locationsText": "US, CA, Santa Clara"},
                {"title": "Barista", "externalPath": "/job/x/Barista_R2", "locationsText": "Seattle"}]})
        def get(s, url):
            return R(200, {"jobPostingInfo": {"jobDescription": "<p>Python and LLMs</p>", "location": "Santa Clara, CA",
                                              "externalUrl": "https://acme.wd5.myworkdayjobs.com/Site/job/US-CA/ML-Eng_R1"}})
    c = dict(name="Acme", ats="workday", host="acme.wd5.myworkdayjobs.com", tenant="acme", site="Site")
    jobs = list(fetchers.workday(C(), c, wanted=lambda t: "Engineer" in t))
    assert [j["title"] for j in jobs] == ["Machine Learning Engineer", "Barista"]
    assert "Python and LLMs" in jobs[0]["description"] and jobs[1]["description"] == ""
    assert learn.workday_from_url("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA/x_R1") == \
        dict(host="nvidia.wd5.myworkdayjobs.com", tenant="nvidia", site="NVIDIAExternalCareerSite")
    assert learn.workday_from_url("https://example.com/en-US/x") is None
    path = str(tmp_path / "w.json")
    comps = []
    cfg = CFG
    added = learn.learn(["https://acme.wd5.myworkdayjobs.com/en-US/Site/job/a"], comps, cfg, path=path, client=C())
    assert added == ["Acme"] and comps[0]["ats"] == "workday"
    fresh = []
    assert learn.merge_into(fresh, path) == 1 and learn.merge_into(fresh, path) == 0


def test_merge_ignores_entries_without_slug():
    import verify_companies as v
    existing = [dict(name="Microsoft", ats="eightfold", host="x")]
    merged, added = v.merge(existing, [dict(name="Acme", ats="greenhouse", slug="acme")])
    assert len(merged) == 2 and added[0]["slug"] == "acme"


def test_h1b_lookup_adds_up_name_variants(tmp_path):
    import sqlite3
    from h1b_history import H1BHistory
    p = str(tmp_path / "h.db")
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE employers(company_norm TEXT PRIMARY KEY, display TEXT, certified INT, tech INT, transfers INT)")
    db.executemany("INSERT INTO employers VALUES (?,?,?,?,?)", [
        ("goldman sachs &", "GOLDMAN SACHS & CO. LLC", 100, 60, 10), ("goldman sachs bank usa", "GS BANK", 50, 30, 5),
        ("wal mart associates", "WAL-MART ASSOCIATES", 40, 40, 5), ("cohere health", "COHERE HEALTH", 99, 99, 9)])
    db.commit()
    h = H1BHistory(p)
    assert h.lookup("Goldman Sachs")["certified"] == 150 and h.lookup("Goldman Sachs")["ok"]
    assert h.lookup("Walmart")["ok"]
    assert not h.lookup("Cohere")["ok"]          # 'cohere health' is a different company, not a generic variant


def test_extra_providers_and_second_pass():
    import fetchers, verify_companies as v

    class R:
        def __init__(s, data, code=200): s._d, s.status_code = data, code
        def json(s): return s._d
        def raise_for_status(s): pass

    class C:
        def get(s, url, params=None):
            if "smartrecruiters" in url and url.endswith("/postings"):
                return R({"content": [{"id": "1", "name": "ML Engineer", "location": {"city": "Austin", "region": "TX", "country": "us"}}]})
            if "smartrecruiters" in url:
                return R({"jobAd": {"sections": {"jobDescription": {"text": "<p>Python LLM</p>"}}}})
            if "workable" in url:
                return R({"jobs": [{"title": "AI Engineer", "url": "https://apply.workable.com/a/j/1", "city": "NYC", "country": "United States", "description": "<b>RAG</b>"}]})
            return R({"offers": [{"title": "FDE", "careers_url": "https://a.recruitee.com/o/1", "location": "Remote", "country": "United States", "description": "x"}]})
    sr = list(fetchers.smartrecruiters(C(), "acme", "Acme"))
    assert sr[0]["description"] == "Python LLM" and "Austin" in sr[0]["location"]
    assert list(fetchers.workable(C(), "acme", "Acme"))[0]["description"] == "RAG"
    assert list(fetchers.recruitee(C(), "acme", "Acme"))[0]["title"] == "FDE"
    assert v.is_board("smartrecruiters", {"content": [], "totalFound": 0}) is False
    assert v.is_board("workable", {"jobs": [{}]}) is True
    assert v.find_board("Acme Inc", lambda ats, slug: ats == "workable" and slug == "acme", v.SECOND_PASS)["ats"] == "workable"


def test_find_careers_reads_job_system_from_company_site():
    import find_careers as fc
    html = ('<a href="https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite">Jobs</a>'
            '<a href="https://boards.greenhouse.io/acme/jobs/1">x</a><a href="https://jobs.smartrecruiters.com/Acme2">y</a>')
    got = fc.boards_in(html)
    assert ("workday", dict(host="nvidia.wd5.myworkdayjobs.com", tenant="nvidia", site="NVIDIAExternalCareerSite")) in got
    assert ("greenhouse", dict(slug="acme")) in got and ("smartrecruiters", dict(slug="Acme2")) in got
    assert fc.candidate_sites("Scale AI")[0] == "scaleai.com"

    class R:
        def __init__(s, code, text, url): s.status_code, s.text, s.url = code, text, url

    class C:
        def get(s, url):
            if url == "https://www.acme.com/careers":
                return R(200, html, url)
            raise OSError("no such site")
    fc.validate = lambda client, ats, fields, cfg: ats == "greenhouse"
    assert fc.find("Acme", C(), {})["ats"] == "greenhouse"


def test_fetch_all_runs_boards_in_parallel_and_survives_failures(monkeypatch):
    import sys, threading, time, types, fetchers

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(
        Client=FakeClient, Timeout=lambda *a, **k: None, Limits=lambda *a, **k: None))
    threads = set()

    def slow(client, slug, company):
        threads.add(threading.get_ident())
        time.sleep(0.2)
        if slug == "bad":
            raise RuntimeError("timed out")
        yield dict(company=company, title="ML Engineer", url=f"https://x/{slug}", location="US", description="")
    monkeypatch.setitem(fetchers.FETCHERS, "lever", slow)
    fetchers._LAST_RUN.clear()
    comps = [dict(name=f"C{i}", ats="lever", slug=("bad" if i == 3 else f"s{i}")) for i in range(12)]
    t = time.time()
    jobs = list(fetchers.fetch_all(comps))
    assert len(jobs) == 11 and time.time() - t < 1.0 and len(threads) > 1


def test_fast_lane_hiring_mode():
    from pipeline import fast_companies
    cos = [dict(name="A"), dict(name="B"), dict(name="C")]
    now = 1_000_000_000
    hot = {"A": now - 60, "B": now - 30 * 86400}
    assert [c["name"] for c in fast_companies({"fast_lane": {"companies": "hiring"}}, cos, hot, now)] == ["A"]
    assert len(fast_companies({"fast_lane": {"companies": "all"}}, cos, hot, now)) == 3


def test_fetch_all_drops_text_of_unwanted_or_seen_jobs(monkeypatch):
    import sys, types, fetchers

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(
        Client=FakeClient, Timeout=lambda *a, **k: None, Limits=lambda *a, **k: None))

    def board(client, slug, company):
        for t, u in (("ML Engineer", "new"), ("Barista", "b"), ("ML Engineer", "old")):
            yield dict(company=company, title=t, url=f"https://x/{slug}/{u}", location="US", description="x" * 5000)
    monkeypatch.setitem(fetchers.FETCHERS, "lever", board)
    fetchers._LAST_RUN.clear()
    jobs = list(fetchers.fetch_all([dict(name=f"C{i}", ats="lever", slug=f"s{i}") for i in range(40)],
                                   seen=lambda u: u.endswith("/old"), wanted=lambda t: "Engineer" in t))
    assert len(jobs) == 120
    kept = [j for j in jobs if j["description"]]
    assert len(kept) == 40 and all(j["url"].endswith("/new") for j in kept)


def test_posting_dates_normalized():
    from dates import to_iso, describe
    ref = 1793640000                                     # 2026-11-02 17:20 UTC
    assert to_iso(1793600000000) == to_iso(1793600000) == "2026-11-02T06:13:20Z"
    assert to_iso("2026-10-30T07:00:00-05:00") == "2026-10-30T12:00:00Z"
    assert to_iso("2026-10-30") == "2026-10-30T12:00:00Z"
    assert to_iso("2026-10-30 12:00:00 UTC") == "2026-10-30T12:00:00Z"
    assert to_iso("Posted 3 Days Ago", ref) == "2026-10-30T17:20:00Z"
    assert to_iso("Posted Today", ref) == "2026-11-02T17:20:00Z"
    assert to_iso("nonsense") is None and to_iso(None) is None
    from datetime import datetime, timezone
    assert describe("2026-10-30T17:20:00Z", datetime(2026, 11, 2, 17, 20, tzinfo=timezone.utc)).endswith("3 days ago")


def test_old_dates_converted(tmp_path):
    import pipeline
    t = Tracker(str(tmp_path / "d.db"), 2)
    jid = t.add(dict(url="https://w/1", company="W", title="ML Engineer", posted_at="Posted 2 Days Ago"), status="notified")
    t.db.execute("UPDATE jobs SET first_seen=? WHERE id=?", (1793640000, jid))
    t.db.commit()
    assert pipeline.normalize_dates(t) == 1
    assert t.db.execute("SELECT posted_at FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "2026-10-31T17:20:00Z"


def test_old_postings_are_not_new_matches(tmp_path, monkeypatch):
    import pipeline, time
    from dates import to_iso
    now = time.time()
    fake = [dict(company="Acme", title="Machine Learning Engineer", url="https://a/old", location="Remote - US",
                 posted_at=to_iso(now - 30 * 86400), description="Python, RAG, LLM. We sponsor H-1B visas."),
            dict(company="Acme", title="Machine Learning Engineer II", url="https://a/new", location="Remote - US",
                 posted_at=to_iso(now - 600), description="Python, RAG, LLM. We sponsor H-1B visas.")]
    monkeypatch.setattr(pipeline, "fetch_all", lambda companies, seen=None, wanted=None: iter(fake))
    cfg = dict(CFG, master_resume=os.path.join(os.path.dirname(__file__), "profile", "master_resume.json"),
               max_posting_age_days=3)
    t = Tracker(str(tmp_path / "t.db"), 2)
    stats = pipeline.run_once(cfg, [], t, use_aggregators=False)
    st = {r["url"]: r["status"] for r in t.db.execute("SELECT url, status FROM jobs")}
    assert st["https://a/old"] == "too_old" and st["https://a/new"] == "notified"
    assert stats["too_old"] == 1 and stats["notified"] == 1
    # a match saved earlier ages out of New match on a later run
    t.db.execute("UPDATE jobs SET posted_at=? WHERE url='https://a/new'", (to_iso(now - 5 * 86400),))
    t.db.commit()
    assert pipeline.demote_old(t, cfg) == 1



def test_forward_deployed_only_mode(tmp_path):
    import pipeline
    cfg = dict(CFG, roles=dict(CFG["roles"], only="forward_deployed"))
    assert title_ok("Forward Deployed Engineer", cfg)
    assert title_ok("Senior Forward-Deployed Software Engineer, AI", cfg)
    assert title_ok("FDE - Enterprise", cfg)
    assert not title_ok("Machine Learning Engineer", cfg)
    assert not title_ok("AI Engineer", cfg)
    # existing non-FDE matches leave the lists; applied ones stay
    t = Tracker(str(tmp_path / "r.db"), 5)
    a = t.add(dict(url="https://r/1", company="A", title="Machine Learning Engineer"), status="notified")
    b = t.add(dict(url="https://r/2", company="B", title="Forward Deployed Engineer"), status="notified")
    c = t.add(dict(url="https://r/3", company="C", title="AI Engineer"), status="applied")
    assert pipeline.hide_other_roles(t, cfg) == 1
    st = {r["id"]: r["status"] for r in t.db.execute("SELECT id, status FROM jobs")}
    assert st == {a: "other_role", b: "notified", c: "applied"}


def test_end_of_day_summary(tmp_path):
    import digest, time
    from datetime import datetime
    db = str(tmp_path / "s.db")
    t = Tracker(db, 5)
    t.add(dict(url="https://s/1", company="Acme", title="Forward Deployed Engineer", score=88,
               sponsorship="sponsors"), status="notified")
    t.add(dict(url="https://s/2", company="Beta", title="FDE", score=70, sponsorship="unclear"), status="notified")
    t.db.close()
    cfg = dict(alert_email="me@example.com", daily_summary=dict(hour=21, dashboard_url="https://dash"))
    state = str(tmp_path / "st.json")
    assert not digest.maybe_send(cfg, db, datetime(2026, 11, 3, 15, 0), state)        # before 9 PM: nothing
    assert digest.maybe_send(cfg, db, datetime.now().replace(hour=21, minute=5), state)
    assert not digest.maybe_send(cfg, db, datetime.now().replace(hour=22, minute=0), state)  # once a day
    subject, body, counts = digest.build(db, time.time() - 3600, "2026-11-03", "https://dash")
    assert counts["new"] == 2 and "Forward Deployed Engineer @ Acme" in body and "[sponsorship unclear]" in body
    assert body.index("Acme") < body.index("Beta")                                    # best first


class _R:
    def __init__(self, data=None, text="", code=200, url="https://x/"):
        self._d, self.text, self.status_code, self.url = data, text, code, url
    def json(self): return self._d
    def raise_for_status(self): pass


def test_more_job_systems():
    import fetchers_more as fm
    wanted = lambda t: "Forward Deployed" in t  # noqa: E731

    class C:
        def get(self, url, params=None):
            if url.endswith("/careers/list"):
                return _R({"result": [{"id": "7", "jobOpeningName": "Forward Deployed Engineer",
                                       "location": {"city": "Austin", "state": "Texas"}, "atsLocation": {"country": "United States"}},
                                      {"id": "8", "jobOpeningName": "Accountant", "location": {}}]})
            if url.endswith("/careers/7/detail"):
                return _R({"result": {"jobOpening": {"description": "<p>LLM work</p>", "datePosted": "2026-11-02"}}})
            if url.endswith(".breezy.hr/json"):
                return _R([{"name": "Forward Deployed Engineer", "url": "https://a.breezy.hr/p/1",
                            "published_date": "2026-11-02T10:00:00Z", "location": {"city": "NYC", "country": {"name": "United States"}}}])
            if "breezy.hr/p/1" in url:
                return _R(text='<div class="description"><p>Customer-facing AI</p></div></div>')
            if url.endswith("postings.json"):
                return _R({"data": [{"title": "Forward Deployed Engineer", "url": "https://a.pinpointhq.com/postings/1",
                                     "location": {"city": "Denver", "province": "Colorado"}, "description": "RAG"}]})
            if "recruitingCEJobRequisitions" in url:
                return _R({"items": [{"requisitionList": [{"Id": "55", "Title": "Forward Deployed Engineer",
                                                           "PrimaryLocation": "Chicago, IL, United States",
                                                           "PostedDate": "2026-11-01"}]}]})
            if "recruitingCEJobRequisitionDetails" in url:
                return _R({"items": [{"ExternalDescriptionStr": "<b>Python</b>"}]})
            if "icims.com/jobs/search" in url:
                return _R(text='<a href="https://careers-acme.icims.com/jobs/123/forward-deployed-engineer/job?in_iframe=1">'
                               '<span>Forward Deployed Engineer</span></a>')
            if "icims.com/jobs/123" in url:
                return _R(text="<dt>Job Locations</dt><dd>US-TX-Austin</dd><dt>Posted Date</dt><dd>2026-11-02</dd> AI")
            raise AssertionError(url)

    c = C()
    b = list(fm.bamboohr(c, dict(name="A", slug="acme"), wanted=wanted))
    assert b[0]["description"] == "LLM work" and b[0]["posted_at"] == "2026-11-02" and "Austin" in b[0]["location"]
    assert b[1]["description"] == ""                                       # not your role: no text fetched
    br = list(fm.breezy(c, dict(name="A", slug="a"), wanted=wanted))
    assert "Customer-facing AI" in br[0]["description"]
    assert list(fm.pinpoint(c, dict(name="A", slug="a")))[0]["location"].startswith("Denver")
    o = list(fm.oracle(c, dict(name="A", host="x.fa.us2.oraclecloud.com", site="CX_1", queries=["fde"]), wanted=wanted))
    assert o[0]["url"].endswith("/sites/CX_1/job/55") and o[0]["description"] == "Python"
    i = list(fm.icims(c, dict(name="A", host="careers-acme.icims.com", queries=["fde"]), wanted=wanted))
    assert i[0]["title"] == "Forward Deployed Engineer" and i[0]["location"] == "US-TX-Austin"


def test_own_careers_page_reader():
    import fetchers_more as fm
    listing = ('<html><a href="/jobs/42">Forward Deployed Engineer, AI</a><a href="/jobs/43">Office Manager</a>'
               '<a href="/about">About us</a></html>')
    detail = ('<script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting",'
              '"title":"Forward Deployed Engineer, AI","datePosted":"2026-11-03",'
              '"jobLocation":{"address":{"addressLocality":"Seattle","addressRegion":"WA","addressCountry":"US"}},'
              '"description":"<p>Ship LLM apps with customers</p>"}</script>')

    class C:
        def get(self, url, params=None):
            return _R(text=listing, url="https://acme.com/careers") if url.endswith("/careers") else _R(text=detail, url=url)
    jobs = list(fm.careerpage(C(), dict(name="Acme", url="https://acme.com/careers"),
                              wanted=lambda t: "Forward Deployed" in t))
    assert len(jobs) == 1 and jobs[0]["url"] == "https://acme.com/jobs/42"
    assert jobs[0]["posted_at"] == "2026-11-03" and "Seattle" in jobs[0]["location"]
    assert "Ship LLM apps" in jobs[0]["description"]


def test_find_careers_detects_new_systems():
    import find_careers as fc
    page = ('<a href="https://acme.bamboohr.com/careers">x</a> <a href="https://careers-acme.icims.com/jobs/intro">y</a>'
            '<a href="https://abc.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions">z</a>')
    got = fc.boards_in(page)
    assert ("bamboohr", dict(slug="acme")) in got and ("icims", dict(host="careers-acme.icims.com")) in got
    assert ("oracle", dict(host="abc.fa.us2.oraclecloud.com", site="CX_1")) in got


def test_master_resume_accepts_other_headings_and_agent_layout(tmp_path):
    import json, tailor, build_master_resume as b
    master = json.load(open(os.path.join(os.path.dirname(__file__), "profile", "master_resume.json")))
    p = str(tmp_path / "r.docx")
    tailor.write_docx(master, p)                     # the agent's own layout: CAPITAL headings, tabs
    d = b.parse(b.docx_to_text(p))
    assert [j["company"] for j in d["experience"]] == [j["company"] for j in master["experience"]]
    assert d["experience"][2]["location"] == "Dallas, TX" and len(d["skills"]) == len(master["skills"])
    assert b.heading_of("PROFESSIONAL SUMMARY") == "Professional Summary"
    assert b.heading_of("Technical Skills:") == "Skills & Tools" and b.heading_of("Work Experience") == "Professional Experience"
    try:
        b.parse("Name\nme@x.com\nObjective\nHello\n")
        assert False
    except ValueError as e:
        assert "Skills & Tools" in str(e) and "Professional Experience" in str(e)


def test_projects_and_headline_round_trip(tmp_path):
    import tailor, build_master_resume as b
    text = ("JANE DOE\nDallas, TX | 555-123-4567 | jane@x.com\nForward Deployed Engineer | Applied AI\n"
            "SUMMARY\nEngineer with 5+ years.\nTECHNICAL SKILLS\nAI: LLMs, RAG\nWork Experience\nAcme\n"
            "Forward Deployed Engineer\tAugust 2025 – Present | Remote\n- Built RAG with LLMs for customers.\n"
            "PROJECTS\nJob Agent | Python, LLMs\n- Built an agent that finds jobs with RAG.\n- Deployed it on a server.\n"
            "Chat Bot: A Slack bot answering HR questions.\nEducation\nMS CS")
    d = b.parse(text)
    assert d["name"] == "Jane Doe" and d["headline"] == "Forward Deployed Engineer | Applied AI"
    assert d["experience"][0]["location"] == "Remote" and d["experience"][0]["end"] == "Present"
    assert [p["name"] for p in d["projects"]] == ["Job Agent | Python, LLMs", "Chat Bot"]
    assert d["projects"][1]["bullets"] == ["A Slack bot answering HR questions."]
    job = dict(company="Z", title="Forward Deployed Engineer", description="RAG LLM agents")
    st = tailor.select(d, job)
    assert st["projects"][0]["name"].startswith("Job Agent")
    p = str(tmp_path / "x.docx")
    tailor.write_docx(st, p)
    again = b.parse(b.docx_to_text(p))
    assert [x["name"] for x in again["projects"]][0].startswith("Job Agent") and again["headline"] == d["headline"]


def test_new_master_resume_rebuilds_current_matches(tmp_path, monkeypatch):
    import json, shutil, time, pipeline
    master = str(tmp_path / "master.json")
    shutil.copyfile(os.path.join(os.path.dirname(__file__), "profile", "master_resume.json"), master)
    cfg = dict(CFG, master_resume=master)
    monkeypatch.chdir(tmp_path)
    t = Tracker(str(tmp_path / "m.db"), 5)
    jid = t.add(dict(url="https://m/1", company="Acme", title="Forward Deployed Engineer",
                     description="Kubernetes, Python, RAG", score=10, sponsorship="sponsors"), status="notified")
    app = t.add(dict(url="https://m/2", company="Beta", title="Forward Deployed Engineer",
                     description="Python", score=10, sponsorship="sponsors"), status="applied")
    pipeline.make_resume(dict(t.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()), jid, cfg, t)
    assert pipeline.refresh_after_master_change(t, cfg) == 0                 # resume is newer than the master
    m = json.load(open(master))
    m["skills"]["Cloud and DevOps"] = ["Kubernetes"]                           # upload a new master
    json.dump(m, open(master, "w"))
    os.utime(master, (time.time() + 60, time.time() + 60))
    assert pipeline.refresh_after_master_change(t, cfg) == 1                 # only the current match
    row = t.db.execute("SELECT score, matched, resume_file FROM jobs WHERE id=?", (jid,)).fetchone()
    assert "kubernetes" in row["matched"] and row["resume_file"] and row["score"] > 10
    assert t.db.execute("SELECT resume_file FROM jobs WHERE id=?", (app,)).fetchone()[0] is None


def test_employer_link_preferred_over_job_sites():
    import fetchers, links
    j = dict(job_title="Remote AI Forward Deployed Engineer", employer_name="Hard Rock Digital LLC",
             employer_website="https://www.hardrock.bet",
             job_apply_link="https://www.linkedin.com/jobs/view/123",
             apply_options=[{"apply_link": "https://www.ziprecruiter.com/c/x", "is_direct": False},
                            {"apply_link": "https://jobs.lever.co/hardrockdigital/abc", "is_direct": False}])
    job = fetchers._jsearch_job(j)
    assert job["url"] == "https://jobs.lever.co/hardrockdigital/abc" and len(job["links"]) == 3
    assert links.kind("https://careers.hardrock.bet/job/1", "Hard Rock Digital", "https://www.hardrock.bet") == "company"
    assert links.kind("https://www.indeed.com/viewjob?jk=1", "Hard Rock Digital") == "jobsite"
    only_sites = dict(j, apply_options=[])
    assert fetchers._jsearch_job(only_sites)["url"].startswith("https://www.linkedin.com")   # nothing better known


def test_recruitee_widget_on_company_site_links_to_company_page():
    import fetchers, find_careers as fc
    page = ('<script>var widget = new RTWidget({ \n "companies": [\n   62125\n ],\n "detailsMode": "popup"});</script>')

    class C:
        def get(self, url, params=None):
            if "api.recruitee.com/c/62125" in url:
                return _R({"offers": [{"careers_url": "https://hardrockdigital.recruitee.com/o/devops"}]})
            if url == "https://hardrockdigital.recruitee.com/api/offers/":
                return _R({"offers": [{"id": 2813877, "title": "Senior DevOps Engineer", "location": "Hollywood, Florida, United States",
                                       "careers_url": "https://hardrockdigital.recruitee.com/o/devops",
                                       "published_at": "2026-11-03 16:43:55 UTC", "description": "<p>Ops</p>"}]})
            raise AssertionError(url)
    w = fc.recruitee_widget(C(), page, "https://www.hardrockdigital.com/#careers")
    assert w == dict(ats="recruitee", slug="hardrockdigital", careers_page="https://www.hardrockdigital.com/")
    jobs = list(fetchers.recruitee(C(), "hardrockdigital", "Hard Rock Digital", w["careers_page"]))
    assert jobs[0]["url"] == "https://www.hardrockdigital.com/#job-2813877"


def test_job_site_listing_traced_to_company_posting(tmp_path, monkeypatch):
    import pipeline, resolver, fetchers
    assert resolver.same_title("Remote AI Forward Deployed Engineer Enterprise", "AI Forward Deployed Engineer - Enterprise")
    assert not resolver.same_title("Forward Deployed Engineer", "Senior DevOps Engineer")
    board = dict(name="Acme AI", ats="lever", slug="acme")

    def fake_fetch(client, c, seen, wanted):
        return [dict(company="Acme AI", title="Forward Deployed Engineer", url="https://jobs.lever.co/acme/1",
                     location="US", description="x")] if c is board or c.get("slug") == "acme" else []
    monkeypatch.setattr(fetchers, "_fetch_company", fake_fetch)
    import find_careers
    monkeypatch.setattr(find_careers, "find", lambda name, client, cfg, domains=None:
                        dict(name=name, ats="lever", slug="acme") if name == "Newco" else None)
    comps = [board]
    j = dict(company="Acme AI", title="Remote Forward Deployed Engineer", url="https://www.linkedin.com/jobs/1")
    assert resolver.resolve(j, comps, CFG, client=object()) == "https://jobs.lever.co/acme/1"
    j2 = dict(company="Newco", title="Forward Deployed Engineer", url="https://www.indeed.com/x", employer_site="https://newco.com")
    assert resolver.resolve(j2, comps, CFG, client=object(), learned_path=str(tmp_path / "l.json")) == "https://jobs.lever.co/acme/1"
    assert any(c["name"] == "Newco" for c in comps)                         # now watched
    j3 = dict(company="Closedco", title="Forward Deployed Engineer", url="https://www.indeed.com/y")
    assert resolver.resolve(j3, comps, CFG, client=object()) is None


def test_existing_job_site_matches_are_traced_or_moved(tmp_path, monkeypatch):
    import pipeline, resolver
    t = Tracker(str(tmp_path / "e.db"), 5)
    a = t.add(dict(url="https://www.linkedin.com/jobs/1", company="Acme", title="FDE"), status="notified")
    b = t.add(dict(url="https://www.indeed.com/2", company="Gone", title="FDE"), status="notified")
    c = t.add(dict(url="https://careers.acme.com/9", company="Acme", title="FDE II"), status="notified")
    monkeypatch.setattr(resolver, "resolve", lambda job, companies, cfg, client=None:
                        "https://jobs.lever.co/acme/7" if job["company"] == "Acme" else None)
    assert pipeline.resolve_existing(t, CFG, [], state_path=str(tmp_path / "r.json")) == 2
    rows = {r["id"]: (r["url"], r["status"]) for r in t.db.execute("SELECT id, url, status FROM jobs")}
    assert rows[a] == ("https://jobs.lever.co/acme/7", "notified")
    assert rows[b][1] == "notified"                     # real employer: stays listed, flagged "Not confirmed"
    assert rows[c] == ("https://careers.acme.com/9", "notified")             # company link: untouched
    d = t.add(dict(url="https://www.indeed.com/3", company="Rise Technical Recruitment", title="FDE"), status="notified")
    e = t.add(dict(url="https://www.indeed.com/4", company="Gone2", title="FDE"), status="notified")
    pipeline.resolve_existing(t, dict(CFG, unconfirmed_jobs="off"), [], state_path=str(tmp_path / "r2.json"))
    pipeline.resolve_existing(t, CFG, [], state_path=str(tmp_path / "r3.json"))
    st = {r["id"]: r["status"] for r in t.db.execute("SELECT id, status FROM jobs")}
    assert st[d] == "jobsite_only" and st[e] == "jobsite_only"   # a recruiter; and with the flag turned off


def test_open_posting_never_goes_to_a_job_site():
    from links import is_recruiter, open_url
    assert open_url("https://www.linkedin.com/jobs/view/1", "OpenAI", "Forward Deployed Engineer").startswith(
        "https://www.google.com/search?q=")
    assert open_url("https://jobs.ashbyhq.com/openai/1", "OpenAI", "FDE") == "https://jobs.ashbyhq.com/openai/1"
    assert is_recruiter("Clearance Jobs") and is_recruiter("FDE Times") and is_recruiter("Rise Technical Recruitment Ltd")
    assert is_recruiter("Acme", "We are hiring on behalf of our client, a fintech.")
    assert not is_recruiter("OpenAI") and not is_recruiter("The Home Depot") and not is_recruiter("Salesforce, Inc.")


def test_ats_reads_keywords_from_the_posting_itself():
    from ats import posting_terms
    terms = posting_terms("Forward Deployed Engineer",
                          "About us. We build things.\nRequirements: Python, TypeScript and LLMs. Kubernetes on AWS.\n"
                          "Preferred: GraphQL, Node.js. Acme is an EEO employer in the US.", "Acme")
    assert terms["typescript"] == 2 and terms["kubernetes"] == 2 and terms["graphql"] == 1
    assert "llm" in terms and "llms" not in terms                      # one skill, counted once
    assert "eeo" not in terms and "us" not in terms and "acme" not in terms


def test_auto_add_missing_keywords(tmp_path):
    from tailor import tailor
    job = dict(title="Forward Deployed Engineer", company="Acme",
               description="Requirements: Python, TypeScript, Kubernetes and MCP. Located in Austin, TX.")
    _, before = tailor(job, 1, data_dir=str(tmp_path), auto_keywords=False)
    assert {"TypeScript", "Kubernetes", "MCP"} <= set(before["missing_terms"]) and "TX" not in before["missing_terms"]
    path, after = tailor(job, 2, data_dir=str(tmp_path), auto_keywords=True)
    import docx, json
    text = "\n".join(p.text for p in docx.Document(path).paragraphs)
    assert "Additional Skills: " in text and "TypeScript" in text and "MCP" in text
    assert not {"TypeScript", "Kubernetes", "MCP"} & set(after["missing_terms"])
    assert after["score"] > before["score"]
    st = json.load(open(tmp_path / "resumes" / "2" / "tailored.json"))
    assert "Kubernetes" in st["added_keywords"]


def test_rate_limited_systems_are_paused_and_dead_boards_strike_out(monkeypatch):
    import sys, time, types, fetchers

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(
        Client=FakeClient, Timeout=lambda *a, **k: None, Limits=lambda *a, **k: None))

    class Err(Exception):
        def __init__(self, code): super().__init__(f"HTTP {code}"); self.response = types.SimpleNamespace(status_code=code)

    calls = {"workable": 0, "lever": 0}

    def workable(client, slug, company):
        calls["workable"] += 1
        raise Err(429)
        yield

    def lever(client, slug, company):
        calls["lever"] += 1
        raise Err(404)
        yield
    monkeypatch.setitem(fetchers.FETCHERS, "workable", workable)
    monkeypatch.setitem(fetchers.FETCHERS, "lever", lever)
    monkeypatch.setattr(fetchers, "COOLDOWN", {})
    monkeypatch.setattr(fetchers, "STRIKES", {})
    monkeypatch.setitem(fetchers.LIMITS, "workable", (2, 0.0))
    comps = [dict(name=f"W{i}", ats="workable", slug=f"w{i}") for i in range(5)] + [dict(name="Gone", ats="lever", slug="gone")]
    for _ in range(4):
        fetchers._LAST_RUN.clear()
        list(fetchers.fetch_all(comps))
    assert fetchers.COOLDOWN["workable"] > time.time() + 800          # paused for ~15 minutes
    assert calls["workable"] <= 5                                      # not hammered again while paused
    assert calls["lever"] == 3 and fetchers.STRIKES[("lever", "gone")][1] > time.time() + 80000   # once a day now


def test_find_workday_sites_from_robots():
    import find_workday as fw
    robots = ("Sitemap: https://acme.wd3.myworkdayjobs.com/AcmeCareers/siteMap.xml\n\nUser-agent: *\n"
              "Allow: /AcmeCareers/\nDisallow: /talentcommunity/\nAllow: /AcmeUniversity/\n")
    assert fw.sites_from_robots(robots) == ["AcmeCareers"]
    assert fw.tenants("Goldman Sachs")[0] == "goldmansachs"

    class R:
        def __init__(self, code, text="", data=None): self.status_code, self.text, self._d = code, text, data
        def json(self): return self._d

    class C:
        def get(self, url):
            return R(200, robots) if url == "https://acme.wd3.myworkdayjobs.com/robots.txt" else R(422, '{"httpStatus":422}')
        def post(self, url, json=None):
            return R(200, data={"total": 12}) if "/wday/cxs/acme/AcmeCareers/jobs" in url else R(404, data={})
    assert fw.find("Acme", C()) == dict(name="Acme", ats="workday", host="acme.wd3.myworkdayjobs.com",
                                        tenant="acme", site="AcmeCareers")
    assert fw.find("Nobody Here", C()) is None


def test_unclear_location_decided_by_description(tmp_path):
    from locations import where_with_description as w
    assert w("Remote", "Join our team in London. We are a UK fintech.") == "non_us"
    assert w("2 Locations", "Offices in Bengaluru and Hyderabad, India.") == "non_us"
    assert w("Remote", "This role is Remote - EMEA.") == "non_us"
    assert w("Remote", "Remote (US). Must be authorized to work in the US.") == "us"
    assert w("Remote", "Our HQ is in San Francisco; we also have a London office.") == "unclear"
    assert w("Austin, TX", "We also hire in India.") == "us"
    import pipeline
    t = Tracker(str(tmp_path / "l.db"), 5)
    a = t.add(dict(url="https://x/1", company="A", title="FDE", location="Remote",
                   description="Based in Berlin, Germany."), status="notified")
    b = t.add(dict(url="https://x/2", company="B", title="FDE", location="New York, NY", description="x"),
              status="notified")
    c = t.add(dict(url="https://x/3", company="C", title="FDE", location="Remote", description="Great team."),
              status="notified")
    assert pipeline.recheck_locations(t, CFG) == 2
    st = {r["id"]: r["status"] for r in t.db.execute("SELECT id, status FROM jobs")}
    assert st[a] == "outside_us" and st[b] == "notified" and st[c] == "location_unclear"


# ---- link check / name cleanup ----
class _Resp:
    def __init__(self, code, data=None, text=""):
        self.status_code, self._d, self.text = code, data, text

    def json(self):
        if self._d is None:
            raise ValueError
        return self._d


class _Client:
    def __init__(self, routes):
        self.routes = routes

    def request(self, method, url, headers=None):
        return self.routes.get(url, _Resp(404))

    def get(self, url, **k):
        return self.request("GET", url)


def test_linkcheck_ashby_open_closed_mismatch():
    import linkcheck
    api = "https://api.ashbyhq.com/posting-api/job-board/acme"
    c = _Client({api: _Resp(200, {"jobs": [{"id": "a1", "title": "Forward Deployed Engineer"},
                                           {"id": "b2", "title": "Account Executive"}]})})
    assert linkcheck.check("https://jobs.ashbyhq.com/acme/a1", "Forward Deployed Engineer", c)[0] == "open"
    assert linkcheck.check("https://jobs.ashbyhq.com/acme/zz", "Forward Deployed Engineer", c)[0] == "closed"
    assert linkcheck.check("https://jobs.ashbyhq.com/acme/b2", "Forward Deployed Engineer", c)[0] == "mismatch"


def test_linkcheck_workday_and_page():
    import linkcheck
    wd = ("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/job/New-York-NY/"
          "Forward-Deployed-Engineer_R1")
    c = _Client({wd: _Resp(200, {"jobPostingInfo": {"title": "Forward Deployed Engineer"}}),
                 "https://x.example/job/1": _Resp(200, text="<title>Sorry</title><p>This job is no longer available</p>"),
                 "https://x.example/job/2": _Resp(200, text="<title>Forward Deployed Engineer | X</title>")})
    url = "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/New-York-NY/Forward-Deployed-Engineer_R1"
    assert linkcheck.check(url, "Forward Deployed Engineer", c)[0] == "open"
    assert linkcheck.check(url.replace("_R1", "_R9"), "Forward Deployed Engineer", c)[0] == "closed"
    assert linkcheck.check("https://x.example/job/1", "Forward Deployed Engineer", c)[0] == "closed"
    assert linkcheck.check("https://x.example/job/2", "Forward Deployed Engineer", c)[0] == "open"


def test_clean_names_and_duplicates(tmp_path):
    from db import Tracker
    from pipeline import clean_company, clean_title, tidy_listed
    assert clean_company("Aecom2 (smartrecruiters)") == "Aecom"
    assert clean_company("Allcareers Quanta") == "Quanta"
    assert clean_company("Point72") == "Point72"
    assert clean_title("Title Forward Deployed Engineer") == "Forward Deployed Engineer"
    t = Tracker(str(tmp_path / "j.db"))
    a = t.add({"url": "https://a/1", "company": "Aecom2 (smartrecruiters)", "title": "FDE"}, status="notified")
    b = t.add({"url": "https://a/2", "company": "Aecom2", "title": "FDE"}, status="notified")
    assert tidy_listed(t) == 1
    st = {r["id"]: (r["status"], r["company"]) for r in t.db.execute("SELECT id, status, company FROM jobs")}
    assert st[a] == ("notified", "Aecom") and st[b][0] == "duplicate"


def test_undated_posting_on_first_read_is_old(tmp_path, monkeypatch):
    import pipeline
    from db import Tracker
    monkeypatch.setattr(pipeline, "load_boards_read", lambda: set())          # board never read before
    job = {"company": "Acme", "title": "Forward Deployed Engineer", "url": "https://acme/1",
           "location": "New York, NY", "posted_at": None, "description": "We sponsor H-1B visas. Python."}
    monkeypatch.setattr(pipeline, "fetch_all", lambda *a, **k: iter([dict(job)]))
    cfg = yaml.safe_load(open("config.yaml"))
    t = Tracker(str(tmp_path / "j.db"), 2)
    pipeline.run_once(cfg, [], t, use_aggregators=False)
    assert t.db.execute("SELECT status FROM jobs").fetchone()[0] == "too_old"


def _master_for_rewrite():
    return {"name": "Test Person", "contact": "a@b.com | 555", "summary": "Python developer with 5+ years. Builds APIs.",
            "skills": {"Languages": ["Python", "SQL"], "Backend": ["FastAPI", "REST APIs"]},
            "experience": [{"title": "Developer", "company": "Acme", "start": "2021", "end": "Present",
                            "bullets": ["Built REST APIs with FastAPI and Python for 12 partner systems.",
                                        "Wrote SQL reports for the finance team every month.",
                                        "Fixed production bugs in the Python services quickly."]}],
            "education": []}


def test_claude_rewrite_keeps_only_checked_lines():
    import rewrite
    from tailor import load_profile_from, select
    m = _master_for_rewrite()
    job = {"title": "Forward Deployed Engineer", "company": "X",
           "description": "Build REST APIs with Python and FastAPI for customer integrations. Kubernetes a plus."}
    owned = load_profile_from(m)
    reply = json.dumps({
        "headline": "Forward Deployed Engineer",
        "summary": "Python developer with 5+ years of experience building REST APIs with FastAPI for customer "
                   "integrations, working closely with partner teams to deliver reliable backend services and "
                   "fix production issues quickly.",
        "jobs": [{"job": 0, "bullets": [
            {"from": ["0.0"], "text": "Built REST APIs with Python and FastAPI to integrate 12 partner systems for customer-facing workflows."},
            {"from": ["0.1"], "text": "Deployed SQL reporting services on Kubernetes for 40 finance stakeholders every month."},
        ]}]})
    out = rewrite.rewrite(select(m, job, 0), m, job, owned, call=lambda p: reply)
    b = out["experience"][0]["bullets"]
    assert b[0].startswith("Built REST APIs with Python and FastAPI to integrate 12")
    assert b[1] in m["experience"][0]["bullets"]          # new number + Kubernetes: an original bullet instead
    assert out["headline"] == "Forward Deployed Engineer" and "customer integrations" in out["summary"]
    assert out["rewritten"]["replaced_with_original"] == 1
    assert out["experience"][0]["title"] == "Developer" and out["experience"][0]["start"] == "2021"


def test_tailor_uses_claude_version(tmp_path, monkeypatch):
    import rewrite
    import tailor
    m = _master_for_rewrite()
    mp = tmp_path / "m.json"
    mp.write_text(json.dumps(m))
    job = {"title": "Forward Deployed Engineer", "company": "X", "description": "Python FastAPI REST APIs."}
    monkeypatch.setattr(tailor, "_claude_ready", lambda: True)
    monkeypatch.setattr(rewrite, "_call", lambda model, p: json.dumps({"headline": "Forward Deployed Engineer"}))
    monkeypatch.setattr(rewrite, "USAGE_FILE", str(tmp_path / "u.json"))
    tailor.tailor(job, 1, str(mp), str(tmp_path), auto_keywords=False)
    st = json.load(open(tmp_path / "resumes" / "1" / "tailored.json"))
    assert st["headline"] == "Forward Deployed Engineer" and "rewritten" in st


class _FakeResp:
    def __init__(self, data=None, text="", code=200):
        self._d, self.text, self.status_code = data, text, code

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_amazon_reader():
    from fetchers_more import amazon

    class C:
        def get(self, url, params=None):
            return _FakeResp({"jobs": [{"title": "Forward Deployed Engineer, AWS", "job_path": "/en/jobs/123/fde",
                                 "posted_date": "November 3, 2026", "normalized_location": "Seattle, Washington, USA",
                                 "description": "<p>Build with customers.</p>"}]})
    jobs = list(amazon(C(), {"name": "Amazon", "ats": "amazon"}))
    assert jobs[0]["url"] == "https://www.amazon.jobs/en/jobs/123/fde" and jobs[0]["posted_at"] == "November 3, 2026"
    assert "Build with customers." in jobs[0]["description"] and len(jobs) == 1


def test_successfactors_reader():
    from fetchers_more import successfactors
    page = ('<tr><td><a href="/ey/job/New-York-AI-Forward-Deployed-Engineer-NY-10001/1234501/" class="jobTitle-link">'
            'AI Forward Deployed Engineer - Senior</a></td><td><span class="jobLocation">New York, NY, US</span>'
            '<span class="jobDate">Nov 4, 2026</span></td></tr>')

    class C:
        def get(self, url, params=None):
            return _FakeResp(text=page if "/search/" in url else '<span class="jobdescription">Python, LLMs.</span></span>')
    jobs = list(successfactors(C(), {"name": "EY", "ats": "successfactors", "host": "careers.ey.com", "site": "ey"}))
    assert jobs[0]["url"] == "https://careers.ey.com/ey/job/New-York-AI-Forward-Deployed-Engineer-NY-10001/1234501/"
    assert jobs[0]["title"] == "AI Forward Deployed Engineer - Senior" and jobs[0]["location"] == "New York, NY, US"
    assert jobs[0]["posted_at"] == "Nov 4, 2026" and "Python" in jobs[0]["description"]


def test_release_unconfirmed(tmp_path, monkeypatch):
    import pipeline
    from dates import to_iso
    monkeypatch.setattr(pipeline, "make_resume", lambda *a, **k: None)
    t = Tracker(str(tmp_path / "u.db"), 2)
    now = to_iso(time.time())
    ok = t.add(dict(url="https://www.linkedin.com/jobs/1", company="OpenAI", title="Forward Deployed Engineer",
                    location="San Francisco, CA", posted_at=now), status="jobsite_only")
    rec = t.add(dict(url="https://www.linkedin.com/jobs/2", company="FDE Times", title="Forward Deployed Engineer",
                     location="New York, NY", posted_at=now), status="jobsite_only")
    ind = t.add(dict(url="https://www.linkedin.com/jobs/3", company="Saxecap", title="Forward Deployed AI Engineer India",
                     location="", posted_at=now), status="jobsite_only")
    cfg = dict(CFG, unconfirmed_jobs="flag", max_posting_age_days=3)
    assert pipeline.release_unconfirmed(t, cfg) == 1
    st = {r["id"]: r["status"] for r in t.db.execute("SELECT id, status FROM jobs")}
    assert st[ok] == "notified" and st[rec] == "jobsite_only" and st[ind] == "jobsite_only"
