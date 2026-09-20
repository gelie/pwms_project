#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/pwms/src/pwms"
PYTHON_BIN="/opt/pwms/.venv/bin/python"
LOG_DIR="/var/log/pwms/gunicorn"

cd "${APP_DIR}" || exit 1
mkdir -p "${LOG_DIR}"

# Use venv python directly (avoid user-local uv path under SELinux)
exec "${PYTHON_BIN}" -m gunicorn wsgi:application --config gunicorn_config.py --no-control-socket
