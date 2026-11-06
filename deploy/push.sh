#!/usr/bin/env bash
# Copy the agent from this Mac to the server, then run the setup there.
#     bash deploy/push.sh root@SERVER_IP
# Run it again later to send code changes (your data on the server is kept unless --with-data is given).
set -euo pipefail
DEST="${1:?usage: bash deploy/push.sh root@SERVER_IP [--with-data]}"
cd "$(dirname "$0")/.."
EXCL_ICLOUD=()
# iCloud (Desktop/Documents sync) may have moved files off this Mac; reading each one brings it back first.
# Files that are only in iCloud are listed and requested; regenerable state files are simply skipped.
STUCK=$(find . -type f -flags +dataless -not -path './venv/*' -not -path './data/logs/*' 2>/dev/null || true)
if [ -n "$STUCK" ]; then
  echo "Asking iCloud to download:"; echo "$STUCK"
  echo "$STUCK" | while read -r f; do brctl download "$f" 2>/dev/null || true; done
  sleep 10
  STUCK=$(find . -type f -flags +dataless -not -path './venv/*' -not -path './data/logs/*' 2>/dev/null || true)
  STUCK_CODE=$(echo "$STUCK" | grep -v '^./data/' || true)
  if [ -n "$STUCK_CODE" ]; then
    echo "Still only in iCloud (open Finder, right-click each one > Download Now, then run this again):"
    echo "$STUCK_CODE"; exit 1
  fi
  for f in $STUCK; do EXCL_ICLOUD+=(--exclude "${f#./}"); done
fi
EXCL=(--exclude __pycache__ --exclude .pytest_cache --exclude 'data/logs' --exclude '*.pid' --exclude venv
      --exclude '*.bak' --exclude '*.bak2' --exclude '.DS_Store' "${EXCL_ICLOUD[@]+"${EXCL_ICLOUD[@]}"}")
# Your data on the server (jobs, resumes, watch list, keys) is NEVER overwritten unless you ask with --with-data.
if [ "${2:-}" != "--with-data" ]; then
  if ! ssh -o ConnectTimeout=15 "$DEST" true; then
    echo "Could not reach $DEST. Nothing was sent; try again in a minute."; exit 1
  fi
  EXCL+=(--exclude jobs.db --exclude 'jobs.db-*' --exclude data --exclude .env --exclude companies.yaml
         --exclude profile --exclude backups)
  echo "Sending code only (your data on the server is kept)."
else
  read -r -p "This REPLACES the server's jobs, resumes, watch list and .env with this Mac's copies. Type yes: " ok
  [ "$ok" = "yes" ] || { echo "Cancelled."; exit 1; }
  [ -f data/run.pid ] && bash run.sh stop || true        # freeze the database on this Mac before copying it
  echo "Sending code and data (jobs.db, resumes, H-1B index, .env)."
fi
ssh "$DEST" mkdir -p /opt/jobagent
rsync -az "${EXCL[@]}" ./ "$DEST:/opt/jobagent/"
ssh -t "$DEST" "cd /opt/jobagent && bash deploy/setup_server.sh"
echo
echo "The server now runs the agent 24/7. Do NOT start it on this Mac again (two copies would send duplicate alerts)."
