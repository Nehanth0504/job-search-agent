"""Tailor the master resume to one job, using ONLY facts already in profile/master_resume.json.

What it does (nothing else):
  * keeps your real job titles, companies and dates
  * picks the bullets most relevant to the posting (verbatim, never reworded) and drops the rest
  * puts the skills the posting names first inside your skill groups
  * trims the summary to the sentences that fit the posting
It never adds a skill, tool, number or employer. Skills you lack are reported as gaps instead.

Output is a plain one-column .docx (what ATS parsers read best) in data/resumes/<job id>/.
"""
import json
import os
import re

from ats import MAX_WORDS, TARGET, check, report_text
from scoring import find_skills, load_profile

CAPS = [8, 6, 4, 3]            # max bullets for the 1st (newest), 2nd ... job; older jobs get fewer
SUMMARY_SENTENCES = 5


def _weights(job):
    w = find_skills(job["title"] + "\n" + job["title"] + "\n" + job.get("description", ""))
    return {s: min(n, 3) for s, n in w.items()}


def _bullet_score(text, w):
    return sum(w.get(s, 0) for s in find_skills(text))


def pick_bullets(bullets, cap, w, must):
    """Cover wanted skills first (greedy), then fill with the best-scoring bullets; keep original order."""
    chosen, covered = [], set()
    ranked = sorted(range(len(bullets)), key=lambda i: (-_bullet_score(bullets[i], w), i))
    for skill in must:                                   # greedy cover
        if skill in covered or len(chosen) >= cap:
            continue
        for i in ranked:
            if i not in chosen and skill in find_skills(bullets[i]):
                chosen.append(i)
                covered |= set(find_skills(bullets[i]))
                break
    for i in ranked:
        if len(chosen) >= cap:
            break
        if i not in chosen and _bullet_score(bullets[i], w) > 0:
            chosen.append(i)
    for i in range(len(bullets)):                        # a job never shows empty
        if not chosen and i not in chosen:
            chosen.append(i)
    return [bullets[i] for i in sorted(chosen)]


def pick_summary(summary, w):
    sents = re.split(r"(?<=[.!?])\s+", summary.strip())
    ranked = sorted(range(1, len(sents)), key=lambda i: (-_bullet_score(sents[i], w), i))
    keep = {0, *ranked[:SUMMARY_SENTENCES - 1]}
    return " ".join(sents[i] for i in sorted(keep))


def order_skills(skills, w, job_text):
    low = job_text.lower()

    def hit(item):
        return (any(s in w for s in find_skills(item)) or
                (len(item) > 2 and item.lower() in low))

    groups = []
    for name, items in skills.items():
        items2 = sorted(items, key=lambda it: 0 if hit(it) else 1)   # stable: keeps original order otherwise
        groups.append((sum(1 for it in items if hit(it)), name, items2))
    groups.sort(key=lambda g: -g[0])
    return [(n, i) for _, n, i in groups]


def select(master, job, shrink=0):
    w = _weights(job)
    owned = load_profile_from(master)
    must = sorted((s for s in w if s in owned), key=lambda s: -w[s])
    jobs = []
    for idx, j in enumerate(master["experience"]):
        cap = max(2, CAPS[min(idx, len(CAPS) - 1)] - shrink)
        jobs.append(dict(j, bullets=pick_bullets(j["bullets"], cap, w, must)))
    jt = job["title"] + "\n" + job.get("description", "")
    contact = " | ".join(x.strip() for x in master["contact"].split("|") if x.strip())
    # projects: the most relevant first (at most 3), each with its most relevant bullets
    projects = sorted(master.get("projects") or [],
                      key=lambda p: -sum(_bullet_score(x, w) for x in [p.get("name", "")] + p.get("bullets", [])))
    projects = [dict(p, bullets=pick_bullets(p.get("bullets", []), max(2, 4 - shrink), w, must) if p.get("bullets") else [])
                for p in projects[:max(1, 3 - shrink)]]
    return dict(master, contact=contact, summary=pick_summary(master["summary"], w),
                skills=dict(order_skills(master["skills"], w, jt)), experience=jobs, projects=projects)


def load_profile_from(master):
    parts = [master["summary"]]
    for p in master.get("projects") or []:
        parts.append(p.get("name", ""))
        parts.extend(p.get("bullets", []))
    for items in master["skills"].values():
        parts.extend(items)
    for j in master["experience"]:
        parts.append(j["title"])
        parts.extend(j["bullets"])
    return set(find_skills("\n".join(parts)))


FONT = "Times New Roman"


def _font(run_or_style, size=None, bold=None, italic=None):
    """Times New Roman on every script slot, so Word never falls back to Calibri."""
    from docx.oxml.ns import qn
    from docx.shared import Pt
    f = run_or_style.font
    f.name = FONT
    el = run_or_style.element if hasattr(run_or_style, "element") else run_or_style._element
    rpr = el.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(fonts)
    for k in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(k), FONT)
    if size:
        f.size = Pt(size)
    if bold is not None:
        f.bold = bold
    if italic is not None:
        f.italic = italic


def _rule(p):
    """Thin line under a section heading."""
    from docx.oxml.ns import qn
    ppr = p._p.get_or_add_pPr()
    bdr = ppr.makeelement(qn("w:pBdr"), {})
    bottom = bdr.makeelement(qn("w:bottom"), {qn("w:val"): "single", qn("w:sz"): "6", qn("w:space"): "1",
                                               qn("w:color"): "000000"})
    bdr.append(bottom)
    ppr.append(bdr)


def write_docx(r, path):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
    from docx.shared import Inches, Pt
    J, C = WD_ALIGN_PARAGRAPH.JUSTIFY, WD_ALIGN_PARAGRAPH.CENTER
    d = Document()
    s = d.sections[0]
    s.page_width, s.page_height = Inches(8.5), Inches(11)
    s.left_margin = s.right_margin = Inches(0.7)
    s.top_margin = s.bottom_margin = Inches(0.6)
    width = s.page_width - s.left_margin - s.right_margin
    for name in ("Normal", "List Bullet"):
        st = d.styles[name]
        _font(st, size=11)
        st.paragraph_format.space_after = Pt(0)
        st.paragraph_format.space_before = Pt(0)
        st.paragraph_format.line_spacing = 1.0

    def para(align=J, after=0, before=0, style=None):
        p = d.add_paragraph(style=style)
        p.alignment = align
        p.paragraph_format.space_after = Pt(after)
        p.paragraph_format.space_before = Pt(before)
        return p

    def run(p, text, size=11, bold=False, italic=False):
        rn = p.add_run(text)
        _font(rn, size=size, bold=bold, italic=italic)
        return rn

    def heading(t):
        p = para(align=WD_ALIGN_PARAGRAPH.LEFT, before=8, after=3)
        run(p, t.upper(), size=11.5, bold=True)
        _rule(p)

    # header: name and contact centred
    run(para(C), r["name"], size=18, bold=True)
    contact = "  |  ".join(re.sub(r",\s*([A-Za-z]{2})$", lambda m: ", " + m.group(1).upper(), x.strip())
                           for x in r["contact"].split("|") if x.strip())
    run(para(C, after=0 if r.get("headline") else 2), contact, size=11)
    if r.get("headline"):
        run(para(C, after=2), r["headline"], size=11, italic=True)

    heading("Professional Summary")
    run(para(J), r["summary"])

    heading("Technical Skills")
    for name, items in r["skills"].items():
        p = para(J, after=1)
        run(p, name + ": ", bold=True)
        run(p, ", ".join(items))

    heading("Professional Experience")
    for i, j in enumerate(r["experience"]):
        p = para(J, before=6 if i else 2)
        p.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
        p.paragraph_format.keep_with_next = True
        run(p, j["company"], bold=True)
        run(p, "\t" + (j.get("location") or ""), bold=True)
        p = para(J, after=2)
        p.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
        p.paragraph_format.keep_with_next = True
        run(p, j["title"], italic=True)
        run(p, f"\t{j['start']} - {j['end']}", italic=True)
        for b in j["bullets"]:
            bp = para(J, after=1, style="List Bullet")
            bp.paragraph_format.left_indent = Inches(0.25)
            bp.paragraph_format.first_line_indent = Inches(-0.18)
            run(bp, b)

    if r.get("projects"):
        heading("Projects")
        for i, p in enumerate(r["projects"]):
            pp = para(J, before=4 if i else 2, after=1)
            pp.paragraph_format.keep_with_next = bool(p.get("bullets"))
            run(pp, p.get("name", ""), bold=True)
            for b in p.get("bullets", []):
                bp = para(J, after=1, style="List Bullet")
                bp.paragraph_format.left_indent = Inches(0.25)
                bp.paragraph_format.first_line_indent = Inches(-0.18)
                run(bp, b)

    heading("Education")
    for line in r["education"]:
        run(para(J), line)
    d.save(path)


def build_files(struct, job, folder, master_name, owned):
    """Write the .docx for `struct`, run the ATS check, save report + editable copy. Returns (path, res)."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", job["company"]).strip("_")
    path = os.path.join(folder, f"{re.sub(' ', '_', master_name)}_{safe}.docx")
    write_docx(struct, path)
    res = check(path, job, struct, owned)
    with open(os.path.join(folder, "ats_report.txt"), "w") as f:
        f.write(report_text(res, job))
    with open(os.path.join(folder, "tailored.json"), "w") as f:
        json.dump(struct, f, indent=2, ensure_ascii=False)
    return path, res


def _auto_keywords_setting():
    try:
        import yaml
        return bool((yaml.safe_load(open("config.yaml")) or {}).get("auto_add_missing_keywords", False))
    except (OSError, ValueError, ImportError):
        return False


def add_missing_keywords(struct: dict, missing: list) -> list:
    """Put the posting's missing keywords (its own wording) on an 'Additional Skills' line. Returns those added."""
    have = {x.lower() for v in struct["skills"].values() for x in v}
    new = [k for k in missing if k.lower() not in have]
    if new:
        struct["skills"]["Additional Skills"] = struct["skills"].get("Additional Skills", []) + new
        struct["added_keywords"] = sorted(set(struct.get("added_keywords", [])) | set(new))
    return new


def _claude_ready():
    try:
        import rewrite
        return rewrite.available()
    except Exception:  # noqa: BLE001
        return False


def _claude_version(master, job, owned):
    """The Claude-written version (see rewrite.py), or None if it can't be made right now."""
    import rewrite
    try:
        return rewrite.rewrite(select(master, job, 0), master, job, owned)
    except Exception as e:  # noqa: BLE001  (API down, bad reply ...): plain tailoring instead
        print(f"[claude] resume rewrite skipped for {job.get('company')}: {e}")
        return None


def _trim_one(struct) -> bool:
    """Drop one bullet from the oldest job (or project) that still has more than 2; False when nothing left."""
    for group in (list(reversed(struct.get("experience") or [])), list(reversed(struct.get("projects") or []))):
        for j in group:
            if len(j.get("bullets") or []) > 2:
                j["bullets"].pop()
                return True
    return False


def tailor(job, job_id, master_path="profile/master_resume.json", data_dir="data", auto_keywords=None,
           use_claude=None):
    """Build the tailored resume for `job`; returns (docx_path, ats_result). Retries shorter if too long.
    With auto_add_missing_keywords on (config.yaml), the posting's missing keywords are added to the Skills section."""
    master = json.load(open(master_path))
    owned = load_profile_from(master)
    folder = os.path.join(data_dir, "resumes", str(job_id))
    os.makedirs(folder, exist_ok=True)
    struct = None
    if use_claude is not False:
        struct = _claude_version(master, job, owned) if (use_claude or _claude_ready()) else None
    if struct is not None:
        path, res = build_files(struct, job, folder, master["name"], owned)
        while res["words"] > MAX_WORDS and _trim_one(struct):
            path, res = build_files(struct, job, folder, master["name"], owned)
    else:
        for shrink in range(0, 4):
            struct = select(master, job, shrink)
            path, res = build_files(struct, job, folder, master["name"], owned)
            if res["words"] <= MAX_WORDS and not res["missing_owned"]:
                break
    if (auto_keywords if auto_keywords is not None else _auto_keywords_setting()) and res.get("missing_terms"):
        if add_missing_keywords(struct, res["missing_terms"]):
            path, res = build_files(struct, job, folder, master["name"], owned)
    _cover(folder, job)
    return path, res


def _cover(folder, job):
    """Cover letter beside the resume; never blocks the resume."""
    try:
        import cover_letter
        cover_letter.make(json.load(open(os.path.join(folder, "tailored.json"))), job, folder)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] cover letter not made for {job.get('company')}: {e}")


def rebuild(struct, job, job_id, master_path="profile/master_resume.json", data_dir="data"):
    """Rebuild the .docx from a hand-edited structure (the dashboard editor)."""
    owned = load_profile_from(json.load(open(master_path)))
    folder = os.path.join(data_dir, "resumes", str(job_id))
    os.makedirs(folder, exist_ok=True)
    return build_files(struct, job, folder, struct["name"], owned)


if __name__ == "__main__":
    # python tailor.py --backfill   -> make a tailored resume for every job you were already alerted about
    import sqlite3
    import sys
    import yaml
    if "--reformat" in sys.argv:          # redraw every saved resume in the current layout; content unchanged
        import glob
        n = 0
        for js in glob.glob("data/resumes/*/tailored.json"):
            folder = os.path.dirname(js)
            struct = json.load(open(js))
            for docx_path in glob.glob(os.path.join(folder, "*.docx")):
                if docx_path.endswith("_Cover_Letter.docx"):
                    continue
                write_docx(struct, docx_path)
                n += 1
        print(f"Reformatted {n} resumes.")
    if "--covers" in sys.argv:            # cover letters for jobs that already have a tailored resume
        cfg = yaml.safe_load(open("config.yaml"))
        import cover_letter
        con = sqlite3.connect("jobs.db")
        con.row_factory = sqlite3.Row
        n = 0
        for r in con.execute("SELECT * FROM jobs WHERE resume_file IS NOT NULL"):
            folder = os.path.join("data", "resumes", str(r["id"]))
            js = os.path.join(folder, "tailored.json")
            if os.path.exists(js) and not os.path.exists(os.path.join(folder, "cover_letter.txt")):
                cover_letter.make(json.load(open(js)), dict(r), folder)
                n += 1
        print(f"Made {n} cover letters.")
    if "--backfill" in sys.argv:
        cfg = yaml.safe_load(open("config.yaml"))
        from db import Tracker
        trk = Tracker("jobs.db", cfg["max_per_company"])
        db = trk.db
        for r in db.execute("SELECT * FROM jobs WHERE status IN ('notified','review_queue') ORDER BY score DESC"):
            p, res = tailor(dict(r), r["id"], cfg.get("master_resume", "profile/master_resume.json"))
            trk.set_ats(r["id"], res, os.path.basename(p))
            print(f"{r['company']} | {r['title']} | ATS check {res['score']} -> {p}")
