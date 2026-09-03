#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "[ERROR] Missing .venv/bin/python" >&2
  echo "Create the Python 3.10 environment according to 服务端口与启动说明.md first." >&2
  exit 1
fi

export COLONY_API_WORKERS=1
export UVICORN_WORKERS=1
export WEB_CONCURRENCY=1
export COLONY_LOG_REDACT_SENSITIVE=1

MVS_ROOT="/opt/MVS"
# Official MvImport does getenv('MVCAM_COMMON_RUNENV') + "/64/libMvCameraControl.so".
if [[ -d "${MVS_ROOT}/lib/64" || -d "${MVS_ROOT}/lib/aarch64" ]]; then
  export MVCAM_COMMON_RUNENV="${MVS_ROOT}/lib"
else
  export MVCAM_COMMON_RUNENV="${MVCAM_COMMON_RUNENV:-${MVS_ROOT}}"
fi
for libdir in "${MVS_ROOT}/lib/64" "${MVS_ROOT}/lib/aarch64" "${MVCAM_COMMON_RUNENV}/64" "${MVCAM_COMMON_RUNENV}/aarch64" "${MVS_ROOT}/lib"; do
  if [[ -d "$libdir" ]]; then
    export LD_LIBRARY_PATH="${libdir}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  fi
done
if [[ -d "${MVS_ROOT}/bin" ]]; then
  export PATH="${MVS_ROOT}/bin${PATH:+:$PATH}"
fi

echo "Validating deployment configuration..."
"$PYTHON" -m workflow.deployment_preflight
"$PYTHON" -m workflow.config_validator

echo "Starting Colony System API on 0.0.0.0:8000 with one worker..."
exec "$PYTHON" -m uvicorn workflow.api_server:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 1 \
  --lifespan on \
  --timeout-graceful-shutdown 45
