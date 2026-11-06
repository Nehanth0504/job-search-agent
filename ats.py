"""ATS match score: how a resume-screening system would see this resume for this posting.

Employers' ATS products (Workday, Greenhouse, iCIMS, Taleo ...) keep their scoring private and every employer
configures its own, so no outside tool can reproduce an employer's exact number. This score follows how those
systems and recruiters actually screen (parse the file, then match it against the posting):

  hard skills   45  every tool / technology / acronym the posting names, read from the posting itself
                    (requirements count double); found in your resume = matched
  job title     10  the posting's title words appear in your resume (headline, titles, summary)
  experience    10  your years meet the minimum the posting asks for
  education      5  you hold the degree level the posting asks for
  soft skills   10  communication / stakeholder / customer-facing / collaboration ... the posting names
  readability   20  a parser finds your name, email, phone, standard sections, every job with dates;
                    no tables, images or text boxes; reasonable length

80+ is a strong match. Skills you do not have are listed (missing_terms) and never added automatically.
"""
import re

from scoring import VOCAB, find_skills

TARGET = 80
HEADINGS = ["Professional Summary", ("Skills & Tools", "Technical Skills", "Skills"), "Professional Experience", "Education"]
MAX_WORDS = 1000

NOT_SKILLS = {
    "us", "usa", "u.s", "eeo", "eoe", "pto", "hr", "ceo", "cto", "cfo", "coo", "vp", "llc", "inc", "ltd", "faq", "dei",
    "ada", "lgbtq", "lgbtqia", "401k", "401(k)", "i", "ii", "iii", "iv", "ok", "or", "and", "the", "we", "you", "our",
    "ny", "nyc", "sf", "ca", "tx", "wa", "fl", "il", "ma", "co", "nj", "va", "ga", "nc", "pa", "dc", "uk", "eu", "est",
    "pst", "cst", "et", "pt", "ct", "am", "pm", "q1", "q2", "q3", "q4", "ote", "w2", "h1b", "h-1b", "gpa", "phd", "ms",
    "bs", "ba", "ma", "mba", "jd", "md", "yoe", "wfh", "kpi", "kpis", "okr", "okrs", "b2b", "b2c", "saas", "id", "it",
    "fte", "pii", "nda", "covid", "covid-19", "usd", "inclusive", "equal", "opt", "cpt", "ead", "gc", "note", "notes",
    "apply", "remote", "onsite", "hybrid", "salary", "benefits", "team", "teams", "role", "job", "us-based",
}
COMMON_WORDS = {  # Capitalised ordinary words that show up inside slashed or hyphenated tokens
    "manager", "director", "engineer", "engineering", "senior", "lead", "staff", "principal", "full", "time",
    "part", "contract", "remote", "hybrid", "onsite", "office", "and", "or", "the", "of", "us", "team", "teams",
    "product", "sales", "customer", "customers", "partner", "partners", "client", "clients", "business",
}
US_STATES = {"al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in", "ia", "ks", "ky", "la",
             "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
             "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc"}
KNOWN_ACRONYMS = {  # all-capital words that ARE skills (anything else in capitals must be 2-3 letters to count)
    "aws", "gcp", "sql", "llm", "llms", "rag", "api", "apis", "rest", "ml", "ai", "nlp", "etl", "elt", "sdk", "mcp",
    "k8s", "iam", "vpc", "json", "yaml", "http", "https", "css", "html", "html5", "css3", "jvm", "gpu", "gpus", "cuda",
    "tpu", "oauth", "sso", "saml", "rbac", "jwt", "tcp", "dns", "cdn", "ssh", "gitops", "devops", "mlops", "llmops",
    "nosql", "olap", "oltp", "crm", "erp", "sap", "bi", "ocr", "asr", "tts", "cv", "rl", "rlhf", "dpo", "lora", "peft",
    "sre", "ci", "cd", "ci/cd", "tdd", "bdd", "orm", "ui", "ux", "grpc", "ec2", "s3", "eks", "ecs", "gke", "aks",
    "hipaa", "soc2", "gdpr", "fedramp", "pci", "iot", "arm", "simd", "kafka", "dbt", "aws", "azure", "posix", "unix",
}
TECH_WORDS = {  # lower-case technology words that postings often write without capitals
    "python", "java", "javascript", "typescript", "golang", "rust", "scala", "kotlin", "swift", "ruby", "php", "sql",
    "nosql", "postgres", "postgresql", "mysql", "mongodb", "redis", "kafka", "spark", "airflow", "dbt", "snowflake",
    "databricks", "bigquery", "docker", "kubernetes", "terraform", "ansible", "jenkins", "linux", "bash", "react",
    "angular", "vue", "node", "django", "flask", "fastapi", "graphql", "grpc", "rest", "pytorch", "tensorflow",
    "keras", "pandas", "numpy", "langchain", "llamaindex", "openai", "anthropic", "huggingface", "faiss", "pinecone",
    "weaviate", "elasticsearch", "opensearch", "embeddings", "rag", "llm", "llms", "nlp", "mlops", "etl", "aws", "gcp",
    "azure", "git", "ci/cd", "microservices", "agents", "agentic", "fine-tuning", "evals", "observability",
}
SOFT = {
    "communication": ["communicat"], "stakeholder management": ["stakeholder"],
    "customer-facing": ["customer-facing", "customer facing", "client-facing", "client facing", "with customers",
                        "customer"],
    "collaboration": ["collaborat", "cross-functional", "cross functional"], "leadership": ["lead ", "leadership"],
    "problem solving": ["problem-solving", "problem solving", "troubleshoot"], "ownership": ["ownership", "own "],
    "mentoring": ["mentor"], "presentation": ["present to", "presentation", "demo"],
    "requirements gathering": ["requirement"], "agile": ["agile", "scrum"],
}
DISPLAY: dict = {}                                       # term -> how the posting wrote it
ALIAS_TO_SKILL = {al.lower(): skill for skill, aliases in VOCAB.items() for al in aliases}
SOFT_IN_VOCAB = {"customer-facing"}                     # scored under soft skills, not hard skills
REQ_START = re.compile(r"(requirements|qualifications|what you(?:'|’)ll need|you have|must have|minimum|"
                       r"what we(?:'|’)re looking for|who you are|basic qualifications)", re.I)
PREF_START = re.compile(r"(preferred|nice to have|bonus|pluses|plus if|good to have|ideally)", re.I)
TOKEN = re.compile(r"(?<![\w@])([A-Za-z][A-Za-z0-9]*(?:[.+#/-][A-Za-z0-9+#]+)*[+#]*)(?![\w@])")


def _text(path):
    import docx
    d = docx.Document(path)
    return d, "\n".join(p.text for p in d.paragraphs)


def _sections(desc: str):
    """(required_text, other_text) split by 'Requirements' / 'Preferred' style headings."""
    req, other, mode = [], [], "other"
    for line in re.split(r"(?<=[.!?])\s+|\n", desc or ""):
        if PREF_START.search(line[:60]):
            mode = "pref"
        elif REQ_START.search(line[:60]):
            mode = "req"
        (req if mode == "req" else other).append(line)
    return " ".join(req), " ".join(other)


def _is_skill_token(core: str) -> bool:
    """Is this word from a posting a tool / technology / method (and not a state, code or ordinary word)?"""
    low = core.lower()
    if low in TECH_WORDS or low in KNOWN_ACRONYMS:
        return True
    if low in NOT_SKILLS or low in US_STATES or re.match(r"^li-[a-z0-9]+$", low) or re.search(r"\d{3,}", low):
        return False
    parts = [p for p in re.split(r"[/-]", core) if p]
    if len(parts) > 1 and all(p.lower() in COMMON_WORDS or (p[:1].isupper() and p[1:].islower()) for p in parts):
        return False                                     # Manager/Director, Full-Time
    letters = re.sub(r"[^A-Za-z]", "", core)
    if re.fullmatch(r"[A-Za-z]{1,2}[+#]+|\.net|\.NET", core):     # C++, C#, F#, .NET
        return True
    if core.isupper():                                   # capitals: short acronyms only (MCP, SQL), not words (NOTE)
        return 2 <= len(letters) <= 3 and low not in US_STATES
    mixed = any(c.isupper() for c in core[1:]) and any(c.islower() for c in core)   # TypeScript, LangChain
    symbol = bool(re.search(r"[.+#]", core)) and len(core) <= 15 and len(letters) >= 1   # Node.js, C++, C#
    return mixed or symbol


def posting_terms(title: str, desc: str, company: str = ""):
    """{term: weight} for the hard skills a posting names (from the posting's own words)."""
    req, other = _sections(desc)
    comp = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", company or "")}
    terms = {}
    shown = DISPLAY.clear() or DISPLAY

    def add(t, w):
        k = t.lower().strip(".-/")
        k = ALIAS_TO_SKILL.get(k, k)                    # "LLMs" / "LangChain" -> one skill, counted once
        if not k or k in NOT_SKILLS or k in comp or len(k) < 2 or k.isdigit() or k in SOFT_IN_VOCAB:
            return
        terms[k] = max(terms.get(k, 0), w)
        shown.setdefault(k, t if t.lower() != k or not t.islower() else t)

    for text, w in ((req, 2), (other + " " + title, 1)):
        for m in TOKEN.finditer(text):
            tok = m.group(1)
            core = tok.strip(".")
            if _is_skill_token(core):
                add(core, w)
        for skill in find_skills(text):                    # multi-word skills: "machine learning", "vector search"
            add(skill, w)
    return terms


def _has(term: str, text_low: str, vocab_found: set) -> bool:
    if term in VOCAB:
        return term in vocab_found
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text_low) is not None


def _years(master):
    m = re.search(r"(\d+)\+?\s+years", master.get("summary", ""))
    return int(m.group(1)) if m else None


def check(path, job, master, owned_skills):
    from roles import required_years
    d, text = _text(path)
    low = text.lower()
    desc = job.get("description", "") or ""
    vocab_found = set(find_skills(text))
    parts, rows = [], []

    def row(name, ok, detail=""):
        rows.append(dict(name=name, ok=bool(ok), detail=detail))
        return bool(ok)

    # readability (20)
    first = [l for l in text.splitlines() if l.strip()][:3]
    pts = 3 * row("Name at the top", master["name"].lower() in " ".join(first).lower())
    pts += 3 * row("Email found", re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text))
    pts += 2 * row("Phone found", re.search(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", text))
    missing_h = [(h if isinstance(h, str) else h[0]) for h in HEADINGS
                 if not any(x.lower() in low for x in ((h,) if isinstance(h, str) else h))]
    pts += 4 * row("Standard section headings", not missing_h, ", ".join(missing_h))
    pts += 3 * row("No tables, images or text boxes", not d.tables and not d.inline_shapes
                   and "w:txbxContent" not in d.element.xml)
    gaps_jobs = [j["company"] for j in master["experience"] if j["company"] not in text or j["start"] not in text]
    pts += 3 * row("Every job with company and dates", not gaps_jobs, ", ".join(gaps_jobs))
    words = len(text.split())
    pts += 2 * row(f"Reasonable length ({words} words)", words <= MAX_WORDS)
    parts.append(("Readability", pts, 20))

    # hard skills (45)
    terms = posting_terms(job.get("title", ""), desc, job.get("company", ""))
    found = {t: w for t, w in terms.items() if _has(t, low, vocab_found)}
    missing_terms = [DISPLAY.get(t, t) for t in sorted((t for t in terms if t not in found), key=lambda t: (-terms[t], t))]
    total = sum(terms.values())
    hard = 45.0 if not total else 45.0 * sum(found.values()) / total
    parts.append(("Hard skills", round(hard, 1), 45))
    row(f"Hard skills from the posting: {len(found)} of {len(terms)} found", not missing_terms,
        ", ".join(missing_terms[:12]))

    # job title (10)
    stop = {"senior", "sr", "junior", "jr", "lead", "staff", "principal", "remote", "hybrid", "i", "ii", "iii", "iv",
            "the", "and", "of", "for", "us", "usa", "-", "–", "new", "york"}
    tw = [w for w in re.findall(r"[a-z0-9+#]+", re.sub(r"\(.*?\)", "", job.get("title", "").lower())) if w not in stop]
    head_text = low[:2500]
    t_hit = sum(1 for w in tw if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", head_text))
    title_pts = 10.0 if not tw else round(10.0 * t_hit / len(tw), 1)
    parts.append(("Job title", title_pts, 10))
    row("Job title words in your resume", title_pts >= 9.9,
        ", ".join(w for w in tw if w not in head_text))

    # experience (10)
    need, have = required_years(desc), _years(master)
    exp = 10.0 if not need or (have and have >= need) else round(10.0 * (have or 0) / need, 1)
    parts.append(("Experience", exp, 10))
    row(f"Years of experience ({have or '?'} vs {need or 'none'} asked)", exp >= 9.9)

    # education (5)
    dl = desc.lower()
    asks_master = bool(re.search(r"master'?s|m\.s\.|\bms\b|graduate degree", dl)) and not re.search(
        r"bachelor'?s or master'?s|bs/ms|bs or ms", dl)
    asks_phd = "phd" in dl or "ph.d" in dl
    has_master, has_bach = "master" in low, bool(re.search(r"bachelor|b\.tech|b\.s\.|b\.e\.", low))
    edu = 5.0
    if asks_phd and "phd" not in low and "ph.d" not in low:
        edu = 2.0
    elif asks_master and not has_master:
        edu = 3.0
    elif re.search(r"bachelor|degree", dl) and not (has_master or has_bach):
        edu = 0.0
    parts.append(("Education", edu, 5))
    row("Degree level the posting asks for", edu >= 5)

    # soft skills (10)
    asked = [k for k, keys in SOFT.items() if any(x in dl for x in keys)]
    got = [k for k in asked if any(x in low for x in SOFT[k])]
    soft = 10.0 if not asked else round(10.0 * len(got) / len(asked), 1)
    parts.append(("Soft skills", soft, 10))
    row(f"Soft skills the posting names: {len(got)} of {len(asked)}", len(got) == len(asked),
        ", ".join(k for k in asked if k not in got))

    score = round(sum(p[1] for p in parts), 1)
    # compatibility with the rest of the app
    wanted = find_skills(job.get("title", "") + "\n" + desc)
    in_doc = vocab_found
    missing_owned = sorted(s for s in wanted if s in owned_skills and s not in in_doc)
    gaps = sorted(s for s in wanted if s not in owned_skills)
    return dict(v=3, score=score, parts=[dict(name=n, pts=p, max=m) for n, p, m in parts], checks=rows,
                format=parts[0][1], keywords=round(hard, 1), missing_terms=missing_terms, found_terms=sorted(DISPLAY.get(t, t) for t in found),
                missing_owned=missing_owned, gaps=gaps, words=words, meets_target=score >= TARGET)


def report_text(res, job):
    out = [f"ATS match for {job['title']} @ {job['company']}: {res['score']}/100 "
           f"({'strong match' if res['meets_target'] else 'below ' + str(TARGET)})", ""]
    for p in res.get("parts", []):
        out.append(f"  {p['name']:<12} {p['pts']}/{p['max']}")
    out.append("")
    for r in res["checks"]:
        out.append(f"[{'x' if r['ok'] else ' '}] {r['name']}" + (f"  - {r['detail']}" if r["detail"] else ""))
    if res.get("missing_terms"):
        out.append("\nPosting keywords not in this resume: " + ", ".join(res["missing_terms"]))
    return "\n".join(out)
