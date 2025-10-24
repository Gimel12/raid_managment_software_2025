#!/usr/bin/env python3
"""
Simple RAID Management WebUI for MegaRAID Controllers
Uses StorCLI for backend operations
"""

from flask import Flask, render_template, jsonify, request
import subprocess
import json
import re

app = Flask(__name__)

# StorCLI path
STORCLI = "/opt/MegaRAID/storcli/storcli64"

def run_storcli(command):
    """Execute StorCLI command and return output"""
    try:
        cmd = f"sudo {STORCLI} {command}"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return result.stdout
    except Exception as e:
        return f"Error: {str(e)}"

def format_state(state):
    """Convert StorCLI state abbreviations to user-friendly names"""
    state_map = {
        'Onln': 'Online',
        'Optl': 'Optimal',
        'UGood': 'Unconfigured Good',
        'UBad': 'Unconfigured Bad',
        'Offln': 'Offline',
        'Dgrd': 'Degraded',
        'Pdgd': 'Partially Degraded',
        'Failed': 'Failed',
        'Rbld': 'Rebuilding',
        'GHS': 'Global Hot Spare',
        'DHS': 'Dedicated Hot Spare'
    }
    return state_map.get(state, state)

def parse_controller_info():
    """Get controller information"""
    output = run_storcli("/c0 show")
    info = {
        "model": "Unknown",
        "serial": "Unknown",
        "firmware": "Unknown",
        "status": "Unknown"
    }
    
    for line in output.split('\n'):
        if "Product Name" in line:
            info["model"] = line.split('=')[1].strip() if '=' in line else "Unknown"
        elif "Serial Number" in line:
            info["serial"] = line.split('=')[1].strip() if '=' in line else "Unknown"
        elif "FW Version" in line:
            info["firmware"] = line.split('=')[1].strip() if '=' in line else "Unknown"
        elif "Controller = 0" in line:
            info["status"] = "Online"
    
    return info

def parse_physical_drives():
    """Get list of physical drives"""
    output = run_storcli("/c0/eall/sall show")
    drives = []
    
    in_drive_section = False
    for line in output.split('\n'):
        if "EID:Slt" in line and "DID" in line:
            in_drive_section = True
            continue
        
        if in_drive_section and line.strip() and not line.startswith('-'):
            if "EID=" in line or not line.strip():
                break
                
            parts = line.split()
            if len(parts) >= 8:
                drives.append({
                    "slot": parts[0],
                    "did": parts[1],
                    "state": format_state(parts[2]),
                    "dg": parts[3],
                    "size": parts[4] + " " + parts[5],
                    "interface": parts[6],
                    "media": parts[7],
                    "model": " ".join(parts[11:13]) if len(parts) > 12 else (" ".join(parts[11:]) if len(parts) > 11 else "Unknown")
                })
    
    return drives

def parse_virtual_drives():
    """Get list of virtual drives (RAID arrays)"""
    output = run_storcli("/c0/vall show")
    vdrives = []
    mount_info = get_mount_info()
    
    in_vd_section = False
    for line in output.split('\n'):
        if "DG/VD" in line and "TYPE" in line:
            in_vd_section = True
            continue
        
        if in_vd_section and line.strip() and not line.startswith('-'):
            if "VD=" in line or "DG=" in line or not line.strip():
                break
                
            parts = line.split()
            if len(parts) >= 9:
                # Extract VD number to get detailed info
                vd_num = parts[0].split('/')[-1]
                
                # Get OS device name from detailed VD info
                vd_detail = run_storcli(f"/c0/v{vd_num} show all")
                os_device = ""
                for detail_line in vd_detail.split('\n'):
                    if "OS Drive Name" in detail_line and '=' in detail_line:
                        os_device = detail_line.split('=')[-1].strip()
                        break
                
                # Check mount status and filesystem
                mount_point = mount_info.get(os_device, "")
                filesystem = check_filesystem(os_device) if os_device else None
                
                vdrives.append({
                    "dg_vd": parts[0],
                    "type": parts[1],
                    "state": format_state(parts[2]),
                    "access": parts[3],
                    "size": parts[8] + " " + parts[9] if len(parts) > 9 else parts[8],
                    "device": os_device if os_device else "N/A",
                    "mount_point": mount_point,
                    "filesystem": filesystem,
                    "mounted": bool(mount_point),
                    "name": " ".join(parts[10:]).strip() if len(parts) > 10 else ""
                })
    
    return vdrives

def list_block_devices():
    """List top-level block devices (e.g., /dev/sd*, /dev/nvme*n1)."""
    devices = []
    try:
        # List block devices of type 'disk' only, names only
        result = subprocess.run("lsblk -ndo NAME,TYPE", shell=True, capture_output=True, text=True)
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == 'disk':
                name = parts[0].strip()
                if name:
                    devices.append(f"/dev/{name}")
    except Exception:
        pass
    return devices

@app.route('/api/test_devices')
def get_test_devices():
    """Return list of devices available for performance testing."""
    devices = set()
    # Add VD OS devices from storcli parsing
    try:
        for vd in parse_virtual_drives():
            dev = vd.get('device')
            if dev and dev != 'N/A':
                devices.add(dev)
    except Exception:
        pass
    # Add system block devices
    for dev in list_block_devices():
        devices.add(dev)
    # Sort for stable UI
    device_list = sorted(devices)
    return jsonify(device_list)

@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')

@app.route('/api/controller')
def get_controller():
    """API endpoint for controller info"""
    info = parse_controller_info()
    return jsonify(info)

@app.route('/api/drives')
def get_drives():
    """API endpoint for physical drives"""
    drives = parse_physical_drives()
    return jsonify(drives)

@app.route('/api/vdrives')
def get_vdrives():
    """API endpoint for virtual drives"""
    vdrives = parse_virtual_drives()
    return jsonify(vdrives)

@app.route('/api/create_raid', methods=['POST'])
def create_raid():
    """Create a new RAID array"""
    data = request.json
    raid_type = data.get('type', 'raid1')
    drives = data.get('drives', [])
    
    if not drives:
        return jsonify({"success": False, "error": "No drives selected"})
    
    # Format drives list for StorCLI
    drive_list = ",".join(drives)
    
    # Create RAID command
    command = f"/c0 add vd type={raid_type} drives={drive_list}"
    output = run_storcli(command)
    
    success = "Success" in output or "Succeeded" in output
    
    return jsonify({
        "success": success,
        "output": output
    })

@app.route('/api/delete_raid', methods=['POST'])
def delete_raid():
    """Delete a RAID array"""
    data = request.json
    vd_id = data.get('vd_id', '')
    
    if not vd_id:
        return jsonify({"success": False, "error": "No VD ID provided"})
    
    # Extract VD number from DG/VD format (e.g., "0/239" -> "239")
    vd_num = vd_id.split('/')[-1] if '/' in vd_id else vd_id
    
    command = f"/c0/v{vd_num} del force"
    output = run_storcli(command)
    
    success = "Success" in output or "Succeeded" in output
    
    return jsonify({
        "success": success,
        "output": output
    })

def get_drive_health():
    """Get SMART health status for all drives"""
    drives_health = []
    
    # Get list of physical drives first
    pd_output = run_storcli("/c0/eall/sall show")
    drive_slots = []
    
    for line in pd_output.split('\n'):
        if line.strip() and "252:" in line:
            parts = line.split()
            if len(parts) > 0:
                drive_slots.append(parts[0])
    
    # Get detailed info for each drive including SMART status
    for slot in drive_slots:
        eid, slt = slot.split(':')
        smart_output = run_storcli(f"/c0/e{eid}/s{slt} show all")
        
        health_info = {
            "slot": slot,
            "status": "Unknown",
            "temperature": "N/A",
            "power_on_hours": "N/A",
            "media_errors": "0",
            "predictive_failures": "0",
            "overall_health": "Good"
        }
        
        # Parse SMART attributes
        for line in smart_output.split('\n'):
            if "Drive Temperature" in line:
                temp = line.split('=')[-1].strip() if '=' in line else "N/A"
                health_info["temperature"] = temp
            elif "Power On Hours" in line or "Power_On_Hours" in line:
                poh = line.split('=')[-1].strip() if '=' in line else "N/A"
                health_info["power_on_hours"] = poh
            elif "Media Error Count" in line:
                errors = line.split('=')[-1].strip() if '=' in line else "0"
                health_info["media_errors"] = errors
            elif "Predictive Failure Count" in line:
                failures = line.split('=')[-1].strip() if '=' in line else "0"
                health_info["predictive_failures"] = failures
        
        # Determine overall health
        if int(health_info["media_errors"]) > 0 or int(health_info["predictive_failures"]) > 0:
            health_info["overall_health"] = "Warning"
            health_info["status"] = "⚠️ Needs Attention"
        else:
            health_info["overall_health"] = "Good"
            health_info["status"] = "✅ Healthy"
        
        drives_health.append(health_info)
    
    return drives_health

def run_speed_test(device, test_type="quick"):
    """Run fio speed test on a device"""
    import tempfile
    import os
    
    # Determine test parameters based on type
    if test_type == "quick":
        duration = "10"
        size = "1G"
    else:
        duration = "30"
        size = "4G"
    
    results = {
        "device": device,
        "read_speed": "N/A",
        "write_speed": "N/A",
        "read_iops": "N/A",
        "write_iops": "N/A",
        "status": "Running"
    }
    
    try:
        # Sequential read test
        read_cmd = f"sudo fio --name=seqread --filename={device} --direct=1 --rw=read --bs=1M --size={size} --runtime={duration} --time_based --output-format=json"
        read_result = subprocess.run(read_cmd, shell=True, capture_output=True, text=True, timeout=int(duration)+10)
        
        if read_result.returncode == 0:
            try:
                read_data = json.loads(read_result.stdout)
                read_bw = read_data['jobs'][0]['read']['bw'] / 1024  # Convert to MB/s
                read_iops = read_data['jobs'][0]['read']['iops']
                results["read_speed"] = f"{read_bw:.2f} MB/s"
                results["read_iops"] = f"{int(read_iops)}"
            except:
                pass
        
        # Sequential write test (only if not mounted - skip for safety)
        # We'll only test read for mounted devices
        results["write_speed"] = "Skipped (safety)"
        results["write_iops"] = "Skipped (safety)"
        results["status"] = "Completed"
        
    except Exception as e:
        results["status"] = f"Error: {str(e)}"
    
    return results

def get_mount_info():
    """Get mount information for all devices"""
    mount_info = {}
    
    # Get all mounted filesystems
    try:
        result = subprocess.run("mount | grep '^/dev/'", shell=True, capture_output=True, text=True)
        for line in result.stdout.split('\n'):
            if line.strip():
                parts = line.split()
                if len(parts) >= 3:
                    device = parts[0]
                    mount_point = parts[2]
                    mount_info[device] = mount_point
    except:
        pass
    
    return mount_info

def check_filesystem(device):
    """Check if device has a filesystem"""
    try:
        result = subprocess.run(f"sudo blkid {device}", shell=True, capture_output=True, text=True)
        if "TYPE=" in result.stdout:
            # Extract filesystem type
            for part in result.stdout.split():
                if part.startswith("TYPE="):
                    return part.split("=")[1].strip('"')
        return None
    except:
        return None

def mount_device(device, mount_point, filesystem=None):
    """Mount a device to a mount point"""
    try:
        # Check if filesystem exists
        if not filesystem:
            filesystem = check_filesystem(device)
        
        if not filesystem:
            return {"success": False, "error": "No filesystem found. Please format the device first."}
        
        # Create mount point if it doesn't exist
        subprocess.run(f"sudo mkdir -p {mount_point}", shell=True, check=True)
        
        # Mount the device
        result = subprocess.run(f"sudo mount {device} {mount_point}", shell=True, capture_output=True, text=True)
        
        if result.returncode == 0:
            return {"success": True, "message": f"Successfully mounted {device} to {mount_point}"}
        else:
            return {"success": False, "error": result.stderr}
    except Exception as e:
        return {"success": False, "error": str(e)}

def unmount_device(device):
    """Unmount a device"""
    try:
        result = subprocess.run(f"sudo umount {device}", shell=True, capture_output=True, text=True)
        
        if result.returncode == 0:
            return {"success": True, "message": f"Successfully unmounted {device}"}
        else:
            return {"success": False, "error": result.stderr}
    except Exception as e:
        return {"success": False, "error": str(e)}

def format_device(device, filesystem="ext4"):
    """Format a device with specified filesystem"""
    try:
        # Unmount if mounted
        subprocess.run(f"sudo umount {device}", shell=True, capture_output=True)
        
        # Format the device
        if filesystem == "ext4":
            cmd = f"sudo mkfs.ext4 -F {device}"
        elif filesystem == "xfs":
            cmd = f"sudo mkfs.xfs -f {device}"
        else:
            return {"success": False, "error": f"Unsupported filesystem: {filesystem}"}
        
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
        
        if result.returncode == 0:
            return {"success": True, "message": f"Successfully formatted {device} as {filesystem}"}
        else:
            return {"success": False, "error": result.stderr}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.route('/api/raid_types')
def get_raid_types():
    """Get supported RAID types"""
    return jsonify([
        {"value": "raid0", "name": "RAID 0 (Stripe - No Redundancy)", "min_drives": 2},
        {"value": "raid1", "name": "RAID 1 (Mirror - 50% Capacity)", "min_drives": 2},
        {"value": "raid5", "name": "RAID 5 (Stripe + Parity)", "min_drives": 3},
        {"value": "raid6", "name": "RAID 6 (Double Parity)", "min_drives": 4},
        {"value": "raid10", "name": "RAID 10 (Stripe + Mirror)", "min_drives": 4}
    ])

@app.route('/api/health')
def get_health():
    """API endpoint for drive health status"""
    health_data = get_drive_health()
    return jsonify(health_data)

@app.route('/api/speed_test', methods=['POST'])
def speed_test():
    """API endpoint for speed testing"""
    data = request.json
    device = data.get('device', '/dev/sda')
    test_type = data.get('type', 'quick')
    
    results = run_speed_test(device, test_type)
    return jsonify(results)

@app.route('/api/mount', methods=['POST'])
def api_mount():
    """API endpoint to mount a device"""
    data = request.json
    device = data.get('device')
    mount_point = data.get('mount_point')
    
    if not device or not mount_point:
        return jsonify({"success": False, "error": "Device and mount point are required"})
    
    result = mount_device(device, mount_point)
    return jsonify(result)

@app.route('/api/unmount', methods=['POST'])
def api_unmount():
    """API endpoint to unmount a device"""
    data = request.json
    device = data.get('device')
    
    if not device:
        return jsonify({"success": False, "error": "Device is required"})
    
    result = unmount_device(device)
    return jsonify(result)

@app.route('/api/format', methods=['POST'])
def api_format():
    """API endpoint to format a device"""
    data = request.json
    device = data.get('device')
    filesystem = data.get('filesystem', 'ext4')
    
    if not device:
        return jsonify({"success": False, "error": "Device is required"})
    
    result = format_device(device, filesystem)
    return jsonify(result)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
