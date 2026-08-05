#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer with sudo: sudo ./install.sh"
  exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="raid-webui.service"

if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    mdadm smartmontools python3 python3-venv e2fsprogs
else
  echo "Automatic package installation currently supports Ubuntu and Debian."
  exit 1
fi

python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

sed \
  -e "s|^WorkingDirectory=.*|WorkingDirectory=${APP_DIR}|" \
  -e "s|^ExecStart=.*|ExecStart=${APP_DIR}/.venv/bin/gunicorn --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:5000 --access-logfile - --error-logfile - app:app|" \
  "${APP_DIR}/raid-webui.service" > "/etc/systemd/system/${SERVICE_NAME}"

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

echo
echo "RAID Studio is installed and running on port 5000."
systemctl --no-pager --full status "${SERVICE_NAME}" || true
