#!/usr/bin/env bash
# Grow the watch list in the background: verify every candidate name, then open the websites of the rest.
# Takes several hours for ~14,000 names; safe to stop and rerun (both steps continue where they stopped).
#   bash grow.sh          start in the background
#   bash grow.sh status   show progress
cd "$(dirname "$0")" || exit 1
mkdir -p data/logs
PY="${PYTHON:-$(command -v python)}"
if [ "$1" = "status" ]; then tail -n 5 data/logs/grow.log; exit 0; fi
nohup sh -c "\"$PY\" -u verify_companies.py && \"$PY\" -u find_careers.py" >> data/logs/grow.log 2>&1 &
command -v caffeinate >/dev/null && { caffeinate -i -w $! & disown; }
echo "Growing the watch list in the background. Check progress with: bash grow.sh status"
