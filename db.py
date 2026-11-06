"""SQLite tracker with the per-company cap.

Slot rule: at most MAX jobs per company may be in an "active" status
(notified / review_queue / applied). A new job takes a free slot, or bumps the
lowest-scoring *notified-but-not-applied* job if it scores higher. Applied jobs are
never bumped (they count permanently, so you never over-apply to one employer).
"""
import re
import sqlite3
import time

# Statuses that use up one of a company's slots. Anything you applied to stays counted for good,
# whatever the outcome; "skipped" is the only way for you to give a slot back.
ACTIVE = ("notified", "review_queue", "applied", "interview", "offer", "rejected_by_employer", "withdrawn")
BUMPABLE = ("notified", "review_queue")

SUFFIXES = r"\b(incorporated|inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|gmbh|plc|holdings|group|technologies|labs)\b\.?"
ALIASES = {  # extend as you see parent/subsidiary duplicates
    "alphabet": "google",
    "google deepmind": "google",
    "meta platforms": "meta",
    "facebook": "meta",
    "amazon web services": "amazon",
    "aws": "amazon",
    "amazon com": "amazon",
    "international business machines": "ibm",
    "ernst & young": "ey",
    "ernst young": "ey",
}


def normalize_company(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"[^\w\s&]", " ", n)
    n = re.sub(SUFFIXES, " ", n)
    n = " ".join(n.split())
    return ALIASES.get(n, n)


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  url TEXT UNIQUE NOT NULL,
  company TEXT NOT NULL,
  company_norm TEXT NOT NULL,
  title TEXT NOT NULL,
  location TEXT,
  posted_at TEXT,
  description TEXT,
  sponsorship TEXT,
  sponsorship_evidence TEXT,
  matched TEXT,
  gaps TEXT,
  score REAL,
  status TEXT NOT NULL DEFAULT 'new',
  first_seen REAL NOT NULL,
  updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_company ON jobs(company_norm, status);
CREATE TABLE IF NOT EXISTS applications (
  job_id INTEGER PRIMARY KEY,
  applied_at REAL,
  follow_up TEXT,
  notes TEXT,
  portal_id INTEGER,
  updated REAL
);
CREATE TABLE IF NOT EXISTS portals (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT,
  username TEXT,
  password_enc TEXT,
  notes TEXT,
  created REAL
);
CREATE TABLE IF NOT EXISTS vault_meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Tracker:
    def __init__(self, path="jobs.db", max_per_company=2):
        self.db = sqlite3.connect(path, timeout=30)   # the dashboard and the agent share this file
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
        for col, typ in (("ats_score", "REAL"), ("ats_report", "TEXT"), ("resume_file", "TEXT"),
                         ("apply_links", "TEXT"), ("employer_site", "TEXT")):
            if col not in have:                      # upgrade an older jobs.db in place, data is kept
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
        self.db.commit()
        self.max = max_per_company

    def seen(self, url: str) -> bool:
        return self.db.execute("SELECT 1 FROM jobs WHERE url=?", (url,)).fetchone() is not None

    def seen_urls(self) -> set:
        """Every stored posting URL (a snapshot, safe to read from other threads)."""
        return {r[0] for r in self.db.execute("SELECT url FROM jobs")}

    def seen_posting(self, company: str, title: str) -> bool:
        """Same company and title already stored (used to drop copies of a job found via another source)."""
        return self.db.execute("SELECT 1 FROM jobs WHERE company_norm=? AND lower(title)=lower(?)",
                               (normalize_company(company), title)).fetchone() is not None

    def add(self, job: dict, status="new") -> int:
        now = time.time()
        cur = self.db.execute(
            """INSERT OR IGNORE INTO jobs
               (url, company, company_norm, title, location, posted_at, description,
                sponsorship, sponsorship_evidence, matched, gaps, score, status, first_seen, updated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job["url"], job["company"], normalize_company(job["company"]), job["title"],
             job.get("location"), job.get("posted_at"), job.get("description"),
             job.get("sponsorship"), job.get("sponsorship_evidence"),
             ", ".join(job.get("matched") or []), ", ".join(job.get("gaps") or []), job.get("score"),
             status, now, now),
        )
        if cur.lastrowid and (job.get("links") or job.get("employer_site")):
            import json
            self.db.execute("UPDATE jobs SET apply_links=?, employer_site=? WHERE id=?",
                            (json.dumps(job.get("links") or []), job.get("employer_site") or None, cur.lastrowid))
        self.db.commit()
        return cur.lastrowid

    def set_match(self, job_id: int, score: float, matched, gaps):
        """Match score and skill lists, recomputed from the job's own tailored resume after you edit it."""
        self.db.execute("UPDATE jobs SET score=?, matched=?, gaps=?, updated=? WHERE id=?",
                        (score, ", ".join(matched), ", ".join(gaps), time.time(), job_id))
        self.db.commit()

    def set_ats(self, job_id: int, res: dict, filename: str):
        import json
        self.db.execute("UPDATE jobs SET ats_score=?, ats_report=?, resume_file=?, updated=? WHERE id=?",
                        (res["score"], json.dumps(res), filename, time.time(), job_id))
        self.db.commit()

    def set_status(self, job_id: int, status: str):
        self.db.execute("UPDATE jobs SET status=?, updated=? WHERE id=?", (status, time.time(), job_id))
        self.db.commit()

    def claim_slot(self, job_id: int):
        """Try to give this job one of the company's slots.

        Returns (granted: bool, bumped_job_id: int | None).
        """
        job = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        marks = ",".join("?" * len(ACTIVE))
        active = self.db.execute(
            f"SELECT id, score, status FROM jobs WHERE company_norm=? AND id!=? AND status IN ({marks})",
            (job["company_norm"], job_id, *ACTIVE),
        ).fetchall()

        if len(active) < self.max:
            self.set_status(job_id, "notified")
            return True, None

        bumpable = [r for r in active if r["status"] in BUMPABLE]
        if bumpable:
            worst = min(bumpable, key=lambda r: r["score"] or 0)
            if (job["score"] or 0) > (worst["score"] or 0):
                self.set_status(worst["id"], "bumped")
                self.set_status(job_id, "notified")
                return True, worst["id"]

        self.set_status(job_id, "capped")
        return False, None
