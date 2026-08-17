#!/usr/bin/env bash
set -euo pipefail
systemctl --no-pager --full status raid-webui.service
echo
if [[ -S /run/raid-studio/raid-studio.sock ]]; then
  echo "Private API socket: ready"
else
  echo "Private API socket: missing"
  exit 1
fi
echo "Network exposure: Cockpit only (no standalone RAID Studio port)"
