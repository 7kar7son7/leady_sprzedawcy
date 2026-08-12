#!/usr/bin/env bash
# Start panelu na hostingu (PM2 / Docker / systemd)
set -euo pipefail
cd "$(dirname "$0")"
export PORT="${PORT:-8000}"
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
