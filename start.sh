#!/usr/bin/env bash
set -euo pipefail
sudo systemctl start raid-webui.service
sudo systemctl --no-pager --full status raid-webui.service
