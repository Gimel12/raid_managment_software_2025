#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer with sudo: sudo ./install.sh"
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/raid-studio"
COCKPIT_DIR="/usr/local/share/cockpit/raid_studio"
SERVICE_NAME="raid-webui.service"

if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    mdadm smartmontools python3 python3-venv e2fsprogs xfsprogs btrfs-progs
else
  echo "Automatic package installation currently supports Ubuntu and Debian."
  exit 1
fi

if [[ -d "${APP_DIR}" ]]; then
  BACKUP_DIR="/var/backups/raid-studio-$(date -u +%Y%m%dT%H%M%SZ)"
  cp -a "${APP_DIR}" "${BACKUP_DIR}"
  echo "Existing installation backed up to ${BACKUP_DIR}"
fi

install -d -m 0755 "${APP_DIR}/templates" "${APP_DIR}/static/css" "${APP_DIR}/static/js"
install -d -m 0700 /var/backups/raid-studio
install -m 0755 "${SOURCE_DIR}/app.py" "${APP_DIR}/app.py"
install -m 0644 "${SOURCE_DIR}/requirements.txt" "${APP_DIR}/requirements.txt"
install -m 0644 "${SOURCE_DIR}/templates/index.html" "${APP_DIR}/templates/index.html"
install -m 0644 "${SOURCE_DIR}/static/css/style.css" "${APP_DIR}/static/css/style.css"
install -m 0644 "${SOURCE_DIR}/static/js/app.js" "${APP_DIR}/static/js/app.js"

python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

install -m 0644 "${SOURCE_DIR}/raid-webui.service" "/etc/systemd/system/${SERVICE_NAME}"

install -d -m 0755 "${COCKPIT_DIR}/static/css" "${COCKPIT_DIR}/static/js"
install -m 0644 "${SOURCE_DIR}/cockpit/manifest.json" "${COCKPIT_DIR}/manifest.json"
install -m 0644 "${SOURCE_DIR}/templates/index.html" "${COCKPIT_DIR}/index.html"
install -m 0644 "${SOURCE_DIR}/static/css/style.css" "${COCKPIT_DIR}/static/css/style.css"
install -m 0644 "${SOURCE_DIR}/static/js/app.js" "${COCKPIT_DIR}/static/js/app.js"

chown -R root:root "${APP_DIR}" "${COCKPIT_DIR}"
find "${APP_DIR}" "${COCKPIT_DIR}" -type d -exec chmod 0755 {} +
find "${APP_DIR}" "${COCKPIT_DIR}" -type f -exec chmod go-w {} +

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

echo
echo "RAID Studio is installed inside Cockpit."
echo "Open https://SERVER_IP:9090/raid_studio"
systemctl --no-pager --full status "${SERVICE_NAME}" || true
