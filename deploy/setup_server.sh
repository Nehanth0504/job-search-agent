#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu 24.04 server. Run as root from /opt/jobagent:
#     bash deploy/setup_server.sh
# Installs Python, the agent and the dashboard as always-on services (restart on crash and on reboot),
# a private network (Tailscale) so only your own devices can open the dashboard, a firewall, and daily backups.
set -euo pipefail
APP=/opt/jobagent
cd "$APP"

echo "== 1/7 System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip sqlite3 ufw curl > /dev/null
timedatectl set-timezone America/Chicago || true
if [ ! -f /swapfile ]; then                      # small servers: avoid running out of memory
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== 2/7 Service user and Python packages"
id jobagent >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin jobagent
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
grep -v -E '^(pandas|#)' requirements.txt | grep -v '^$' > /tmp/req.txt
"$APP/venv/bin/pip" install -q -r /tmp/req.txt
mkdir -p data/logs backups

echo "== 3/7 Private network (Tailscale)"
if ! command -v tailscale >/dev/null; then curl -fsSL https://tailscale.com/install.sh | sh >/dev/null; fi
if ! tailscale status >/dev/null 2>&1; then
  echo ">>> Open the link below and sign in with the same account you use on your Mac/phone:"
  tailscale up --hostname=jobagent
fi
TSIP=$(tailscale ip -4 | head -1)

echo "== 4/7 Dashboard settings in .env"
touch .env
grep -q '^DASHBOARD_HOST=' .env || echo "DASHBOARD_HOST=0.0.0.0" >> .env
TSNAME=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
sed -i '/^DASHBOARD_ALLOWED_HOSTS=/d' .env
echo "DASHBOARD_ALLOWED_HOSTS=127.0.0.1,localhost,jobagent,$TSIP,$TSNAME" >> .env
if ! grep -q '^DASHBOARD_PASSWORD=.' .env; then
  read -r -s -p "Choose a dashboard password: " PW; echo
  echo "DASHBOARD_PASSWORD=$PW" >> .env
fi
chmod 600 .env

echo "== 5/7 Firewall: SSH from anywhere, dashboard only over Tailscale"
ufw allow OpenSSH >/dev/null
ufw allow in on tailscale0 to any port 8000 proto tcp >/dev/null
ufw --force enable >/dev/null

echo "== 6/7 Always-on services (memory-capped, so the dashboard and Tailscale always stay up)"
chown -R jobagent:jobagent "$APP"
unit() {  # name, script, restart policy, extra [Service] lines
  cat > /etc/systemd/system/jobagent-$1.service <<UNIT
[Unit]
Description=Job agent ($1)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=jobagent
WorkingDirectory=$APP
EnvironmentFile=$APP/.env
ExecStart=$2
Restart=$3
RestartSec=10
StandardOutput=append:$APP/data/logs/$1.log
StandardError=append:$APP/data/logs/$1.log
$4

[Install]
WantedBy=multi-user.target
UNIT
}
unit agent     "$APP/venv/bin/python -u $APP/pipeline.py"   always     "MemoryHigh=700M
MemoryMax=900M"
unit dashboard "$APP/venv/bin/python -u $APP/dashboard.py"  always     "OOMScoreAdjust=-800"
unit grow      "/bin/sh -c '$APP/venv/bin/python -u $APP/find_workday.py; $APP/venv/bin/python -u $APP/discover_boards.py; $APP/venv/bin/python -u $APP/verify_companies.py; $APP/venv/bin/python -u $APP/find_careers.py'" \
               on-failure "MemoryMax=500M
Nice=10"
mkdir -p /etc/systemd/system/tailscaled.service.d
TS_PROTECT=/etc/systemd/system/tailscaled.service.d/protect.conf
NEW_PROTECT=0; [ -f "$TS_PROTECT" ] || NEW_PROTECT=1
printf '[Service]\nOOMScoreAdjust=-900\n' > "$TS_PROTECT"
systemctl daemon-reload
systemctl enable jobagent-agent jobagent-dashboard >/dev/null 2>&1
[ "$NEW_PROTECT" = 1 ] && systemctl restart tailscaled      # only the first time; restarting it drops the HTTPS address briefly
systemctl restart jobagent-agent jobagent-dashboard
pkill -u jobagent -f verify_companies.py 2>/dev/null || true; pkill -u jobagent -f find_careers.py 2>/dev/null || true
systemctl restart jobagent-grow     # continues the company search where it stopped; ends by itself when done

echo "== HTTPS address (Tailscale Serve)"
for i in $(seq 1 30); do tailscale status >/dev/null 2>&1 && break; sleep 1; done   # wait until Tailscale is up
SERVE_NOW=$(tailscale serve status 2>/dev/null || true)
if [[ "$SERVE_NOW" == *"127.0.0.1:8000"* ]] || timeout 20 tailscale serve --bg --https=443 http://127.0.0.1:8000 </dev/null >/dev/null 2>&1; then
  URL="https://$TSNAME"
else
  URL="http://jobagent:8000"
  echo "   HTTPS is not enabled for your Tailscale network yet: turn on 'HTTPS Certificates' at"
  echo "   https://login.tailscale.com/admin/dns and run this setup again."
fi

echo "== 7/7 Daily backup of the job database (keeps 14 days)"
cat > /etc/cron.daily/jobagent-backup <<'CRON'
#!/bin/sh
cd /opt/jobagent && sqlite3 jobs.db ".backup backups/jobs-$(date +%F).db" && find backups -name 'jobs-*.db' -mtime +14 -delete
CRON
chmod +x /etc/cron.daily/jobagent-backup

echo
echo "Done. Dashboard (from any device signed in to Tailscale): $URL"
echo "      (also works: http://jobagent:8000  or  http://$TSIP:8000)"
echo "User name: anything; password: the one you just chose."
echo "Check:  systemctl status jobagent-agent    Logs:  tail -f $APP/data/logs/agent.log"
echo "Company search progress:  tail -n 3 $APP/data/logs/grow.log"
