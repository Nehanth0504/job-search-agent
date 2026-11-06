# Job agent (work in progress)

Finds AI/ML and Forward Deployed Engineer postings, keeps only ones open to sponsorship,
scores them against your resume, limits to 2 per company, and emails you each one.

Resume tailoring + ATS-style check: built (see below). Not built yet: auto-apply, Docker/server deploy.

## Try it on your own computer (needs Python 3.8 or newer)

1. Unzip, then open a terminal in the `jobagent` folder.
2. Create an environment and install:

       python3 -m venv .venv
       source .venv/bin/activate          # Windows: .venv\Scripts\activate
       pip install -r requirements.txt

3. Check the logic (should say `13 passed`):

       python -m pytest -q

4. Run one live cycle against the real job boards (alerts print to the screen):

       python pipeline.py --once

   Leaving off `--once` keeps it polling every 10 minutes, but only while your laptop is awake.
   Running 24/7 needs a server; that is the deploy step still to come.

## Dashboard (a website on your own computer)
After running `python pipeline.py --once` at least once, start the dashboard from the same folder:

    pip install -r requirements.txt     # once, to add the new packages
    python dashboard.py

Then open http://127.0.0.1:8000 in your browser. Leave the terminal window open while you use it;
press Ctrl+C there to stop it. It shows every job, lets you mark applications (applied, interview,
offer...), keeps notes and follow-up dates, saves the resume you used for each application, and has a
Portals page where login passwords are stored encrypted behind a master password (see `vault.py`).
Your data lives in `jobs.db` and the `data/` folder next to it, so back those up.

## Watching more companies
The agent reads companies' own public job boards (LinkedIn and Indeed block automated reading). It only sees the
companies listed in `companies.yaml`. To grow that list, add names to `company_candidates.txt` and run:

    python verify_companies.py

It finds each company's board, keeps the ones that exist and saves them. Run it again whenever you add names.

To find companies nobody listed (thousands of US employers), run:

    python discover_boards.py

It uses a free public web index to find company job boards, keeps only boards that currently list your target
roles in the US, and adds them to `companies.yaml`. Rerun it weekly; it remembers what it already checked.

## Listings from LinkedIn, Indeed and other sites (all companies)
No public list of every company's job board exists, so for broad coverage the agent can use the JSearch service,
which collects listings from LinkedIn, Indeed, Glassdoor and company sites. It is paid (about $25/month for 10,000
requests; there is a small free tier). Steps:

1. Create an account at openwebninja.com (or on RapidAPI) and subscribe to JSearch. Copy your API key.
2. Copy `.env.example` to a new file named `.env` in this folder and paste the key after `JSEARCH_API_KEY=`.
   Never paste the key into a chat. The agent reads `.env` automatically each time it starts.
3. If you subscribed through RapidAPI, set `provider: rapidapi` under `sources: jsearch:` in `config.yaml`.
4. `python pipeline.py --once`

Every posting still goes through the same filters (your roles, level, US only, sponsorship) and the 2-per-company cap.

## Tailored resumes
Every alert now comes with a resume made for that job (`tailor.py`), saved in `data/resumes/<job id>/` and shown on the
job's page in the dashboard, with an `ats_report.txt` beside it. It only picks and orders text that is already in
`profile/master_resume.json`; it never adds a skill. To make resumes for jobs you were already alerted about:

    python tailor.py --backfill

The ATS score is a proxy (file parses cleanly + every skill you have that the posting names appears). No real ATS
publishes a score. Skills a posting wants that you lack are listed as gaps and never added.

## Optional: real emails
Set these before step 4 (use a Gmail app password, never your normal password):

    export GMAIL_USER="you@gmail.com"
    export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"

## Files
- `config.yaml` - roles, sponsorship policy, score floor, 2-per-company cap, alert email
- `companies.yaml` - which company job boards to poll (starter list; needs to grow)
- `pipeline.py` - one polling cycle; `fetchers.py` - Greenhouse/Lever/Ashby
- `roles.py` - keeps only your two roles and your experience level (rules are in `config.yaml`)
- `locations.py` - United States only; unclear locations (a bare "Remote") are held for your review
- `sponsorship.py` - rejects "no sponsorship"/citizenship postings
- `h1b_history.py` - employer H-1B filing history from DOL data (see file header)
- `tailor.py` / `ats.py` - per-job resume and its ATS-style check
- `scoring.py` - match score against `profile/master_resume.json`
- `db.py` - tracker and company cap; `notifier.py` - email alerts
- `profile/` - your resume (`resume.docx`) and its parsed form
