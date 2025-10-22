# RAID Management WebUI

A simple, clean web interface for managing MegaRAID controllers using StorCLI.

## Features

- ✅ View controller information (model, serial, firmware, status)
- ✅ List all physical drives with detailed information
- ✅ List all RAID arrays (virtual drives)
- ✅ Create new RAID arrays (RAID 0, 1, 5, 6, 10)
- ✅ Delete existing RAID arrays
- ✅ **Drive Health Monitoring** - SMART data, temperature, errors
- ✅ **Performance Testing** - Read/write speed tests with fio
- ✅ Real-time status updates
- ✅ Clean, modern Bootstrap UI
- ✅ Mobile-responsive design

## Access

WebUI URL: `http://192.168.1.220:5000/`

Access from any computer on your network using the URL above.

## Quick Start

### Option 1: Run as Service (Recommended - Auto-starts on boot)
```bash
# Install and enable the service
sudo cp raid-webui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable raid-webui
sudo systemctl start raid-webui

# Check status
sudo systemctl status raid-webui
```

### Option 2: Run Manually
```bash
cd /home/bizon/raid-webui
python3 app.py
```

The WebUI will be available at `http://192.168.1.220:5000/`

## Service Management

**Check service status:**
```bash
sudo systemctl status raid-webui
```

**View live logs:**
```bash
sudo journalctl -u raid-webui -f
```

**Restart service:**
```bash
sudo systemctl restart raid-webui
```

**Stop service:**
```bash
sudo systemctl stop raid-webui
```

**Disable auto-start:**
```bash
sudo systemctl disable raid-webui
```

## Auto-Start on Boot

To make the WebUI start automatically on system boot:

```bash
# Install the service
sudo cp /home/bizon/raid-webui/raid-webui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable raid-webui
sudo systemctl start raid-webui

# Check status
sudo systemctl status raid-webui

# View logs
sudo journalctl -u raid-webui -f
```

## Managing the Service

```bash
# Start
sudo systemctl start raid-webui

# Stop
sudo systemctl stop raid-webui

# Restart
sudo systemctl restart raid-webui

# Status
sudo systemctl status raid-webui
```

## Supported RAID Types

- **RAID 0** - Striping (2+ drives, no redundancy, maximum performance)
- **RAID 1** - Mirroring (2+ drives, 50% capacity, full redundancy)
- **RAID 5** - Stripe + Parity (3+ drives, 1 drive failure tolerance)
- **RAID 6** - Double Parity (4+ drives, 2 drive failure tolerance)
- **RAID 10** - Stripe + Mirror (4+ drives, 50% capacity, high performance + redundancy)

## How to Use

### View Status
The main page shows:
- Controller information at the top
- RAID arrays in the middle
- Physical drives at the bottom

### Create RAID Array
1. Click "Create RAID" button
2. Select RAID type from dropdown
3. Check the drives you want to use
4. Click "Create RAID"
5. Confirm the operation

### Delete RAID Array
1. Find the RAID array in the table
2. Click "Delete" button
3. Confirm deletion
4. **WARNING: All data will be lost!**

### Check Drive Health
1. Scroll to "Drive Health Monitor" section
2. Click "Check Health" button
3. View health status for all drives:
   - ✅ **Healthy** - Drive is good
   - ⚠️ **Needs Attention** - Drive has errors
   - Temperature monitoring
   - Power-on hours
   - Media errors count
   - Predictive failure warnings

### Run Performance Test
1. Scroll to "Performance Testing" section
2. Select device to test (RAID array or individual drive)
3. Choose test type:
   - **Quick (10s)** - Fast test
   - **Full (30s)** - More comprehensive
4. Click "Run Test"
5. View results:
   - Read speed (MB/s)
   - Read IOPS
   - Write performance metrics

**Note:** Write tests are disabled for safety on mounted devices

## Technical Details

- **Backend:** Python Flask
- **Frontend:** HTML, CSS, JavaScript with Bootstrap 5
- **RAID Management:** StorCLI (/opt/MegaRAID/storcli/storcli64)
- **Port:** 5000 (default)
- **Location:** /home/bizon/raid-webui

## Files

```
raid-webui/
├── app.py                 # Flask backend
├── templates/
│   └── index.html         # Main HTML template
├── static/
│   ├── css/
│   │   └── style.css     # Custom styles
│   └── js/
│       └── app.js        # Frontend JavaScript
├── requirements.txt       # Python dependencies
├── start.sh              # Startup script
├── raid-webui.service    # Systemd service file
└── README.md             # This file
```

## Troubleshooting

### WebUI not accessible
```bash
# Check if running
ps aux | grep app.py

# Check port
ss -ltnp | grep 5000

# Restart
cd /home/bizon/raid-webui
python3 app.py
```

### StorCLI errors
```bash
# Test StorCLI directly
sudo /opt/MegaRAID/storcli/storcli64 /c0 show

# Check permissions
ls -l /opt/MegaRAID/storcli/storcli64
```

### View application logs
```bash
# If running manually - check the terminal

# If running as service
sudo journalctl -u raid-webui -f
```

## Security Notes

- **This WebUI runs with root privileges** (required for StorCLI)
- **No authentication** is implemented - use firewall rules to restrict access
- **Development server** - for production, use a proper WSGI server like Gunicorn
- **RAID operations are destructive** - always backup data before making changes

## Current RAID Configuration

- **RAID 1 Array (VD 239):** 2x 15TB KIOXIA drives, mounted at /mnt/raid1
- **Available drives:** 2x 30TB KIOXIA drives (unconfigured)

## Support

This is a custom-built tool using the official Broadcom StorCLI utility.
For StorCLI documentation, visit: https://www.broadcom.com/support/knowledgebase

Created: October 21, 2025
