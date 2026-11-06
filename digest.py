"""End-of-day summary email: the matches found today (best first) and what you applied to today.

Sent once a day, after `daily_summary.hour` (local time on the server, config.yaml).
"""
import json
import os
import sqlite3
from datetime import datetime

STATE = "data/digest_state.json"


def build(db_path: str, day_start: float, today: str, base_url: str = None) -> tuple:
    """(subject, body, counts) for the jobs found and applications made since `day_start`."""
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    new = db.execute("SELECT * FROM jobs WHERE status IN ('notified','applied','interview','offer') "
                     "AND first_seen >= ? ORDER BY score DESC", (day_start,)).fetchall()
    applied = db.execute("SELECT j.company, j.title FROM applications a JOIN jobs j ON j.id=a.job_id "
                         "WHERE a.applied_at >= ? ORDER BY a.applied_at", (day_start,)).fetchall()
    waiting = db.execute("SELECT COUNT(*) FROM jobs WHERE status='notified'").fetchone()[0]
    db.close()
    lines = [f"Your job search today, {today}.", "", f"NEW MATCHES TODAY ({len(new)})"]
    for j in new[:50]:
        flag = "" if j["sponsorship"] in ("sponsors", "likely_sponsors") else "  [sponsorship unclear]"
        done = "  [applied]" if j["status"] in ("applied", "interview", "offer") else ""
        lines.append(f"  [{(j['score'] or 0):.0f}] {j['title']} @ {j['company']}{flag}{done}")
        lines.append(f"        apply: {j['url']}" + (f"\n        resume: {base_url.rstrip('/')}/jobs/{j['id']}" if base_url else ""))
    if not new:
        lines.append("  none today")
    lines += ["", f"APPLIED TODAY ({len(applied)})"]
    lines += [f"  {r['company']} - {r['title']}" for r in applied] or ["  none"]
    lines += ["", f"Still waiting on the dashboard: {waiting} new match{'es' if waiting != 1 else ''}."]
    if base_url:
        lines.append(f"Dashboard: {base_url}")
    subject = f"End of day {today}: {len(new)} new match{'es' if len(new) != 1 else ''}, {len(applied)} applied"
    return subject, "\n".join(lines), dict(new=len(new), applied=len(applied))


def maybe_send(cfg: dict, db_path: str = "jobs.db", now: datetime = None, state_path: str = STATE) -> bool:
    """Send today's summary once, after the configured hour. Returns True if sent."""
    dcfg = cfg.get("daily_summary") or {}
    if not dcfg.get("enabled", True):
        return False
    now = now or datetime.now()
    if now.hour < int(dcfg.get("hour", 21)):
        return False
    try:
        state = json.load(open(state_path))
    except (OSError, ValueError):
        state = {}
    today = now.date().isoformat()
    if state.get("last_date") == today:
        return False
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    subject, body, _ = build(db_path, day_start, today, dcfg.get("dashboard_url"))
    from notifier import send_email
    try:
        send_email(subject, body, cfg.get("alert_email"))
    except Exception as e:  # noqa: BLE001
        print(f"[digest] email failed: {e}")
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    json.dump(dict(last_date=today), open(state_path, "w"))
    print(f"[digest] sent: {subject}")
    return True
