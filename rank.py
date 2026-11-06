"""Rank jobs by how worth applying they are (0-100). Deterministic, so every number can be explained.

  55%  resume match score            (how much of the posting's skills your resume really has)
  25%  sponsorship certainty         (stated in the posting > inferred from H-1B history > unclear)
  15%  keyword coverage              (skills you have / skills the posting names)
  5%   freshness                     (newer postings are less likely to be flooded or closed)

This ranks fit and practicality. It cannot predict whether an employer will respond or hire.
"""
import time

SPONSOR_PTS = {"sponsors": 100, "likely_sponsors": 70}


def priority(job, now=None):
    now = now or time.time()
    matched = [s for s in (job["matched"] or "").split(", ") if s]
    gaps = [s for s in (job["gaps"] or "").split(", ") if s]
    match = job["score"] or 0
    spons = SPONSOR_PTS.get(job["sponsorship"], 30)
    named = len(matched) + len(gaps)
    cover = 100 * len(matched) / named if named else 60
    age_h = (now - (job["first_seen"] or now)) / 3600
    fresh = 100 if age_h <= 24 else 70 if age_h <= 48 else 40
    score = 0.55 * match + 0.25 * spons + 0.15 * cover + 0.05 * fresh
    why = []
    why.append({"sponsors": "Sponsorship stated", "likely_sponsors": "Sponsorship inferred (confirm with recruiter)"}
               .get(job["sponsorship"], "Sponsorship unclear"))
    if named:
        why.append(f"{len(matched)} of {named} skills on your resume")
    if gaps:
        why.append("missing: " + ", ".join(gaps[:3]) + ("..." if len(gaps) > 3 else ""))
    if age_h <= 24:
        why.append("posted in the last day")
    return round(score, 1), why
