#!/bin/bash
# Startup script for RAID WebUI

cd /home/bizon/raid_managment_software_2025
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
fi
python3 app.py
