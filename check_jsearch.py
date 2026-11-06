"""One test call to the listing service: shows whether it returns jobs from many different companies.

    python check_jsearch.py
"""
import os
import sys

import yaml

from fetchers import PROVIDERS, _jsearch_job
from pipeline import load_env

load_env()
cfg = yaml.safe_load(open("config.yaml"))
src = (cfg.get("sources") or {}).get("jsearch") or {}
key = os.environ.get("JSEARCH_API_KEY", "")
if not key:
    sys.exit("No JSEARCH_API_KEY found. Open the .env file in this folder and paste the key after JSEARCH_API_KEY=")
import httpx

url, headers = PROVIDERS[src.get("provider", "openwebninja")]
for q in ("Forward Deployed Engineer", "Software Engineer AI ML"):
    r = httpx.get(url, headers=headers(key), timeout=40,
                  params={"query": f"{q} in United States", "page": 1, "num_pages": 1, "country": "us",
                          "date_posted": "week"})
    print(f"\n{q!r}: HTTP {r.status_code}")
    if r.status_code != 200:
        print("  ", r.text[:300])
        continue
    rows = (r.json() or {}).get("data") or []
    jobs = [j for j in (_jsearch_job(x) for x in rows) if j]
    print(f"  {len(rows)} results, {len({j['company'] for j in jobs})} different companies")
    for j in jobs[:10]:
        print(f"  - {j['company']} | {j['title']} | {j['location']}")
