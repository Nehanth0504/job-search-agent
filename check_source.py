"""Show what one company's career site returns, to debug a source.

    python check_source.py Microsoft
"""
import sys

import yaml

from fetchers import fetch_all

name = " ".join(sys.argv[1:]).strip().lower()
companies = yaml.safe_load(open("companies.yaml"))["companies"]
match = [dict(c, max_jobs=5) for c in companies if c["name"].lower() == name]
if not match:
    sys.exit("No company with that name in companies.yaml.")
jobs = list(fetch_all(match))
print(f"{match[0]['name']}: {len(jobs)} postings read")
for j in jobs:
    print(f"  - {j['title']} | {j['location']} | {j['url']} | description {len(j['description'])} chars")
if not jobs:
    print("Nothing came back. Send me any '[fetch] ... failed' line above.")
