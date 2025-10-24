#!/bin/bash
echo "=== RAID WebUI Status ==="
echo ""
if pgrep -f "python3 app.py" > /dev/null; then
    echo "✅ WebUI is RUNNING"
    echo ""
    echo "Access at: http://192.168.1.220:5000/"
    echo ""
    ps aux | grep "python3 app.py" | grep -v grep
else
    echo "❌ WebUI is NOT running"
    echo ""
    echo "Start with: cd /home/bizon/raid_managment_software_2025 && python3 app.py"
fi
echo ""
echo "Port status:"
ss -ltnp | grep 5000 || echo "Port 5000 not listening"
