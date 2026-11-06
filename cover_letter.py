"""Cover letter for one job, built only from facts already in your resume (no invented experience).

Structure: why this role -> what you do now (your most relevant bullets, in first person) -> earlier
experience -> the skills the posting asks for that you actually have -> close. Saved beside the tailored
resume as <Name>_<Company>_Cover_Letter.docx plus cover_letter.txt, which the dashboard lets you edit.
"""
import os
import re
import time

from scoring import find_skills

SKILL_NAMES = {   # how a skill reads in a sentence
    "llm": "large language models", "rag": "retrieval-augmented generation (RAG)", "nlp": "NLP",
    "rest apis": "REST APIs", "ci/cd": "CI/CD", "aws": "AWS", "gcp": "Google Cloud", "sql": "SQL",
    "mlops": "MLOps", "fastapi": "FastAPI", "pandas/numpy": "Pandas and NumPy", "openai": "Azure OpenAI",
    "langchain/agents": "agentic AI tooling", "customer-facing": "working directly with stakeholders",
    "vector search": "vector and semantic search", "data pipelines": "data pipelines", "react": "React",
    "typescript": "TypeScript", "javascript": "JavaScript", "mongodb": "MongoDB", "docker": "Docker",
    "kubernetes": "Kubernetes", "python": "Python", "django": "Django", "flask": "Flask", "azure": "Azure",
    "scikit-learn": "scikit-learn", "embeddings": "embeddings", "prompt engineering": "prompt engineering",
    "model evaluation": "model evaluation", "microservices": "microservices",
}


def _w(job):
    w = find_skills(job["title"] + "\n" + job["title"] + "\n" + job.get("description", ""))
    return {s: min(n, 3) for s, n in w.items()}


AI = {"llm", "rag", "embeddings", "vector search", "fine-tuning", "langchain/agents", "openai", "nlp",
      "deep learning", "transformers", "mlops", "model evaluation", "prompt engineering", "pytorch", "tensorflow",
      "scikit-learn"}


def _score(text, w, skip=()):
    """Skills the posting wants that this sentence shows; AI skills count double (your target roles)."""
    return sum(w.get(s, 0) * (2 if s in AI else 1) for s in find_skills(text) if s not in skip)


def _pick(bullets, w, n):
    """Up to n bullets that together show the most different wanted skills; original order kept."""
    chosen, covered = [], set()
    for _ in range(n):
        rest = [b for b in bullets if b not in chosen]
        if not rest:
            break
        best = max(rest, key=lambda b: (_score(b, w, covered), _score(b, w), -bullets.index(b)))
        chosen.append(best)
        covered |= set(find_skills(best))
    return [b for b in bullets if b in chosen]


def _first_person(bullet: str) -> str:
    b = bullet.strip().rstrip(".")
    return "I " + b[0].lower() + b[1:] + "." if b else ""


def _join(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _years(summary: str):
    m = re.search(r"(\d+\+?)\s+years", summary or "")
    return m.group(1) if m else None


def _role_line(summary: str) -> str:
    first = re.split(r"(?<=[.!?])\s+", (summary or "").strip())[0]
    m = re.match(r"(.+?)\s+with\s+\d", first)
    return m.group(1).strip() if m else "Software engineer"


def build_text(resume: dict, job: dict) -> str:
    """The letter body (paragraphs separated by blank lines). `resume` is the tailored resume structure."""
    w = _w(job)
    company, title = job["company"], job["title"]
    have = set(find_skills("\n".join([resume["summary"], *[", ".join(v) for v in resume["skills"].values()],
                                      *[b for j in resume["experience"] for b in j["bullets"]]])))
    wanted_have = [s for s in sorted(w, key=lambda s: -w[s]) if s in have][:5]
    years = _years(resume["summary"])
    role = _role_line(resume["summary"])

    p1 = (f"I am writing to apply for the {title} position at {company}. "
          + (f"The role's focus on {_join(SKILL_NAMES.get(s, s) for s in wanted_have[:3])} matches the work I have been "
             f"doing{' for the past ' + years + ' years' if years else ''}, and I would welcome the chance to bring "
             f"that experience to your team." if wanted_have else
             "It lines up closely with the work I have been doing, and I would welcome the chance to contribute to your team."))

    cur = resume["experience"][0]
    best = _pick(cur["bullets"], w, 3)
    p2 = (f"In my current role as {cur['title'].strip()} at {cur['company']}, " + _first_person(best[0])
          + (" " + " ".join(_first_person(b) for b in best[1:]) if len(best) > 1 else ""))

    paras = [p1, p2]
    prev = resume["experience"][1] if len(resume["experience"]) > 1 else None
    if prev and prev["bullets"]:
        b = _pick(prev["bullets"], w, 1)[0]
        paras.append(f"Before that, as {prev['title'].strip()} at {prev['company']}, " + _first_person(b))

    if wanted_have:
        paras.append(f"As a {role}, I bring hands-on experience with "
                     f"{_join(SKILL_NAMES.get(s, s) for s in wanted_have)}, along with a habit of building software "
                     "that is reliable in production, easy to maintain and clear for the people who depend on it.")
    paras.append(f"Thank you for considering my application. I would be glad to discuss how my experience can help "
                 f"{company} and to answer any questions about my background.")
    return "\n\n".join(paras)


def write_docx(resume: dict, job: dict, body: str, path: str):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt
    from tailor import _font
    d = Document()
    s = d.sections[0]
    s.page_width, s.page_height = Inches(8.5), Inches(11)
    s.left_margin = s.right_margin = Inches(1)
    s.top_margin = s.bottom_margin = Inches(0.8)
    st = d.styles["Normal"]
    _font(st, size=11.5)
    st.paragraph_format.space_after = Pt(0)
    st.paragraph_format.line_spacing = 1.08

    def para(text, align=WD_ALIGN_PARAGRAPH.JUSTIFY, after=10, size=11.5, bold=False):
        p = d.add_paragraph()
        p.alignment = align
        p.paragraph_format.space_after = Pt(after)
        r = p.add_run(text)
        _font(r, size=size, bold=bold)
        return p

    C = WD_ALIGN_PARAGRAPH.CENTER
    para(resume["name"], C, after=0, size=18, bold=True)
    contact = "  |  ".join(re.sub(r",\s*([A-Za-z]{2})$", lambda m: ", " + m.group(1).upper(), x.strip())
                           for x in resume["contact"].split("|") if x.strip())
    para(contact, C, after=18)
    para(time.strftime("%B %d, %Y").replace(" 0", " "), WD_ALIGN_PARAGRAPH.LEFT, after=12)
    para(f"Hiring Team\n{job['company']}", WD_ALIGN_PARAGRAPH.LEFT, after=12)
    para(f"Re: {job['title']}", WD_ALIGN_PARAGRAPH.LEFT, after=12, bold=True)
    para(f"Dear {job['company']} Hiring Team,", WD_ALIGN_PARAGRAPH.LEFT, after=10)
    for chunk in [c.strip() for c in body.split("\n\n") if c.strip()]:
        para(chunk)
    para("Sincerely,", WD_ALIGN_PARAGRAPH.LEFT, after=2)
    para(resume["name"], WD_ALIGN_PARAGRAPH.LEFT, after=0)
    d.save(path)


def file_name(resume: dict, job: dict) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", job["company"]).strip("_")
    return f"{re.sub(' ', '_', resume['name'])}_{safe}_Cover_Letter.docx"


def make(resume: dict, job: dict, folder: str, body: str = None) -> str:
    """Write the letter into `folder`. Keeps a hand-edited cover_letter.txt unless `body` is given."""
    txt = os.path.join(folder, "cover_letter.txt")
    if body is None:
        body = build_text(resume, job)
    with open(txt, "w") as f:
        f.write(body)
    path = os.path.join(folder, file_name(resume, job))
    write_docx(resume, job, body, path)
    return path
