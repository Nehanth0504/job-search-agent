"""Email alerts via Gmail SMTP (app password). One email per position.

Credentials come from environment variables, never from code:
  GMAIL_USER          the sending Gmail address
  GMAIL_APP_PASSWORD  a Google "app password" (needs 2-step verification on the account)
If they are missing, alerts fall back to console output so nothing crashes.
"""
import os
import smtplib
from email.message import EmailMessage


def build_message(job: dict, to_addr: str, from_addr: str, bumped_id=None, attachment=None) -> EmailMessage:
    msg = EmailMessage()
    msg["To"], msg["From"] = to_addr, from_addr
    msg["Subject"] = f"[{job['score']:.0f}] {job['title']} @ {job['company']}"
    lines = [
        f"{job['title']} at {job['company']}",
        f"Location: {job.get('location') or 'n/a'}",
        f"Match score: {job['score']}",
        *([f"ATS match of the attached tailored resume: {job['ats_score']}/100"]
          if job.get("ats_score") is not None else []),
        ("Sponsorship: NOT stated in the posting - inferred from this employer's H-1B filing history. "
         "Confirm with the recruiter before applying." if job["sponsorship"] == "likely_sponsors"
         else f"Sponsorship: {job['sponsorship']}"),
    ]
    if job.get("sponsorship_evidence"):
        lines.append(f"Evidence: {job['sponsorship_evidence']}" if job["sponsorship"] == "likely_sponsors"
                     else f'Evidence: "{job["sponsorship_evidence"]}"')
    if job.get("matched"):
        lines.append("Matches your resume: " + ", ".join(job["matched"]))
    if job.get("gaps"):
        lines.append("Not on your resume (review before applying): " + ", ".join(job["gaps"]))
    if bumped_id:
        lines.append(f"This replaces an earlier alert from the same company (job #{bumped_id}).")
    lines += ["", f"Apply: {job['url']}"]
    msg.set_content("\n".join(lines))
    files = attachment if isinstance(attachment, (list, tuple)) else [attachment]
    for path in [p for p in files if p and os.path.exists(p)]:   # tailored resume + cover letter
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype="application",
                               subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                               filename=os.path.basename(path))
    return msg


def send_job_alert(job: dict, to_addr: str, bumped_id=None, attachment=None, smtp_factory=smtplib.SMTP_SSL) -> bool:
    user, pw = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    if not (user and pw):
        print(f"[NOTIFY-console] {job['company']} | {job['title']} | {job['score']} | {job['url']}")
        return False
    msg = build_message(job, to_addr, user, bumped_id, attachment)
    with smtp_factory("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.send_message(msg)
    return True


def send_email(subject: str, body: str, to_addr: str, smtp_factory=smtplib.SMTP_SSL) -> bool:
    """Plain email (used by the end-of-day summary). Console fallback when Gmail is not configured."""
    user, pw = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    if not (user and pw):
        print(f"[EMAIL-console] {subject}\n{body}")
        return False
    msg = EmailMessage()
    msg["To"], msg["From"], msg["Subject"] = to_addr, user, subject
    msg.set_content(body)
    with smtp_factory("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.send_message(msg)
    return True
