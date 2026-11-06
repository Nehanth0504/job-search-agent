"""Resume-aware match scoring (deterministic, no API cost).

score = weighted share of the posting's recognised skills that appear in YOUR master resume.
Skills the posting wants but the resume does not contain are returned as `gaps`: they are
shown in the alert so you can judge honestly, and the tailoring step never adds them.

An LLM pass (judging seniority, domain fit, must-have vs nice-to-have) is layered on top
later for jobs that clear this first filter.
"""
import json
import re

# canonical skill -> aliases (matched case-insensitively on word boundaries)
VOCAB = {
    "python": ["python"],
    "pytorch": ["pytorch"],
    "tensorflow": ["tensorflow", "keras"],
    "scikit-learn": ["scikit-learn", "sklearn"],
    "pandas/numpy": ["pandas", "numpy"],
    "llm": ["llm", "llms", "large language model", "large language models"],
    "rag": ["rag", "retrieval-augmented", "retrieval augmented"],
    "prompt engineering": ["prompt engineering", "prompt design", "prompt templates"],
    "embeddings": ["embedding", "embeddings"],
    "vector search": ["vector search", "vector database", "vector db", "faiss", "pinecone",
                      "weaviate", "similarity search", "semantic search", "vector similarity"],
    "fine-tuning": ["fine-tuning", "fine tuning", "finetuning", "lora", "rlhf"],
    "langchain/agents": ["langchain", "llamaindex", "llama index", "agentic", "ai agents", "multi-agent"],
    "openai": ["openai", "azure openai"],
    "nlp": ["nlp", "natural language processing"],
    "deep learning": ["deep learning", "neural network", "neural networks"],
    "transformers": ["transformers", "hugging face", "huggingface"],
    "mlops": ["mlops", "mlflow", "kubeflow", "model deployment", "model serving", "model monitoring"],
    "model evaluation": ["model evaluation", "evals", "precision", "recall", "f1"],
    "fastapi": ["fastapi"],
    "flask": ["flask"],
    "django": ["django"],
    "react": ["react", "react.js", "reactjs"],
    "typescript": ["typescript"],
    "javascript": ["javascript"],
    "rest apis": ["rest api", "rest apis", "restful", "api integration", "api integrations"],
    "microservices": ["microservices"],
    "sql": ["sql", "postgresql", "postgres", "mysql"],
    "mongodb": ["mongodb"],
    "docker": ["docker"],
    "kubernetes": ["kubernetes", "k8s"],
    "aws": ["aws", "amazon web services"],
    "azure": ["azure"],
    "gcp": ["gcp", "google cloud"],
    "ci/cd": ["ci/cd", "jenkins", "gitlab", "github actions"],
    "terraform": ["terraform"],
    "spark": ["spark", "pyspark"],
    "airflow": ["airflow"],
    "kafka": ["kafka"],
    "snowflake/databricks": ["snowflake", "databricks"],
    "java": ["java"],
    "go": ["golang"],
    "c++": ["c++"],
    "rust": ["rust"],
    "gpu/cuda": ["cuda", "gpu", "gpus"],
    "customer-facing": ["customer-facing", "customer facing", "client-facing", "client facing",
                        "stakeholder", "stakeholders", "solutions engineering", "on-site", "onsite"],
    "data pipelines": ["etl", "data pipeline", "data pipelines", "data engineering"],
}

_PATTERNS = {
    skill: re.compile(r"(?<![a-z0-9+#])(" + "|".join(re.escape(a) for a in aliases) + r")(?![a-z0-9+#])", re.I)
    for skill, aliases in VOCAB.items()
}

CORE_TITLE_HINTS = ("machine learning", "ml ", "ai ", "llm", "forward deployed", "genai", "applied")


def find_skills(text: str) -> dict:
    """skill -> number of mentions (only skills that appear)."""
    out = {}
    for skill, rx in _PATTERNS.items():
        n = len(rx.findall(text))
        if n:
            out[skill] = n
    return out


def load_profile(path="profile/master_resume.json") -> set:
    d = json.load(open(path))
    parts = [d["summary"]]
    for items in d["skills"].values():
        parts.extend(items)
    for job in d["experience"]:
        parts.append(job["title"])
        parts.extend(job["bullets"])
    for p in d.get("projects") or []:
        parts.append(p.get("name", ""))
        parts.extend(p.get("bullets", []))
    return set(find_skills("\n".join(parts)))


def score_job(job: dict, resume_skills: set):
    """Return (score 0-100, matched skills, gap skills)."""
    title = job["title"]
    wanted = find_skills(title + "\n" + title + "\n" + job.get("description", ""))  # title counts double
    if not wanted:
        return 35.0, [], []
    weights = {s: min(n, 3) for s, n in wanted.items()}
    total = sum(weights.values())
    have = sum(w for s, w in weights.items() if s in resume_skills)
    coverage = have / total
    bonus = 5 if any(h in (title.lower() + " ") for h in CORE_TITLE_HINTS) else 0
    score = round(min(100.0, 20 + 75 * coverage + bonus), 1)
    matched = sorted((s for s in weights if s in resume_skills), key=lambda s: -weights[s])
    gaps = sorted((s for s in weights if s not in resume_skills), key=lambda s: -weights[s])
    return score, matched, gaps
