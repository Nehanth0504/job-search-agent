#!/usr/bin/env bash
# Keep the job agent and the dashboard running in the background (survives closing Terminal).
#   bash run.sh start    bash run.sh stop    bash run.sh status    bash run.sh logs
cd "$(dirname "$0")" || exit 1
mkdir -p data/logs
PY="${PYTHON:-$(command -v python)}"

loop() {   # name, command...  -> restarts the command 10 seconds after any crash
  name="$1"; shift
  while true; do
    "$@" >> "data/logs/$name.log" 2>&1
    echo "$(date '+%F %T') $name exited; restarting in 10s" >> "data/logs/$name.log"
    sleep 10
  done
}

case "$1" in
  start)
    if [ -f data/run.pid ] && kill -0 "$(cat data/run.pid)" 2>/dev/null; then echo "Already running."; exit 0; fi
    ( loop agent "$PY" -u pipeline.py ) & echo $! > data/agent.pid
    ( loop dashboard "$PY" -u dashboard.py ) & echo $! > data/dashboard.pid
    echo "$(cat data/agent.pid)" > data/run.pid
    command -v caffeinate >/dev/null && { caffeinate -i -w "$(cat data/agent.pid)" & disown; }   # keep the Mac awake
    echo "Started. Dashboard: http://127.0.0.1:8000   Logs: bash run.sh logs"
    ;;
  stop)
    for n in agent dashboard; do
      [ -f data/$n.pid ] && pkill -P "$(cat data/$n.pid)" 2>/dev/null; [ -f data/$n.pid ] && kill "$(cat data/$n.pid)" 2>/dev/null
      rm -f data/$n.pid
    done
    rm -f data/run.pid; echo "Stopped." ;;
  status)
    for n in agent dashboard; do
      if [ -f data/$n.pid ] && kill -0 "$(cat data/$n.pid)" 2>/dev/null; then echo "$n: running"; else echo "$n: not running"; fi
    done ;;
  logs) tail -n 40 -f data/logs/agent.log ;;
  *) echo "usage: bash run.sh start|stop|status|logs" ;;
esac
