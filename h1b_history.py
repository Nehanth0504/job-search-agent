"""Employer H-1B filing history, built from the Department of Labor's public LCA disclosure data.

Used for postings that do not mention sponsorship either way: if the employer has a strong
record of certified H-1B filings (especially tech roles and employer *transfers*, which is what
an H-1B holder needs), the posting is let through and flagged as "inferred, confirm with the
recruiter".

Build the index once, from the LCA disclosure files you download from the DOL OFLC
performance-data page (one file per fiscal year; pass the latest cumulative file for a year,
not every quarterly file):

    python h1b_history.py build --out data/h1b_index.db  LCA_FY2025.xlsx  LCA_FY2026_Q1.csv
    python h1b_history.py lookup "Acme AI" --db data/h1b_index.db

Reading .xlsx needs `pip install openpyxl`; CSV needs nothing extra.

Limits you should know about:
  * A certified LCA is a step before the petition itself, not proof of an approved H-1B.
  * Matching is by normalised legal name. Subsidiaries filing under a different name
    (e.g. "AMAZON.COM SERVICES") will not match "Amazon" unless added to ALIASES in db.py;
    unmatched employers are held for review, never auto-passed.
"""
import argparse
import csv
import os
import re
import sqlite3
import sys

from db import normalize_company

TECH_TITLE = re.compile(
    r"software|machine learning|\bml\b|\bai\b|data scientist|data engineer|full stack|backend|python", re.I
)


def _iter_rows(path):
    if path.lower().endswith(".xlsx"):
        try:
            import openpyxl
        except ImportError:
            sys.exit("Reading .xlsx needs: pip install openpyxl  (or save the file as CSV)")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)   # streams: big files stay small in memory
        rows = wb.worksheets[0].iter_rows(values_only=True)
        head = [str(c or "").strip().upper() for c in next(rows)]
        for row in rows:
            yield {k: ("" if v is None else str(v)) for k, v in zip(head, row)}
        wb.close()
    else:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            for rec in csv.DictReader(f):
                yield {(k or "").strip().upper(): (v or "") for k, v in rec.items()}


def _num(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def build_index(paths, out_db):
    agg, seen = {}, set()  # company_norm -> [display, certified, tech, transfers]
    for p in paths:
        first = True
        for r in _iter_rows(p):
            if first:
                missing = [c for c in ("CASE_STATUS", "EMPLOYER_NAME") if c not in r]
                if missing:
                    raise ValueError(f"{p}: missing columns {missing}; found {sorted(r)[:15]}...")
                first = False
            if r.get("CASE_STATUS", "").strip().upper() != "CERTIFIED":  # excludes "Certified - Withdrawn"
                continue
            if r.get("VISA_CLASS", "H-1B").strip().upper() != "H-1B":  # excludes H-1B1, E-3
                continue
            case = r.get("CASE_NUMBER", "").strip()
            if case:  # cumulative quarterly files overlap; count each case once
                if case in seen:
                    continue
                seen.add(case)
            name = r.get("EMPLOYER_NAME", "").strip()
            if not name:
                continue
            n = max(1, _num(r.get("TOTAL_WORKER_POSITIONS") or r.get("TOTAL_WORKERS"), 1))
            is_tech = r.get("SOC_CODE", "").strip().startswith("15-") or bool(TECH_TITLE.search(r.get("JOB_TITLE", "")))
            a = agg.setdefault(normalize_company(name), [name, 0, 0, 0])
            a[1] += n
            a[2] += n if is_tech else 0
            a[3] += _num(r.get("CHANGE_EMPLOYER"))  # positions filed as a change of employer (transfers)

    os.makedirs(os.path.dirname(out_db) or ".", exist_ok=True)
    db = sqlite3.connect(out_db)
    db.executescript(
        "DROP TABLE IF EXISTS employers;"
        "CREATE TABLE employers(company_norm TEXT PRIMARY KEY, display TEXT, certified INT, tech INT, transfers INT);"
    )
    db.executemany("INSERT INTO employers VALUES (?,?,?,?,?)", [(k, *v) for k, v in agg.items()])
    db.commit()
    db.close()
    return len(agg)


class H1BHistory:
    def __init__(self, db_path, min_certified=25, min_tech=5, min_transfers=3):
        self.db = sqlite3.connect(db_path)
        self.min_certified, self.min_tech, self.min_transfers = min_certified, min_tech, min_transfers

    @classmethod
    def open(cls, cfg: dict):
        path = (cfg or {}).get("data_file")
        if not path or not os.path.exists(path):
            return None
        return cls(path, cfg.get("min_certified", 25), cfg.get("min_tech", 5), cfg.get("min_transfers", 3))

    GENERIC = ("pbc", "opco", "usa", "us", "services", "service", "bank", "na", "lp", "associates", "development",
               "center", "data", "web", "and", "co", "international", "america", "americas", "north", "technology",
               "wealth", "securities", "capital", "global", "platforms", "operations", "management")

    @classmethod
    def _generic_tail(cls, tail: str) -> bool:
        """True if `tail` (spaces removed) is made only of generic legal/division words, e.g. 'bankusa'."""
        ok = [True] + [False] * len(tail)
        for i in range(len(tail)):
            if ok[i]:
                for w in cls.GENERIC:
                    if tail.startswith(w, i):
                        ok[i + len(w)] = True
        return ok[len(tail)]

    def _find(self, company: str):
        """Exact normalised name first; otherwise add up the same employer's other filing names
        ('Goldman Sachs' + 'Goldman Sachs & Co', 'Walmart' + 'Wal Mart Associates')."""
        norm = normalize_company(company)
        row = self.db.execute("SELECT display, certified, tech, transfers FROM employers WHERE company_norm=?",
                              (norm,)).fetchone()
        if row or len(norm) < 4:
            return row
        sq = norm.replace(" ", "")
        first = norm.split()[0][:3]
        shown, c, t, tr = None, 0, 0, 0
        for n2, d, c2, t2, tr2 in self.db.execute(
                "SELECT company_norm, display, certified, tech, transfers FROM employers WHERE company_norm LIKE ?",
                (first + "%",)):
            s2 = n2.replace(" ", "").replace("&", "and")
            if s2.startswith(sq) and self._generic_tail(s2[len(sq):]):
                shown = shown or d
                c, t, tr = c + c2, t + t2, tr + tr2
        return (shown, c, t, tr) if shown else None

    def lookup(self, company: str) -> dict:
        row = self._find(company)
        if not row:
            return dict(ok=False, evidence="no H-1B filing history found under this company name")
        display, certified, tech, transfers = row
        ok = certified >= self.min_certified and tech >= self.min_tech and transfers >= self.min_transfers
        evidence = (f"Filing history (not stated in the posting): {display} has {certified} certified H-1B positions "
                    f"in the loaded DOL data, {tech} in tech roles, {transfers} for employer changes (transfers).")
        return dict(ok=ok, certified=certified, tech=tech, transfers=transfers, evidence=evidence)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("files", nargs="+")
    b.add_argument("--out", default="data/h1b_index.db")
    q = sub.add_parser("lookup")
    q.add_argument("company")
    q.add_argument("--db", default="data/h1b_index.db")
    a = ap.parse_args()
    if a.cmd == "build":
        print("employers indexed:", build_index(a.files, a.out))
    else:
        print(H1BHistory(a.db).lookup(a.company))
