"""Ready-to-paste answers for the questions application forms ask.

Two parts:
  * Standard answers (profile/answers.json): the same for every job. Defaults are filled from your resume
    where possible; check them once on any job's Answers page.
  * Job answers (data/resumes/<id>/answers.json): written for that posting from your resume (facts only)
    and the posting's keywords. Edit freely; your edits are kept until you rebuild.
"""
import json
import os
import re

from cover_letter import SKILL_NAMES, _first_person, _join, _pick, _w
from scoring import find_skills

STANDARD_PATH = "profile/answers.json"


def _years(resume):
    m = re.search(r"(\d+\+?)\s+years", resume.get("summary", ""))
    return m.group(1) if m else ""


def default_standard(resume: dict) -> list:
    contact = [x.strip() for x in resume.get("contact", "").split("|") if x.strip()]
    email = next((x for x in contact if "@" in x), "")
    phone = next((x for x in contact if re.search(r"\d{3}.*\d{4}", x)), "")
    city = next((x for x in contact if x not in (email, phone)), "")
    city = re.sub(r",\s*([A-Za-z]{2})$", lambda m: ", " + m.group(1).upper(), city)
    yrs = _years(resume)
    return [
        ["Full name", resume.get("name", "")],
        ["Email", email],
        ["Phone", phone],
        ["Current location", city],
        ["LinkedIn profile URL", ""],
        ["GitHub / portfolio URL", ""],
        ["Personal website", ""],
        ["Are you legally authorized to work in the United States?", "Yes"],
        ["Will you now or in the future require sponsorship for employment visa status (e.g., H-1B)?", "Yes"],
        ["Years of professional software engineering experience", f"{yrs} years" if yrs else ""],
        ["Are you willing to relocate?", "Yes, I am open to relocating anywhere in the United States."],
        ["Are you comfortable traveling to customer sites?", "Yes, I am comfortable with regular travel to customer sites."],
        ["Earliest start date", "Two weeks after accepting an offer."],
        ["Desired salary", "I am flexible and open to discussing compensation based on the role's scope and total package."],
        ["How did you hear about this role?", "Company careers page."],
    ]


def load_standard(resume: dict, path: str = None) -> list:
    """Your saved standard answers; questions added in newer versions are appended with their defaults."""
    path = path or STANDARD_PATH
    defaults = default_standard(resume)
    try:
        saved = json.load(open(path))
    except (OSError, ValueError):
        return defaults
    have = {q for q, _ in saved}
    return saved + [d for d in defaults if d[0] not in have]


def save_standard(items: list, path: str = None):
    path = path or STANDARD_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    json.dump(items, open(path, "w"), indent=2, ensure_ascii=False)


def job_answers(resume: dict, job: dict) -> list:
    """[[question, answer]] written for this posting, from your resume only."""
    w = _w(job)
    company, title = job["company"], job["title"]
    have = set(find_skills("\n".join([resume.get("summary", ""), *[", ".join(v) for v in resume["skills"].values()],
                                      *[b for j in resume["experience"] for b in j["bullets"]]])))
    top = [SKILL_NAMES.get(s, s) for s in sorted(w, key=lambda s: -w[s]) if s in have][:3]
    cur = resume["experience"][0]
    best = _pick(cur["bullets"], w, 2)
    yrs = _years(resume)
    fde = bool(re.search(r"forward[- ]deploy|\bfde\b", title, re.I))
    why_company = (
        f"{company} is doing the kind of work I want to be part of"
        + (f": putting {_join(top)} into the hands of real customers" if top else "")
        + ". " + (f"The {title} role sits right where that happens, working directly with customers to make the product "
                  "succeed in their environment, which is the part of engineering I enjoy most. " if fde else
                  f"The {title} role lines up closely with what I have been building. ")
        + f"In my current role at {cur['company']}, " + _first_person(best[0])
        + f" I would like to bring that same hands-on, delivery-focused approach to {company}.")
    why_fit = (
        f"I have {yrs + ' years of ' if yrs else ''}experience building and supporting production software"
        + (f", with hands-on work in {_join(top)}" if top else "") + ". "
        + " ".join(_first_person(b) for b in best)
        + (" I am comfortable working directly with stakeholders, troubleshooting in production, and turning "
           "requirements into working solutions quickly, which is what a forward deployed role needs." if fde else ""))
    project = ("At " + cur["company"] + ", " + " ".join(_first_person(b) for b in _pick(cur["bullets"], w, 3)))
    return [
        [f"Why do you want to work at {company}?", why_company],
        [f"Why are you a good fit for the {title} role?", why_fit],
        ["Describe a relevant project or accomplishment.", project],
    ]


def load_job(folder: str, resume: dict, job: dict) -> list:
    path = os.path.join(folder, "answers.json")
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return job_answers(resume, job)


def save_job(folder: str, items: list):
    os.makedirs(folder, exist_ok=True)
    json.dump(items, open(os.path.join(folder, "answers.json"), "w"), indent=2, ensure_ascii=False)
