#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
VENV="$BACKEND_DIR/.venv"
LOG_BACKEND="/tmp/ziprun-backend.log"
LOG_FRONTEND="/tmp/ziprun-frontend.log"

free_port() {
  local port="$1"
  local pids
  pids="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    echo "Stopping whatever is already on port $port..."
    kill -9 $pids 2>/dev/null || true
    sleep 1
  fi
}

wait_for() {
  local name="$1" url="$2" tries="${3:-30}"
  for _ in $(seq 1 "$tries"); do
    if curl -sf -m 2 -o /dev/null "$url"; then
      echo "$name is up ($url)"
      return 0
    fi
    sleep 1
  done
  echo "ERROR: $name did not come up. Check its log:"
  [ "$name" = "backend" ] && echo "  tail $LOG_BACKEND" || echo "  tail $LOG_FRONTEND"
  return 1
}

echo "== ZipRun start =="

if ! docker version >/dev/null 2>&1; then
  echo "WARNING: Docker is not reachable. Start Docker Desktop, then re-run ./start.sh"
fi

echo "== Backend: installing dependencies into backend/.venv =="
cd "$BACKEND_DIR"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv .venv
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r requirements.txt
"$VENV/bin/python" -c "import docker, fastapi, uvicorn" >/dev/null

echo "== Frontend: installing dependencies =="
cd "$FRONTEND_DIR"
if [ ! -d node_modules ]; then
  npm install --silent
fi

free_port 8001
free_port 5173

echo "== Starting backend on 127.0.0.1:8001 =="
cd "$BACKEND_DIR"
( nohup "$VENV/bin/python" -m uvicorn main:app --host 127.0.0.1 --port 8001 >"$LOG_BACKEND" 2>&1 & )

echo "== Starting frontend on 127.0.0.1:5173 =="
cd "$FRONTEND_DIR"
( nohup npm run dev >"$LOG_FRONTEND" 2>&1 & )

wait_for backend "http://127.0.0.1:8001/api/health"
wait_for frontend "http://127.0.0.1:5173/"

echo
echo "Open the app:  http://127.0.0.1:5173"
echo "API root:      http://127.0.0.1:8001"
echo "Backend log:   tail -f $LOG_BACKEND"
echo "Frontend log:  tail -f $LOG_FRONTEND"
