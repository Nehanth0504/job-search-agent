"""Draft additions for keywords a posting wants that your resume lacks.

Nothing here is added automatically: the dashboard shows these drafts on the resume editor, you tick the ones
you want for that job, and only then are they put into that job's tailored resume (never the master resume,
never an emailed resume). Edit the wording afterwards so every line is something you can speak to.
"""
import re

from scoring import find_skills

# skill -> (how it is listed in Skills, the Skills group it belongs to, two draft bullets)
# {d} = the domain of your current role (e.g. "healthcare"), {app} = a short name for the systems you work on
DRAFTS = {
    "langchain/agents": ("LangChain, AI Agents", "Generative AI", [
        "Built LLM agent workflows with LangChain, defining tools, prompt chains and memory to automate multi-step {d} support tasks.",
        "Integrated agent tool-calling with internal REST APIs so the assistant could look up records and return grounded, auditable answers."]),
    "llm": ("Large Language Models (LLMs)", "Generative AI", [
        "Integrated large language models into {d} applications to summarize documents and answer user questions over internal data.",
        "Evaluated LLM responses for accuracy and relevance, tuning prompts and retrieval settings to reduce hallucinated output."]),
    "rag": ("Retrieval-Augmented Generation (RAG)", "Generative AI", [
        "Built a retrieval-augmented generation pipeline that chunks, embeds and retrieves internal documents to ground LLM answers.",
        "Tuned chunk size, top-k retrieval and re-ranking to improve answer relevance on {d} knowledge-base questions."]),
    "prompt engineering": ("Prompt Engineering", "Generative AI", [
        "Designed and versioned prompt templates with system instructions, few-shot examples and retrieved context for consistent LLM output.",
        "Ran prompt A/B comparisons against a test set of real user questions to pick the most accurate prompt version."]),
    "embeddings": ("Text Embeddings", "Generative AI", [
        "Generated text embeddings for {d} documents and stored them for semantic search and similarity matching.",
        "Compared embedding models on retrieval quality before selecting one for production use."]),
    "vector search": ("Vector Databases (FAISS, Pinecone)", "Data Processing & Retrieval", [
        "Implemented vector similarity search over document embeddings to retrieve the most relevant context for user queries.",
        "Indexed and maintained a vector store, handling incremental updates as source documents changed."]),
    "fine-tuning": ("Model Fine-Tuning (LoRA)", "AI/ML Frameworks & Libraries", [
        "Fine-tuned an open-source language model with LoRA on domain-specific examples to improve output format and terminology.",
        "Prepared and cleaned labeled training data and compared fine-tuned results against the base model."]),
    "openai": ("OpenAI / Azure OpenAI APIs", "Generative AI", [
        "Integrated OpenAI / Azure OpenAI chat and embedding APIs into backend services with retries, timeouts and token-usage logging.",
        "Implemented structured (JSON) outputs from the OpenAI API to feed model results directly into downstream workflows."]),
    "nlp": ("Natural Language Processing (NLP)", "AI/ML Frameworks & Libraries", [
        "Applied NLP techniques (tokenization, entity extraction, text classification) to process unstructured {d} text.",
        "Built text preprocessing pipelines that normalize and clean documents before indexing and model inference."]),
    "deep learning": ("Deep Learning", "AI/ML Frameworks & Libraries", [
        "Trained and evaluated neural network models for text classification, tracking precision, recall and F1.",
        "Experimented with network architectures and hyperparameters to improve validation accuracy."]),
    "transformers": ("Hugging Face Transformers", "AI/ML Frameworks & Libraries", [
        "Used Hugging Face Transformers to run pre-trained language models for classification and summarization tasks.",
        "Packaged transformer-based inference behind a FastAPI endpoint for use by other services."]),
    "pytorch": ("PyTorch", "AI/ML Frameworks & Libraries", [
        "Built and trained models in PyTorch, writing custom datasets, training loops and evaluation scripts.",
        "Optimized PyTorch inference with batching to reduce response latency."]),
    "tensorflow": ("TensorFlow, Keras", "AI/ML Frameworks & Libraries", [
        "Developed and trained models with TensorFlow/Keras and exported them for serving.",
        "Monitored training with validation metrics and early stopping to avoid overfitting."]),
    "scikit-learn": ("Scikit-learn", "AI/ML Frameworks & Libraries", [
        "Built classification models with scikit-learn pipelines, including feature engineering and cross-validation.",
        "Evaluated models with precision, recall and F1 and documented results for stakeholders."]),
    "pandas/numpy": ("Pandas, NumPy", "AI/ML Frameworks & Libraries", [
        "Used Pandas and NumPy to clean, transform and analyze large datasets for reporting and model inputs.",
        "Automated recurring data preparation steps with reusable Pandas utilities."]),
    "mlops": ("MLOps (MLflow, model deployment)", "DevOps & CI/CD", [
        "Tracked experiments and model versions with MLflow and promoted approved models to production.",
        "Deployed models as containerized services with health checks and monitoring of prediction quality."]),
    "model evaluation": ("Model Evaluation", "AI/ML Frameworks & Libraries", [
        "Built evaluation sets and automated scoring to measure model and LLM output quality before each release.",
        "Reported precision, recall and error analysis to guide model and prompt improvements."]),
    "python": ("Python", "Programming Languages", [
        "Developed Python services and automation scripts for {d} data processing and API integrations.",
        "Wrote reusable, tested Python modules following clean-code and typing practices."]),
    "fastapi": ("FastAPI", "Backend Development", [
        "Built asynchronous REST APIs with FastAPI and Pydantic models for validation and clear OpenAPI documentation.",
        "Added authentication, error handling and structured logging to FastAPI services."]),
    "flask": ("Flask", "Backend Development", [
        "Developed Flask web services and REST endpoints for internal applications.",
        "Implemented request validation, error handling and unit tests for Flask APIs."]),
    "django": ("Django, Django REST Framework", "Backend Development", [
        "Built Django and Django REST Framework APIs with serializers, viewsets and role-based permissions.",
        "Optimized Django ORM queries to reduce page and API response times."]),
    "react": ("React.js", "Frontend Development", [
        "Built React components and forms that consume REST APIs and handle loading and error states.",
        "Improved React UI performance with memoization and component restructuring."]),
    "typescript": ("TypeScript", "Programming Languages", [
        "Developed typed front-end and Node.js code in TypeScript to catch integration errors at build time.",
        "Defined shared TypeScript interfaces for API payloads between frontend and backend."]),
    "javascript": ("JavaScript (ES6+)", "Programming Languages", [
        "Wrote modern JavaScript (ES6+) for interactive UI features and API integrations.",
        "Refactored legacy JavaScript into modular, testable components."]),
    "rest apis": ("RESTful APIs", "Backend Development", [
        "Designed and integrated RESTful APIs between internal systems and third-party services.",
        "Documented API contracts and handled versioning, pagination and error responses."]),
    "microservices": ("Microservices", "Backend Development", [
        "Developed microservices that communicate over REST, each with its own deployment and monitoring.",
        "Split a monolithic module into independent services to simplify releases."]),
    "sql": ("SQL, PostgreSQL", "Databases", [
        "Wrote and optimized SQL queries and indexes in PostgreSQL for reporting and application features.",
        "Designed relational schemas and migrations for new application modules."]),
    "mongodb": ("MongoDB", "Databases", [
        "Modeled and queried document data in MongoDB, including indexes for frequent lookups.",
        "Built aggregation pipelines in MongoDB for reporting dashboards."]),
    "docker": ("Docker", "DevOps & CI/CD", [
        "Containerized Python services with Docker for consistent local, test and production environments.",
        "Wrote multi-stage Dockerfiles to shrink image size and speed up deployments."]),
    "kubernetes": ("Kubernetes", "DevOps & CI/CD", [
        "Deployed and scaled containerized services on Kubernetes using deployments, services and config maps.",
        "Configured liveness/readiness probes and resource limits for reliable rollouts."]),
    "aws": ("AWS (EC2, S3, Lambda)", "Cloud Technologies", [
        "Deployed and operated services on AWS using EC2, S3 and Lambda.",
        "Built event-driven processing with AWS Lambda and S3 triggers."]),
    "azure": ("Microsoft Azure", "Cloud Technologies", [
        "Deployed applications and AI services on Microsoft Azure, including Azure OpenAI.",
        "Managed Azure resources, configuration and access for development and production environments."]),
    "gcp": ("Google Cloud Platform (GCP)", "Cloud Technologies", [
        "Deployed services on Google Cloud Platform and used managed storage and compute services.",
        "Set up GCP service accounts, IAM roles and monitoring for applications."]),
    "ci/cd": ("CI/CD Pipelines", "DevOps & CI/CD", [
        "Built CI/CD pipelines that run tests, build containers and deploy automatically on merge.",
        "Added automated quality gates (tests, linting) to pipelines to catch issues before release."]),
    "terraform": ("Terraform", "DevOps & CI/CD", [
        "Provisioned cloud infrastructure as code with Terraform modules and remote state.",
        "Reviewed Terraform plans in pull requests to make infrastructure changes safe and repeatable."]),
    "spark": ("Apache Spark (PySpark)", "Data Processing & Retrieval", [
        "Processed large datasets with PySpark, writing transformations and aggregations for downstream analytics.",
        "Tuned Spark jobs (partitioning, caching) to reduce processing time."]),
    "airflow": ("Apache Airflow", "Data Processing & Retrieval", [
        "Orchestrated data pipelines with Apache Airflow DAGs, including retries, alerts and dependencies.",
        "Migrated cron-based jobs into Airflow for visibility and reliability."]),
    "kafka": ("Apache Kafka", "Data Processing & Retrieval", [
        "Built Kafka producers and consumers to stream events between services.",
        "Handled consumer offsets, retries and dead-letter topics for reliable event processing."]),
    "snowflake/databricks": ("Snowflake, Databricks", "Databases", [
        "Built data models and transformations in Snowflake/Databricks for analytics and ML features.",
        "Optimized warehouse queries and storage to reduce compute cost."]),
    "java": ("Java", "Programming Languages", [
        "Developed and maintained Java services and integrated them with Python components.",
        "Wrote unit tests and fixed defects in Java application modules."]),
    "go": ("Go (Golang)", "Programming Languages", [
        "Built lightweight Go services for high-throughput API workloads.",
        "Wrote concurrent Go code using goroutines and channels for parallel processing."]),
    "c++": ("C++", "Programming Languages", [
        "Developed performance-critical components in C++.",
        "Profiled and optimized C++ code paths to reduce latency."]),
    "rust": ("Rust", "Programming Languages", [
        "Built performance-sensitive tooling in Rust.",
        "Used Rust's type system and ownership model to write memory-safe services."]),
    "gpu/cuda": ("GPU Computing (CUDA)", "AI/ML Frameworks & Libraries", [
        "Ran model training and inference on GPUs, managing batch sizes and memory for throughput.",
        "Profiled GPU utilization to remove bottlenecks in model serving."]),
    "customer-facing": ("Customer-Facing Delivery", "Development Tools & Methodologies", [
        "Worked directly with business stakeholders and end users to gather requirements and deliver solutions in their environment.",
        "Led technical walkthroughs and demos for stakeholders and turned their feedback into prioritized changes."]),
    "data pipelines": ("Data Pipelines / ETL", "Data Processing & Retrieval", [
        "Built ETL pipelines to extract, validate and load data from multiple sources into application databases.",
        "Added data-quality checks and alerting to pipelines to catch bad records early."]),
}

DOMAINS = [("healthcare", "healthcare"), ("member", "healthcare"), ("financial", "financial services"),
           ("payment", "financial services"), ("licens", "government"), ("child", "government")]


def _domain(resume):
    text = " ".join(resume["experience"][0]["bullets"]).lower() if resume.get("experience") else ""
    for word, d in DOMAINS:
        if word in text:
            return d
    return "business"


def _text(resume):
    parts = [resume.get("summary", "")]
    for items in resume.get("skills", {}).values():
        parts.extend(items)
    for j in resume.get("experience", []):
        parts.append(j.get("title", ""))
        parts.extend(j.get("bullets", []))
    for p in resume.get("projects") or []:
        parts.append(p.get("name", ""))
        parts.extend(p.get("bullets", []))
    return "\n".join(parts)


def missing(resume, job):
    """Skills the posting wants (most-mentioned first) that this resume does not show."""
    wanted = find_skills(job["title"] + "\n" + job["title"] + "\n" + (job.get("description") or ""))
    have = set(find_skills(_text(resume)))
    return [s for s in sorted(wanted, key=lambda s: -wanted[s]) if s not in have and s in DRAFTS]


def drafts(resume, job):
    """[{skill, label, group, bullets}] for every missing skill."""
    d = _domain(resume)
    out = []
    for s in missing(resume, job):
        label, group, bullets = DRAFTS[s]
        out.append(dict(skill=s, label=label, group=group, bullets=[b.format(d=d) for b in bullets]))
    return out


def apply(resume, chosen_skills, chosen_bullets):
    """Add the ticked skills to the Skills section and ticked bullets to your current role. Returns the resume.
    chosen_skills: [skill]; chosen_bullets: [(skill, bullet_text)]."""
    skills = resume.setdefault("skills", {})
    for s in chosen_skills:
        label, group, _ = DRAFTS[s]
        target = next((g for g in skills if g.lower() == group.lower()), None) or \
            next((g for g in skills if group.split()[0].lower() in g.lower()), None)
        if target is None:
            target = group
            skills[target] = []
        for item in [x.strip() for x in re.split(r",\s*(?![^()]*\))", label) if x.strip()]:
            if item.lower() not in {x.lower() for x in skills[target]}:
                skills[target].append(item)
    if chosen_bullets and resume.get("experience"):
        cur = resume["experience"][0]
        for _, b in chosen_bullets:
            if b not in cur["bullets"]:
                cur["bullets"].append(b)
    return resume
