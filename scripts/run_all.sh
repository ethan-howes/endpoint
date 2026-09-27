#!/usr/bin/env bash
# Start every service (ENDPOINT.md section 8). Each is its own FastAPI app on its
# own port; S3 is optional and comes up last because it needs an API key.
#
#   ./scripts/run_all.sh            # orchestrator + S1 + S2
#   ./scripts/run_all.sh --with-s3  # also S3 Vision
#
# Ctrl-C stops everything. Logs go to logs/<service>.log.

set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs

WITH_S3=0
[[ "${1:-}" == "--with-s3" ]] && WITH_S3=1

PY=.venv/bin/python
[[ -x "$PY" ]] || PY=python

PIDS=()
cleanup() {
  echo
  echo "stopping ${#PIDS[@]} process(es)..."
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
  wait 2>/dev/null
  exit 0
}
trap cleanup INT TERM

start() {
  local name=$1 target=$2 port=$3
  echo "starting $name on :$port  -> logs/$name.log"
  "$PY" -m uvicorn "$target" --host 0.0.0.0 --port "$port" \
    >"logs/$name.log" 2>&1 &
  PIDS+=($!)
}

# S1 first: the orchestrator degrades gracefully without it, but there is no
# point starting the orchestrator before the service it depends on exists.
start s1 services.s1_legal_spots.main:app 8001
start s2 services.s2_weather_cover.main:app 8002
[[ $WITH_S3 -eq 1 ]] && start s3 services.s3_vision.main:app 8003
start orchestrator orchestrator.main:app 8000

echo
echo "waiting 3s for startup, then checking health..."
sleep 3
for port in 8000 8001 8002; do
  if curl -fsS "http://localhost:$port/health" >/dev/null 2>&1; then
    echo "  :$port healthy"
  else
    echo "  :$port NOT responding -- see logs/"
  fi
done
[[ $WITH_S3 -eq 1 ]] && curl -fsS "http://localhost:8003/health" >/dev/null 2>&1 \
  && echo "  :8003 healthy" || echo "  :8003 NOT responding -- see logs/s3.log"

echo
echo "orchestrator: http://localhost:8000/docs"
echo "Ctrl-C to stop."
wait
