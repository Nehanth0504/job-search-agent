"""Parse the original resume (.docx) into profile/master_resume.json.

The master resume is the single source of truth: tailoring may only select, reorder and
reword facts that already exist here. Re-run this whenever you update your resume.

    python build_master_resume.py profile/resume.docx
"""
import json
import re
import subprocess
import sys

HEADINGS = {"Professional Summary", "Skills & Tools", "Professional Experience", "Education"}
# Any of these spellings (any capitalisation, with or without a trailing colon) starts that section.
SYNONYMS = {
    "Professional Summary": ["professional summary", "summary", "profile", "professional profile", "career summary",
                             "summary of qualifications", "about me", "objective", "career objective"],
    "Skills & Tools": ["skills & tools", "skills and tools", "technical skills", "skills", "core skills",
                       "core competencies", "technical expertise", "technical proficiencies", "technologies"],
    "Professional Experience": ["professional experience", "experience", "work experience", "employment history",
                                "work history", "relevant experience", "career history"],
    "Education": ["education", "education & certifications", "education and certifications", "academic background"],
}
SYNONYMS["Projects"] = ["projects", "personal projects", "academic projects", "selected projects", "key projects",
                        "side projects", "project experience", "notable projects"]
OTHER_SECTIONS = ["certifications", "certificates", "awards", "achievements",
                  "publications", "languages", "interests", "volunteer experience", "volunteering", "honors"]
_HEADING_OF = {s: h for h, syns in SYNONYMS.items() for s in syns}
DATE_RX = re.compile(
    r"^(?P<title>.*?)[\s|,]+(?P<start>[A-Z][a-z]+\.? \d{4}|\d{1,2}/\d{4})\s*[–—-]\s*"
    r"(?P<end>Current|Present|Now|Till Date|[A-Z][a-z]+\.? \d{4}|\d{1,2}/\d{4})"
    r"(?:\s*[|,]\s*(?P<loc>[^|]+))?$", re.I
)


def heading_of(line: str):
    """'PROFESSIONAL SUMMARY' / 'Technical Skills:' -> canonical section name, '_other', or None."""
    key = re.sub(r"\s+", " ", line.strip().rstrip(":").strip()).lower()
    if not key or len(key) > 40:
        return None
    if key in _HEADING_OF:
        return _HEADING_OF[key]
    if key in OTHER_SECTIONS:
        return "_other"
    return None


def split_top_level(s: str):
    """Split on commas that are not inside parentheses."""
    out, depth, cur = [], 0, ""
    for ch in s:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def parse(text: str) -> dict:
    lines = [l.rstrip() for l in text.splitlines()]
    name = lines[0].strip()
    if name.isupper():
        name = name.title()
    head = [l.strip() for l in lines[1:6] if l.strip()]
    contact = head[0]
    headline = head[1] if len(head) > 1 and not heading_of(head[1]) and len(head[1]) < 140 else ""

    sections, cur = {}, None
    for l in lines[1:]:
        h = heading_of(l)
        if h:
            cur = h
            sections.setdefault(cur, [])
        elif cur:
            sections[cur].append(l)
    missing = [h for h in ("Professional Summary", "Skills & Tools", "Professional Experience") if h not in sections]
    if missing:
        raise ValueError("Could not find these sections: " + ", ".join(missing) + ". Use headings like "
                         "Summary, Skills (or Technical Skills), Experience (or Professional Experience) and Education.")

    summary = " ".join(p.strip(" -\t").strip() for p in "\n".join(sections["Professional Summary"]).split("\n")
                       if p.strip(" -\t").strip())

    skills = {}
    for l in sections["Skills & Tools"]:
        l = l.strip().lstrip("-•").strip()
        if ":" in l:
            cat, items = l.split(":", 1)
            if cat.strip() and items.strip():
                skills[cat.strip()] = split_top_level(items)

    experience, job = [], None
    block = [l for l in sections["Professional Experience"]]
    i = 0
    while i < len(block):
        l = block[i].strip()
        if not l:
            i += 1
            continue
        if l.startswith("- "):
            job["bullets"].append(l[1:].strip())
        elif job is None or (job["bullets"] and not l.startswith("-")):
            # company line, followed by a "<title> <start> - <end>" line
            nxt = next((block[j].strip() for j in range(i + 1, len(block)) if block[j].strip()), "")
            m = DATE_RX.match(nxt)
            if m:
                if "\t" in l:                                   # "Company<TAB>Location" (the agent's own layout)
                    company, loc = (x.strip() for x in l.split("\t", 1))
                else:
                    company, _, loc = l.partition(",")
                loc = loc.strip() or (m["loc"] or "").strip()      # "Title<TAB>Aug 2025 – Present | Remote"
                job = dict(company=company.strip(), location=loc, title=m["title"].strip(" -\t–"),
                           start=m["start"], end=m["end"], bullets=[])
                experience.append(job)
                while block[i].strip() != nxt:
                    i += 1
        i += 1

    education = [l.strip(" -\t").replace("\t", " | ").strip() for l in sections.get("Education", [])
                 if l.strip(" -\t").strip()]
    projects, proj = [], None
    for l in sections.get("Projects", []):
        s = l.strip()
        if not s:
            continue
        if s.startswith("- "):
            if proj is None:
                proj = dict(name="", bullets=[])
                projects.append(proj)
            proj["bullets"].append(s[2:].strip())
        else:
            name_part, sep, rest = s.partition(":")       # "Name: one-line description" is a project too
            proj = dict(name=(name_part if sep and len(name_part) < 90 else s).replace("\t", " | ").strip(), bullets=[])
            if sep and len(name_part) < 90 and rest.strip():
                proj["bullets"].append(rest.strip())
            projects.append(proj)
    out = dict(name=name, contact=contact, summary=summary, skills=skills,
               experience=experience, education=education, projects=projects)
    if headline:
        out["headline"] = headline
    return out


def docx_to_text(path: str) -> str:
    """Plain text of a .docx without pandoc: bullets become "- ", tabs become spaces."""
    import docx
    out = []
    for p in docx.Document(path).paragraphs:
        t = re.sub(r"[ ]*\t+[ ]*", "\t", p.text).strip()   # a tab separates e.g. company and location
        ppr = p._p.pPr
        bullet = (ppr is not None and ppr.numPr is not None) or p.style.name.lower().startswith("list")
        out.append(("- " + t) if (bullet and t) else t)
    return "\n".join(out)


def build(src: str, dest: str = "profile/master_resume.json") -> dict:
    data = parse(docx_to_text(src))
    if not data["experience"] or not data["skills"]:
        raise ValueError("Could not find the Skills & Tools and Professional Experience sections. "
                         "Use the headings: Professional Summary, Skills & Tools, Professional Experience, Education.")
    with open(dest, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return data


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "profile/resume.docx"
    data = build(src)
    print("jobs:", [(j["company"], j["start"], j["end"], len(j["bullets"])) for j in data["experience"]])
    print("skill categories:", len(data["skills"]), "| skills:", sum(map(len, data["skills"].values())))
    print("education:", data["education"])
