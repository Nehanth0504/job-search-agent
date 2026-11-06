"""Posting dates from every job source, stored one way: ISO 8601 UTC ("2026-11-02T14:05:00Z").

Sources send dates as epoch seconds or milliseconds (Lever, Eightfold), ISO strings with or without a
time zone (Greenhouse, Ashby, SmartRecruiters, JSearch), plain dates (Workable), "2026-11-02 14:05:00 UTC"
(Recruitee) or relative text ("Posted Today", "Posted 3 Days Ago", "Posted 30+ Days Ago": Workday).
Relative text is turned into a date from the moment the posting was read.
"""
import re
from datetime import datetime, timedelta, timezone

ISO = "%Y-%m-%dT%H:%M:%SZ"


def _fmt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(ISO)


def to_iso(value, ref_ts: float = None):
    """Any source's posting date -> ISO UTC string, or None if it cannot be read."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d{9,14}", value.strip())):
        n = float(value)
        if n > 1e12:                                   # milliseconds
            n /= 1000
        return _fmt(datetime.fromtimestamp(n, timezone.utc))
    s = str(value).strip()
    low = s.lower()
    ref = datetime.fromtimestamp(ref_ts, timezone.utc) if ref_ts else datetime.now(timezone.utc)
    if "today" in low or "just posted" in low:
        return _fmt(ref)
    if "yesterday" in low:
        return _fmt(ref - timedelta(days=1))
    m = re.search(r"(\d+)\+?\s*(minute|hour|day|week|month)s?\s+ago", low)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"minute": timedelta(minutes=n), "hour": timedelta(hours=n), "day": timedelta(days=n),
                 "week": timedelta(weeks=n), "month": timedelta(days=30 * n)}[unit]
        return _fmt(ref - delta)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):          # a date with no time: noon UTC, so it shows the same day in the US
        s = s + "T12:00:00Z"
    s2 = re.sub(r"\s*UTC$", "Z", s).replace(" ", "T", 1) if re.match(r"\d{4}-\d{2}-\d{2} \d", s) else s
    s2 = s2.replace("Z", "+00:00")
    try:
        return _fmt(datetime.fromisoformat(s2))
    except ValueError:
        pass
    for f in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return _fmt(datetime.strptime(s, f))
        except ValueError:
            continue
    return None


def parse(iso):
    try:
        return datetime.strptime(iso, ISO).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def describe(iso, now: datetime = None) -> str:
    """'Nov 2, 2026 · today' / 'Oct 30, 2026 · 3 days ago' (shown in your local time)."""
    dt = parse(iso)
    if not dt:
        return ""
    now = now or datetime.now(timezone.utc)
    local = dt.astimezone()
    days = (now.astimezone().date() - local.date()).days
    mins = int((now - dt).total_seconds() // 60)
    if 0 <= mins < 24 * 60:                                   # fine-grained for fresh postings
        ago = "just now" if mins < 1 else f"{mins} min ago" if mins < 60 else f"{mins // 60} h ago"
        return f"{local.strftime('%b')} {local.day}, {local.year} · {ago}"
    ago = ("today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago" if days < 30
           else f"{days // 30} month{'s' if days >= 60 else ''} ago")
    return f"{local.strftime('%b')} {local.day}, {local.year} · {ago}"


def cutoff(days: float, now: float = None) -> str:
    """ISO time `days` ago (postings older than this are not new)."""
    import time
    return _fmt(datetime.fromtimestamp((now or time.time()) - days * 86400, timezone.utc))
