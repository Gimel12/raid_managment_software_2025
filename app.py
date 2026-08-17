#!/usr/bin/env python3
"""RAID Studio — a focused web UI for Linux software RAID powered by mdadm."""

import json
import os
import re
import shlex
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

TASKS = {}
TASK_LOCK = threading.Lock()
SMART_CACHE = {}
SMART_CACHE_LOCK = threading.Lock()
SMART_CACHE_SECONDS = 30
PLAN_LOCK = threading.Lock()
PLANS = {}
PLAN_TTL_SECONDS = 300
OPERATION_LOCK = threading.Lock()
OPERATION_STATE = {"name": None}

SYSTEM_MOUNTPOINTS = {"/", "/boot", "/boot/efi", "/usr", "/var"}
BACKUP_ROOT = Path("/var/backups/raid-studio")
MOUNT_UNIT_DIR = Path("/etc/systemd/system")
MANAGED_MOUNT_MARKER = "# Managed by RAID Studio"

RAID_LEVELS = {
    "raid0": {
        "name": "RAID 0", "label": "Performance", "min": 2,
        "redundancy": 0, "efficiency": "100%",
        "desc": "Fast striped storage with full capacity and no fault tolerance.",
    },
    "raid1": {
        "name": "RAID 1", "label": "Mirror", "min": 2,
        "redundancy": 1, "efficiency": "50% with 2 drives",
        "desc": "Simple, dependable mirroring. Your data remains available after one drive fails.",
    },
    "raid5": {
        "name": "RAID 5", "label": "Balanced", "min": 3,
        "redundancy": 1, "efficiency": "One drive for parity",
        "desc": "A practical balance of capacity and protection for three or more drives.",
    },
    "raid6": {
        "name": "RAID 6", "label": "Resilient", "min": 4,
        "redundancy": 2, "efficiency": "Two drives for parity",
        "desc": "Extra protection for larger arrays, surviving two simultaneous drive failures.",
    },
    "raid10": {
        "name": "RAID 10", "label": "Fast + protected", "min": 4,
        "redundancy": 1, "efficiency": "50%",
        "desc": "Striped mirrors for strong performance, quick rebuilds, and redundancy.",
    },
}

ALLOWED_FILESYSTEMS = {
    "ext4": {"label": "ext4", "description": "Recommended for most Linux storage"},
    "xfs": {"label": "XFS", "description": "Great for large files and parallel workloads"},
    "btrfs": {"label": "Btrfs", "description": "Checksums, snapshots, and advanced features"},
}


def run(cmd, timeout=60):
    """Run a command without invoking a shell."""
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "Command timed out"
    except Exception as exc:
        return 1, "", str(exc)


def backup_file(path):
    path = Path(path)
    if not path.exists():
        return None
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(path).lstrip("/"))
    backup = BACKUP_ROOT / f"{safe_name}.{int(time.time())}.{secrets.token_hex(4)}.bak"
    shutil.copy2(path, backup)
    return backup


def atomic_write(path, content, mode=0o644):
    """Replace a root-owned text file atomically, with a recoverable backup."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_file(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def begin_operation(name):
    with OPERATION_LOCK:
        if OPERATION_STATE["name"]:
            return False, OPERATION_STATE["name"]
        OPERATION_STATE["name"] = name
        return True, None


def end_operation():
    with OPERATION_LOCK:
        OPERATION_STATE["name"] = None


@contextmanager
def storage_operation(name):
    acquired, active = begin_operation(name)
    if not acquired:
        raise RuntimeError(f"Another storage operation is already running: {active}")
    try:
        yield
    finally:
        end_operation()


def human(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if number < 1024:
            return f"{number:.1f} {unit}" if unit != "B" else f"{int(number)} {unit}"
        number /= 1024
    return f"{number:.1f} EB"


def lsblk_tree():
    rc, out, _ = run([
        "lsblk", "-J", "-b", "-o",
        "NAME,KNAME,PATH,MAJ:MIN,SIZE,TYPE,MODEL,SERIAL,WWN,MOUNTPOINTS,FSTYPE,"
        "ROTA,RO,RM,TRAN,VENDOR",
    ])
    if rc != 0:
        return []
    try:
        return json.loads(out).get("blockdevices", [])
    except (TypeError, ValueError):
        return []


def _walk_devices(device):
    yield device
    for child in device.get("children", []) or []:
        yield from _walk_devices(child)


def _device_mountpoints(device):
    value = device.get("mountpoints")
    if isinstance(value, list):
        return [item for item in value if item]
    value = value or device.get("mountpoint")
    return [value] if value else []


def device_signatures(path):
    """Return every on-disk signature reported by wipefs without changing it."""
    rc, out, _ = run(["wipefs", "--json", "--no-act", path], timeout=15)
    if rc != 0:
        return None
    try:
        return json.loads(out or "{}").get("signatures", []) or []
    except (TypeError, ValueError):
        return None


def device_has_holders(name):
    holders = Path("/sys/class/block") / name / "holders"
    try:
        return any(holders.iterdir())
    except OSError:
        return True


def _smart_attribute(table, attribute_id):
    for row in table or []:
        if row.get("id") == attribute_id:
            raw = row.get("raw", {})
            return raw.get("value")
    return None


def smart_health(path):
    """Return normalized SMART/NVMe health information, cached briefly."""
    now = time.time()
    with SMART_CACHE_LOCK:
        cached = SMART_CACHE.get(path)
        if cached and now - cached[0] < SMART_CACHE_SECONDS:
            return cached[1]

    result = {
        "status": "unknown", "label": "Unavailable", "passed": None,
        "temperature": None, "power_on_hours": None, "power_cycles": None,
        "life_used": None, "media_errors": None, "unsafe_shutdowns": None,
        "reallocated": None, "pending": None, "message": "SMART data is unavailable",
    }
    if not shutil.which("smartctl"):
        result["message"] = "smartmontools is not installed"
    else:
        _, out, err = run(["smartctl", "-a", "-j", path], timeout=20)
        try:
            data = json.loads(out or "{}")
            messages = data.get("smartctl", {}).get("messages", [])
            smart = data.get("smart_status", {})
            passed = smart.get("passed")
            temp = data.get("temperature", {}).get("current")
            hours = data.get("power_on_time", {}).get("hours")
            cycles = data.get("power_cycle_count")
            nvme = data.get("nvme_smart_health_information_log", {})
            table = data.get("ata_smart_attributes", {}).get("table", [])

            result.update({
                "passed": passed,
                "temperature": temp if temp is not None else nvme.get("temperature"),
                "power_on_hours": hours if hours is not None else nvme.get("power_on_hours"),
                "power_cycles": cycles if cycles is not None else nvme.get("power_cycles"),
                "life_used": nvme.get("percentage_used"),
                "media_errors": nvme.get("media_errors"),
                "unsafe_shutdowns": nvme.get("unsafe_shutdowns"),
                "reallocated": _smart_attribute(table, 5),
                "pending": _smart_attribute(table, 197),
            })
            warning = any([
                passed is False,
                (result["media_errors"] or 0) > 0,
                (result["reallocated"] or 0) > 0,
                (result["pending"] or 0) > 0,
                (result["temperature"] or 0) >= 60,
            ])
            telemetry = [
                result["temperature"], result["power_on_hours"], result["power_cycles"],
                result["life_used"], result["media_errors"], result["reallocated"],
                result["pending"],
            ]
            if passed is True and not warning:
                result.update(status="healthy", label="Healthy", message="SMART checks passed")
            elif passed is not None or any(value is not None for value in telemetry):
                result.update(status="warning", label="Needs attention", message="Review drive health details")
            elif messages:
                result["message"] = messages[0].get("string", result["message"])
            elif err:
                result["message"] = err.strip().splitlines()[0]
        except (TypeError, ValueError):
            if err:
                result["message"] = err.strip().splitlines()[0]

    with SMART_CACHE_LOCK:
        SMART_CACHE[path] = (now, result)
    return result


def get_disks(include_smart=True):
    """Return physical disks with safety, usage, and optional health metadata."""
    disks = []
    raid_types = {"raid0", "raid1", "raid4", "raid5", "raid6", "raid10", "md"}
    for device in lsblk_tree():
        if device.get("type") != "disk":
            continue
        size = int(device.get("size") or 0)
        path = device.get("path")
        if not path or size <= 0:
            continue

        descendants = list(_walk_devices(device))
        mountpoints = sorted({
            mountpoint
            for item in descendants
            for mountpoint in _device_mountpoints(item)
        })
        in_raid = any(
            item.get("type") in raid_types or item.get("fstype") == "linux_raid_member"
            for item in descendants
        )
        has_children = bool(device.get("children"))
        is_system = any(mp in {"/", "/boot", "/boot/efi"} for mp in mountpoints)
        signatures = device_signatures(path)
        signatures_unknown = signatures is None
        has_signatures = bool(signatures)
        has_holders = device_has_holders(device.get("kname") or device.get("name"))
        read_only = device.get("ro") in (True, "1", 1)
        removable = device.get("rm") in (True, "1", 1)
        reasons = []
        if is_system:
            reasons.append("Protected system drive")
        if in_raid:
            reasons.append("Member of an existing array")
        if mountpoints and not is_system:
            reasons.append("Mounted at " + ", ".join(mountpoints))
        if has_children and not is_system and not in_raid and not mountpoints:
            reasons.append("Contains partitions or data")
        if device.get("fstype") and not in_raid:
            reasons.append(f"Contains an existing {device['fstype']} filesystem")
        if has_signatures and not has_children and not device.get("fstype"):
            types = sorted({item.get("type") or "data" for item in signatures})
            reasons.append("Contains an existing " + ", ".join(types) + " signature")
        if signatures_unknown:
            reasons.append("Could not verify on-disk signatures")
        if has_holders and not in_raid:
            reasons.append("Used by another block device")
        if read_only:
            reasons.append("Read-only device")
        if removable:
            reasons.append("Removable device")

        available = not any([
            is_system, in_raid, mountpoints, has_children, device.get("fstype"),
            has_signatures, signatures_unknown, has_holders, read_only, removable,
        ])
        status = "available" if available else "protected" if is_system else "in-array" if in_raid else "in-use"
        disk = {
            "name": device.get("name"), "path": path, "size": size,
            "size_h": human(size),
            "model": (device.get("model") or device.get("vendor") or "Unknown drive").strip(),
            "serial": (device.get("serial") or "").strip(),
            "wwn": (device.get("wwn") or "").strip(),
            "maj_min": device.get("maj:min"),
            "tran": (device.get("tran") or "").upper(),
            "ssd": device.get("rota") in (False, "0", 0),
            "fstype": device.get("fstype"), "mountpoints": mountpoints,
            "available": available, "protected": is_system, "status": status,
            "reasons": reasons,
            "partitions": sum(1 for item in descendants if item.get("type") == "part"),
        }
        disk["health"] = smart_health(path) if include_smart else {"status": "loading"}
        disks.append(disk)
    return sorted(disks, key=lambda item: item["path"])


def parse_mdstat():
    info = {}
    try:
        text = Path("/proc/mdstat").read_text()
    except OSError:
        return info
    for block in re.split(r"\n(?=\w+\s*:\s)", text):
        match = re.match(r"(md\d+)\s*:", block)
        if not match:
            continue
        entry = {"operation": None, "percent": None, "speed": None, "eta": None}
        progress = re.search(
            r"(resync|recovery|reshape|check)\s*=\s*([\d.]+)%.*?finish=([\d.]+min).*?speed=(\S+)",
            block,
        )
        if progress:
            entry.update(
                operation=progress.group(1), percent=float(progress.group(2)),
                eta=progress.group(3), speed=progress.group(4),
            )
        info[match.group(1)] = entry
    return info


def protected_array_names():
    """Find MD arrays that back the running OS or active swap."""
    sources = []
    for target in sorted(SYSTEM_MOUNTPOINTS):
        rc, out, _ = run(["findmnt", "-rn", "-o", "SOURCE", "--target", target])
        if rc == 0:
            sources.extend(line.strip() for line in out.splitlines() if line.startswith("/dev/"))
    try:
        sources.extend(
            line.split()[0] for line in Path("/proc/swaps").read_text().splitlines()[1:]
            if line.startswith("/dev/")
        )
    except OSError:
        pass

    protected = set()
    for source in set(sources):
        rc, out, _ = run(["lsblk", "-s", "-r", "-n", "-o", "NAME,TYPE", source])
        if rc != 0:
            continue
        for line in out.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] in {"raid0", "raid1", "raid4", "raid5", "raid6", "raid10", "md"}:
                protected.add(fields[0])
    return protected


def classify_member_state(line):
    lowered = line.lower()
    if "faulty" in lowered or " fail" in lowered:
        return "faulty"
    if "removed" in lowered:
        return "removed"
    if "rebuild" in lowered or "recover" in lowered:
        return "rebuilding"
    if "spare" in lowered:
        return "spare"
    return "active"


def classify_array_health(array):
    state = array["state"].lower()
    syncing = array.get("sync") and array["sync"].get("percent") is not None
    if array["failed"]:
        return "degraded"
    if syncing:
        return "syncing"
    if "fail" in state or "degrad" in state:
        return "degraded"
    if "clean" in state or "active" in state:
        return "healthy"
    return "unknown"


def get_arrays():
    arrays = []
    mdstat = parse_mdstat()
    protected_names = protected_array_names()
    try:
        names = sorted(os.listdir("/dev"))
    except OSError:
        names = []
    for name in names:
        if not re.fullmatch(r"md\d+", name):
            continue
        device = f"/dev/{name}"
        rc, out, _ = run(["mdadm", "--detail", device])
        if rc != 0:
            continue
        array = {
            "dev": device, "name": name, "level": "unknown", "size": 0,
            "size_h": "—", "state": "unknown", "raid_devices": 0, "active": 0,
            "failed": 0, "spare": 0, "uuid": None, "array_name": None,
            "members": [], "mountpoint": None, "fstype": None,
            "sync": mdstat.get(name),
            "protected": name in protected_names,
            "protection_reason": "Hosts the running Ubuntu system" if name in protected_names else None,
        }
        for line in (item.strip() for item in out.splitlines()):
            value = line.split(":", 1)[1].strip() if ":" in line else ""
            if line.startswith("Raid Level"):
                array["level"] = value
            elif line.startswith("Array Size"):
                kib_match = re.search(r"Array Size\s*:\s*(\d+)", line)
                if kib_match:
                    array["size"] = int(kib_match.group(1)) * 1024
                    array["size_h"] = human(array["size"])
            elif line.startswith("State "):
                array["state"] = value
            elif line.startswith("Raid Devices"):
                array["raid_devices"] = int(value or 0)
            elif line.startswith("Active Devices"):
                array["active"] = int(value or 0)
            elif line.startswith("Failed Devices"):
                array["failed"] = int(value or 0)
            elif line.startswith("Spare Devices"):
                array["spare"] = int(value or 0)
            elif line.startswith("UUID"):
                array["uuid"] = value
            elif line.startswith("Name"):
                array["array_name"] = value

        table = out.split("Number", 1)[-1] if "Number" in out else ""
        for line in table.splitlines():
            member_match = re.search(r"(/dev/\S+)\s*$", line)
            if not member_match:
                continue
            array["members"].append({
                "path": member_match.group(1),
                "state": classify_member_state(line),
            })

        _, lsblk_out, _ = run(["lsblk", "-J", "-o", "MOUNTPOINT,FSTYPE", device])
        try:
            block = json.loads(lsblk_out)["blockdevices"][0]
            array["mountpoint"] = block.get("mountpoint")
            array["fstype"] = block.get("fstype")
        except (TypeError, ValueError, KeyError, IndexError):
            pass

        array["health"] = classify_array_health(array)
        arrays.append(array)
    return arrays


def available_filesystems():
    return {
        name: details for name, details in ALLOWED_FILESYSTEMS.items()
        if shutil.which(f"mkfs.{name}")
    }


def next_md_device():
    used = set()
    try:
        names = os.listdir("/dev")
    except OSError:
        names = []
    for name in names:
        match = re.fullmatch(r"md(\d+)", name)
        if match:
            used.add(int(match.group(1)))
    number = 0
    while number in used:
        number += 1
    return f"/dev/md{number}"


def set_task(task_id, **values):
    with TASK_LOCK:
        task = TASKS.setdefault(task_id, {"id": task_id, "log": []})
        task.update(values)


def task_log(task_id, message):
    with TASK_LOCK:
        TASKS.setdefault(task_id, {"id": task_id, "log": []})["log"].append(message)


def persist_config():
    """Refresh the managed ARRAY entries while preserving the rest of mdadm.conf."""
    rc, out, err = run(["mdadm", "--detail", "--scan"])
    if rc != 0:
        return (err or out).strip() or "Could not read the active mdadm configuration"
    arrays = [
        re.sub(r"\s+spares=\d+\b", "", line).strip()
        for line in out.splitlines()
        if line.strip().startswith("ARRAY")
    ]
    conf = Path("/etc/mdadm/mdadm.conf")
    try:
        existing = conf.read_text().splitlines() if conf.exists() else []
        kept = [
            line for line in existing
            if not line.strip().startswith("ARRAY")
            and line.strip() != "# Managed by RAID Studio"
        ]
        content = "\n".join(kept).rstrip() + "\n\n# Managed by RAID Studio\n" + "\n".join(arrays) + "\n"
        atomic_write(conf, content)
        rc, out, err = run(["update-initramfs", "-u"], timeout=300)
        if rc != 0:
            return (err or out).strip() or "Could not update initramfs"
    except OSError as exc:
        return str(exc)
    return None


def valid_mountpoint(value):
    if not value or not value.startswith(("/mnt/", "/srv/")):
        return False
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", value):
        return False
    normalized = os.path.normpath(value)
    return normalized == value and ".." not in Path(value).parts and len(value) <= 160


def prepare_mountpoint(value):
    if not valid_mountpoint(value):
        return "Mount points must be a clean path inside /mnt or /srv"
    path = Path(value)
    existing_parent = path
    while not existing_parent.exists() and existing_parent != existing_parent.parent:
        existing_parent = existing_parent.parent
    try:
        if existing_parent.resolve() != existing_parent:
            return "Mount point parents may not be symbolic links"
        if path.exists():
            if path.is_symlink() or not path.is_dir():
                return "Mount point must be a real directory"
            if any(path.iterdir()):
                return "Mount point must be empty"
    except OSError as exc:
        return str(exc)
    return None


def mount_unit_name(mountpoint):
    rc, out, err = run(["systemd-escape", "--path", "--suffix=mount", mountpoint])
    name = out.strip()
    if rc != 0 or not name.endswith(".mount") or "/" in name or len(name) > 255:
        return None, (err or out).strip() or "Could not derive the systemd mount unit name"
    return name, None


def filesystem_uuid(device):
    rc, out, err = run(["blkid", "-s", "UUID", "-o", "value", device])
    uuid = out.strip()
    if rc != 0 or not re.fullmatch(r"[A-Fa-f0-9-]{8,64}", uuid):
        return None, (err or out).strip() or "The filesystem UUID could not be read"
    return uuid, None


def install_mount_unit(device, mountpoint, filesystem):
    """Create, start, and enable a host-visible systemd mount unit."""
    error = prepare_mountpoint(mountpoint)
    if error:
        return error
    uuid, error = filesystem_uuid(device)
    if error:
        return error
    unit_name, error = mount_unit_name(mountpoint)
    if error:
        return error
    unit_path = MOUNT_UNIT_DIR / unit_name
    existed = unit_path.exists()
    previous_content = None
    if existed:
        try:
            previous_content = unit_path.read_text()
        except OSError as exc:
            return str(exc)
        if MANAGED_MOUNT_MARKER not in previous_content:
            return f"{mountpoint} is controlled by an unmanaged systemd mount unit"

    try:
        os.makedirs(mountpoint, exist_ok=True)
        content = (
            f"{MANAGED_MOUNT_MARKER}\n"
            "[Unit]\n"
            f"Description=RAID Studio mount for {device}\n"
            "After=mdmonitor.service\n\n"
            "[Mount]\n"
            f"What=/dev/disk/by-uuid/{uuid}\n"
            f"Where={mountpoint}\n"
            f"Type={filesystem}\n"
            "Options=defaults\n"
            "TimeoutSec=180\n\n"
            "[Install]\n"
            "WantedBy=local-fs.target\n"
        )
        atomic_write(unit_path, content)
    except OSError as exc:
        return str(exc)

    rc, out, err = run(["systemctl", "daemon-reload"])
    if rc == 0:
        rc, out, err = run(["systemctl", "start", unit_name], timeout=240)
    if rc == 0:
        rc, out, err = run(["systemctl", "enable", unit_name])
    if rc != 0:
        message = (err or out).strip() or f"Could not activate {unit_name}"
        run(["systemctl", "stop", unit_name])
        try:
            if existed and previous_content is not None:
                atomic_write(unit_path, previous_content)
            else:
                unit_path.unlink()
        except OSError:
            pass
        run(["systemctl", "daemon-reload"])
        return message
    return None


def remove_mount_unit(mountpoint):
    """Unmount through PID 1, then remove only a RAID Studio-managed unit."""
    unit_name, error = mount_unit_name(mountpoint)
    if error:
        return error
    unit_path = MOUNT_UNIT_DIR / unit_name
    managed = False
    if unit_path.exists():
        try:
            managed = MANAGED_MOUNT_MARKER in unit_path.read_text()
        except OSError as exc:
            return str(exc)

    rc, out, err = run(["systemctl", "stop", unit_name], timeout=240)
    if rc != 0:
        return (err or out).strip() or f"Could not unmount {mountpoint}"
    if managed:
        rc, out, err = run(["systemctl", "disable", unit_name])
        if rc != 0:
            return (err or out).strip() or f"Could not disable {unit_name}"
        try:
            backup_file(unit_path)
            unit_path.unlink()
        except OSError as exc:
            return str(exc)
        rc, out, err = run(["systemctl", "daemon-reload"])
        if rc != 0:
            return (err or out).strip() or "Could not reload systemd"
    return None


def find_array(name):
    return next((item for item in get_arrays() if item["name"] == name), None)


def reject_protected_array(array):
    if array and array.get("protected"):
        return jsonify({
            "ok": False,
            "error": f"{array['name']} is protected because it hosts the running Ubuntu system",
        }), 403
    return None


@app.after_request
def harden_response(response):
    if (
        request.headers.get("X-RAID-Studio-Envelope") == "1"
        and response.status_code >= 400
        and response.is_json
    ):
        original_status = response.status_code
        payload = response.get_json(silent=True) or {"ok": False, "error": "Request failed"}
        payload["status"] = original_status
        response = jsonify(payload)
        response.status_code = 200
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.before_request
def protect_mutations():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("X-RAID-Studio") != "1":
            return jsonify({"ok": False, "error": "Invalid request origin"}), 403


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/overview")
def api_overview():
    disks = get_disks()
    arrays = get_arrays()
    healthy_disks = sum(1 for disk in disks if disk["health"]["status"] == "healthy")
    return jsonify({
        "hostname": os.uname().nodename,
        "disks": disks,
        "arrays": arrays,
        "levels": RAID_LEVELS,
        "filesystems": available_filesystems(),
        "available_count": sum(1 for disk in disks if disk["available"]),
        "healthy_count": healthy_disks,
        "total_raw": sum(disk["size"] for disk in disks),
        "total_raw_h": human(sum(disk["size"] for disk in disks)),
        "array_capacity": sum(array["size"] for array in arrays),
        "array_capacity_h": human(sum(array["size"] for array in arrays)),
        "mdadm_available": bool(shutil.which("mdadm")),
        "refreshed_at": int(time.time()),
    })


def normalize_creation_config(data):
    level = data.get("level")
    devices = data.get("devices") or []
    requested_name = (data.get("name") or "").strip()
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", requested_name).strip("-_")[:32]
    chunk = str(data.get("chunk") or "").strip()
    do_format = data.get("format") is True
    filesystem = data.get("fstype", "ext4")
    do_mount = data.get("mount") is True
    mountpoint = (data.get("mountpoint") or "").strip()

    if level not in RAID_LEVELS:
        return None, "Choose a valid RAID level"
    if (
        not isinstance(devices, list)
        or not all(isinstance(item, str) for item in devices)
        or len(devices) != len(set(devices))
    ):
        return None, "Drive selection is invalid"
    minimum = RAID_LEVELS[level]["min"]
    if len(devices) < minimum:
        return None, f"{RAID_LEVELS[level]['name']} needs at least {minimum} drives"
    if level == "raid10" and len(devices) % 2:
        return None, "RAID 10 requires an even number of drives"
    if requested_name and not name:
        return None, "Array label must contain at least one letter or number"
    if chunk and (not chunk.isdigit() or int(chunk) not in {64, 128, 256, 512, 1024}):
        return None, "Choose a valid chunk size"
    if do_format and filesystem not in available_filesystems():
        return None, f"{filesystem} tools are not installed"
    if do_mount and not do_format:
        return None, "Create a filesystem before mounting the new array"
    if do_mount and mountpoint and not valid_mountpoint(mountpoint):
        return None, "Mount points must be a clean path inside /mnt or /srv"
    return {
        "level": level, "devices": devices, "name": name, "chunk": chunk,
        "format": do_format, "fstype": filesystem, "mount": do_mount,
        "mountpoint": mountpoint,
    }, None


def disk_fingerprint(disk):
    return {
        "path": disk["path"], "maj_min": disk.get("maj_min"), "size": disk["size"],
        "serial": disk.get("serial"), "wwn": disk.get("wwn"),
    }


def validate_live_disks(devices, expected=None):
    live_disks = {disk["path"]: disk for disk in get_disks(include_smart=False)}
    selected = []
    for device in devices:
        disk = live_disks.get(device)
        if not disk:
            return None, f"Unknown drive {device}"
        if not disk["available"]:
            reason = "; ".join(disk["reasons"]) or "drive is in use"
            return None, f"Refusing to use {device}: {reason}"
        fingerprint = disk_fingerprint(disk)
        if expected and expected.get(device) != fingerprint:
            return None, f"Drive identity changed after review: {device}"
        selected.append(disk)
    return selected, None


@app.route("/api/plan", methods=["POST"])
def api_plan():
    data = request.get_json(silent=True) or {}
    config, error = normalize_creation_config(data)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    selected, error = validate_live_disks(config["devices"])
    if error:
        return jsonify({"ok": False, "error": error}), 400
    token = secrets.token_urlsafe(32)
    now = time.time()
    with PLAN_LOCK:
        for old_token, plan in list(PLANS.items()):
            if now - plan["created"] > PLAN_TTL_SECONDS:
                PLANS.pop(old_token, None)
        PLANS[token] = {
            "created": now,
            "config": config,
            "fingerprints": {disk["path"]: disk_fingerprint(disk) for disk in selected},
        }
    return jsonify({"ok": True, "plan": token, "expires_in": PLAN_TTL_SECONDS})


def consume_plan(token, config):
    if not isinstance(token, str):
        return None, "Create a fresh storage plan before continuing"
    with PLAN_LOCK:
        plan = PLANS.pop(token, None)
    if not plan or time.time() - plan["created"] > PLAN_TTL_SECONDS:
        return None, "The storage plan expired; review the drives again"
    if plan["config"] != config:
        return None, "The requested configuration changed after review"
    return plan, None


@app.route("/api/create", methods=["POST"])
def api_create():
    data = request.get_json(silent=True) or {}
    if data.get("confirm") != "ERASE":
        return jsonify({"ok": False, "error": "Destructive confirmation is required"}), 400
    config, error = normalize_creation_config(data)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    plan, error = consume_plan(data.get("plan"), config)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    acquired, active = begin_operation("creating an array")
    if not acquired:
        return jsonify({"ok": False, "error": f"Another storage operation is already running: {active}"}), 409

    release_operation = True
    try:
        _, error = validate_live_disks(config["devices"], plan["fingerprints"])
        if error:
            return jsonify({"ok": False, "error": error}), 400

        md_device = next_md_device()
        chosen_mountpoint = config["mountpoint"] or f"/mnt/{os.path.basename(md_device)}"
        if config["mount"]:
            error = prepare_mountpoint(chosen_mountpoint)
            if error:
                return jsonify({"ok": False, "error": error}), 400

        command = [
            "mdadm", "--create", md_device, "--run",
            f"--level={config['level'].removeprefix('raid')}",
            f"--raid-devices={len(config['devices'])}",
        ]
        if config["name"]:
            command.append(f"--name={config['name']}")
        if config["chunk"] and config["level"] in {"raid0", "raid5", "raid6", "raid10"}:
            command.append(f"--chunk={config['chunk']}")
        command.extend(config["devices"])

        rc, out, err = run(command, timeout=120)
        if rc != 0:
            return jsonify({"ok": False, "error": (err or out).strip() or "mdadm could not create the array"}), 500
        warning = persist_config()

        task_id = None
        if config["format"] or config["mount"]:
            task_id = f"job-{secrets.token_urlsafe(18)}"
            set_task(task_id, status="running", md=md_device, done=False)
            threading.Thread(
                target=_format_mount_job,
                args=(
                    task_id, md_device, config["format"], config["fstype"],
                    config["mount"], chosen_mountpoint,
                ),
                daemon=True,
            ).start()
            release_operation = False
        return jsonify({
            "ok": True, "md": md_device, "task": task_id, "output": out.strip(),
            "warning": warning,
        })
    finally:
        if release_operation:
            end_operation()


def _format_mount_job(task_id, device, do_format, filesystem, do_mount, mountpoint):
    try:
        if do_format:
            task_log(task_id, f"Creating the {filesystem} filesystem…")
            command = [f"mkfs.{filesystem}"]
            command.extend(["-f"] if filesystem in {"xfs", "btrfs"} else ["-F"])
            command.append(device)
            rc, out, err = run(command, timeout=1800)
            if rc != 0:
                set_task(task_id, status="error", done=True, error=(err or out).strip())
                return
            task_log(task_id, "Filesystem created")
        if do_mount:
            task_log(task_id, f"Mounting at {mountpoint}…")
            error = install_mount_unit(device, mountpoint, filesystem)
            if error:
                set_task(task_id, status="error", done=True, error=error)
                return
            task_log(task_id, "Mounted on the host and enabled for automatic mounting")
        set_task(task_id, status="done", done=True)
    except Exception as exc:
        set_task(task_id, status="error", done=True, error=str(exc))
    finally:
        end_operation()


@app.route("/api/task/<task_id>")
def api_task(task_id):
    with TASK_LOCK:
        return jsonify(TASKS.get(task_id, {"status": "unknown", "done": True, "log": []}))


@app.route("/api/array/<name>/mount", methods=["POST"])
def api_mount(name):
    if not re.fullmatch(r"md\d+", name):
        return jsonify({"ok": False, "error": "Invalid array"}), 400
    data = request.get_json(silent=True) or {}
    mountpoint = (data.get("mountpoint") or f"/mnt/{name}").strip()
    error = prepare_mountpoint(mountpoint)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    array = find_array(name)
    if not array:
        return jsonify({"ok": False, "error": "Array not found"}), 404
    protected = reject_protected_array(array)
    if protected:
        return protected
    if array.get("mountpoint"):
        return jsonify({"ok": False, "error": f"Already mounted at {array['mountpoint']}"}), 409
    if not array.get("fstype"):
        return jsonify({"ok": False, "error": "This array does not have a filesystem"}), 400
    acquired, active = begin_operation(f"mounting {name}")
    if not acquired:
        return jsonify({"ok": False, "error": f"Another storage operation is already running: {active}"}), 409
    try:
        array = find_array(name)
        if not array:
            return jsonify({"ok": False, "error": "Array disappeared; refresh and try again"}), 409
        protected = reject_protected_array(array)
        if protected:
            return protected
        error = install_mount_unit(array["dev"], mountpoint, array["fstype"])
        if error:
            return jsonify({"ok": False, "error": error}), 500
        return jsonify({"ok": True, "mountpoint": mountpoint})
    finally:
        end_operation()


@app.route("/api/array/<name>/unmount", methods=["POST"])
def api_unmount(name):
    if not re.fullmatch(r"md\d+", name):
        return jsonify({"ok": False, "error": "Invalid array"}), 400
    array = find_array(name)
    if not array:
        return jsonify({"ok": False, "error": "Array not found"}), 404
    protected = reject_protected_array(array)
    if protected:
        return protected
    if not array.get("mountpoint"):
        return jsonify({"ok": False, "error": "Array is not mounted"}), 409
    acquired, active = begin_operation(f"unmounting {name}")
    if not acquired:
        return jsonify({"ok": False, "error": f"Another storage operation is already running: {active}"}), 409
    try:
        array = find_array(name)
        if not array:
            return jsonify({"ok": False, "error": "Array disappeared; refresh and try again"}), 409
        protected = reject_protected_array(array)
        if protected:
            return protected
        error = remove_mount_unit(array["mountpoint"])
        if error:
            return jsonify({"ok": False, "error": error}), 500
        return jsonify({"ok": True})
    finally:
        end_operation()


@app.route("/api/array/<name>/delete", methods=["POST"])
def api_delete(name):
    if not re.fullmatch(r"md\d+", name):
        return jsonify({"ok": False, "error": "Invalid array"}), 400
    data = request.get_json(silent=True) or {}
    if data.get("confirm") != name:
        return jsonify({"ok": False, "error": f"Type {name} to confirm deletion"}), 400
    array = find_array(name)
    if not array:
        return jsonify({"ok": False, "error": "Array not found"}), 404
    protected = reject_protected_array(array)
    if protected:
        return protected
    acquired, active = begin_operation(f"deleting {name}")
    if not acquired:
        return jsonify({"ok": False, "error": f"Another storage operation is already running: {active}"}), 409
    try:
        array = find_array(name)
        if not array:
            return jsonify({"ok": False, "error": "Array disappeared; refresh and try again"}), 409
        protected = reject_protected_array(array)
        if protected:
            return protected
        if array.get("mountpoint"):
            error = remove_mount_unit(array["mountpoint"])
            if error:
                return jsonify({"ok": False, "error": f"Could not unmount: {error}"}), 500
        rc, out, err = run(["mdadm", "--stop", array["dev"]])
        if rc != 0:
            return jsonify({"ok": False, "error": (err or out).strip()}), 500
        failures = []
        for member in array["members"]:
            rc, out, err = run(["mdadm", "--zero-superblock", member["path"]])
            if rc != 0:
                failures.append(f"{member['path']}: {(err or out).strip()}")
                continue
            rc, out, err = run(["wipefs", "-a", member["path"]])
            if rc != 0:
                failures.append(f"{member['path']}: {(err or out).strip()}")
        warning = persist_config()
        if failures:
            return jsonify({
                "ok": False,
                "error": "The array stopped, but some member metadata could not be cleared: " + "; ".join(failures),
            }), 500
        return jsonify({"ok": True, "warning": warning})
    finally:
        end_operation()


@app.route("/api/array/<name>/action", methods=["POST"])
def api_action(name):
    if not re.fullmatch(r"md\d+", name):
        return jsonify({"ok": False, "error": "Invalid array"}), 400
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    device = data.get("device")
    array = find_array(name)
    if not array:
        return jsonify({"ok": False, "error": "Array not found"}), 404
    protected = reject_protected_array(array)
    if protected:
        return protected
    if data.get("confirm") != f"{str(action).upper()} {device}":
        return jsonify({"ok": False, "error": "Typed confirmation does not match the requested action"}), 400
    known_members = {member["path"] for member in array["members"]}
    if action in {"fail", "remove"} and device not in known_members:
        return jsonify({"ok": False, "error": "Drive is not a member of this array"}), 400
    if action not in {"fail", "remove", "add"}:
        return jsonify({"ok": False, "error": "Invalid action"}), 400
    acquired, active = begin_operation(f"{action} drive on {name}")
    if not acquired:
        return jsonify({"ok": False, "error": f"Another storage operation is already running: {active}"}), 409
    try:
        array = find_array(name)
        if not array:
            return jsonify({"ok": False, "error": "Array disappeared; refresh and try again"}), 409
        protected = reject_protected_array(array)
        if protected:
            return protected
        if action == "fail":
            rc, out, err = run(["mdadm", array["dev"], "--fail", device])
        elif action == "remove":
            rc, out, err = run(["mdadm", array["dev"], "--remove", device])
        else:
            disks = {disk["path"]: disk for disk in get_disks(include_smart=False)}
            if device not in disks or not disks[device]["available"]:
                return jsonify({"ok": False, "error": f"{device} is not available"}), 400
            rc, out, err = run(["mdadm", array["dev"], "--add", device])
        if rc != 0:
            return jsonify({"ok": False, "error": (err or out).strip()}), 500
        warning = persist_config()
        return jsonify({"ok": True, "output": out.strip(), "warning": warning})
    finally:
        end_operation()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
