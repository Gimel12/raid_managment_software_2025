#!/usr/bin/env python3
"""RAID Studio — a focused web UI for Linux software RAID powered by mdadm."""

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

TASKS = {}
TASK_LOCK = threading.Lock()
SMART_CACHE = {}
SMART_CACHE_LOCK = threading.Lock()
SMART_CACHE_SECONDS = 30

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
        "NAME,KNAME,PATH,SIZE,TYPE,MODEL,SERIAL,MOUNTPOINT,FSTYPE,ROTA,TRAN,VENDOR",
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
            item.get("mountpoint") for item in descendants if item.get("mountpoint")
        })
        in_raid = any(
            item.get("type") in raid_types or item.get("fstype") == "linux_raid_member"
            for item in descendants
        )
        has_children = bool(device.get("children"))
        is_system = any(mp in {"/", "/boot", "/boot/efi"} for mp in mountpoints)
        reasons = []
        if is_system:
            reasons.append("Protected system drive")
        if in_raid:
            reasons.append("Member of an existing array")
        if mountpoints and not is_system:
            reasons.append("Mounted at " + ", ".join(mountpoints))
        if has_children and not is_system and not in_raid and not mountpoints:
            reasons.append("Contains partitions or data")

        available = not is_system and not in_raid and not mountpoints and not has_children
        status = "available" if available else "protected" if is_system else "in-array" if in_raid else "in-use"
        disk = {
            "name": device.get("name"), "path": path, "size": size,
            "size_h": human(size),
            "model": (device.get("model") or device.get("vendor") or "Unknown drive").strip(),
            "serial": (device.get("serial") or "").strip(),
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


def get_arrays():
    arrays = []
    mdstat = parse_mdstat()
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
            lowered = line.lower()
            state = "active"
            if "faulty" in lowered or "fail" in lowered:
                state = "faulty"
            elif "spare" in lowered:
                state = "spare"
            elif "rebuild" in lowered or "sync" in lowered:
                state = "rebuilding"
            elif "removed" in lowered:
                state = "removed"
            array["members"].append({"path": member_match.group(1), "state": state})

        _, lsblk_out, _ = run(["lsblk", "-J", "-o", "MOUNTPOINT,FSTYPE", device])
        try:
            block = json.loads(lsblk_out)["blockdevices"][0]
            array["mountpoint"] = block.get("mountpoint")
            array["fstype"] = block.get("fstype")
        except (TypeError, ValueError, KeyError, IndexError):
            pass

        state = array["state"].lower()
        if array["failed"] or "fail" in state or "degrad" in state:
            array["health"] = "degraded"
        elif array["sync"] and array["sync"].get("percent") is not None:
            array["health"] = "syncing"
        elif "clean" in state or "active" in state:
            array["health"] = "healthy"
        else:
            array["health"] = "unknown"
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
    _, out, _ = run(["mdadm", "--detail", "--scan"])
    arrays = [line for line in out.splitlines() if line.strip().startswith("ARRAY")]
    conf = Path("/etc/mdadm/mdadm.conf")
    try:
        existing = conf.read_text().splitlines() if conf.exists() else []
        kept = [line for line in existing if not line.strip().startswith("ARRAY")]
        conf.write_text("\n".join(kept).rstrip() + "\n\n# Managed by RAID Studio\n" + "\n".join(arrays) + "\n")
        run(["update-initramfs", "-u"], timeout=180)
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


def remove_fstab_entry(device, mountpoint=None):
    fstab = Path("/etc/fstab")
    try:
        lines = fstab.read_text().splitlines()
        _, uuid, _ = run(["blkid", "-s", "UUID", "-o", "value", device])
        uuid = uuid.strip()
        kept = []
        for line in lines:
            fields = line.split()
            matches_uuid = bool(uuid and uuid in line)
            matches_mount = bool(mountpoint and len(fields) >= 2 and fields[1] == mountpoint)
            if not matches_uuid and not matches_mount:
                kept.append(line)
        fstab.write_text("\n".join(kept) + "\n")
    except OSError:
        pass


@app.after_request
def harden_response(response):
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


@app.route("/api/create", methods=["POST"])
def api_create():
    data = request.get_json(silent=True) or {}
    level = data.get("level")
    devices = data.get("devices") or []
    name = (data.get("name") or "").strip()
    chunk = str(data.get("chunk") or "").strip()
    do_format = bool(data.get("format"))
    filesystem = data.get("fstype", "ext4")
    do_mount = bool(data.get("mount"))
    mountpoint = (data.get("mountpoint") or "").strip()

    if data.get("confirm") != "ERASE":
        return jsonify({"ok": False, "error": "Destructive confirmation is required"}), 400
    if level not in RAID_LEVELS:
        return jsonify({"ok": False, "error": "Choose a valid RAID level"}), 400
    if not isinstance(devices, list) or len(devices) != len(set(devices)):
        return jsonify({"ok": False, "error": "Drive selection is invalid"}), 400
    minimum = RAID_LEVELS[level]["min"]
    if len(devices) < minimum:
        return jsonify({"ok": False, "error": f"{RAID_LEVELS[level]['name']} needs at least {minimum} drives"}), 400
    if level == "raid10" and len(devices) % 2:
        return jsonify({"ok": False, "error": "RAID 10 requires an even number of drives"}), 400
    if chunk and (not chunk.isdigit() or int(chunk) not in {64, 128, 256, 512, 1024}):
        return jsonify({"ok": False, "error": "Choose a valid chunk size"}), 400
    if do_format and filesystem not in available_filesystems():
        return jsonify({"ok": False, "error": f"{filesystem} tools are not installed"}), 400
    if do_mount and not do_format:
        return jsonify({"ok": False, "error": "Create a filesystem before mounting the new array"}), 400
    if do_mount and mountpoint and not valid_mountpoint(mountpoint):
        return jsonify({"ok": False, "error": "Mount points must be a clean path inside /mnt or /srv"}), 400

    live_disks = {disk["path"]: disk for disk in get_disks(include_smart=False)}
    for device in devices:
        disk = live_disks.get(device)
        if not disk:
            return jsonify({"ok": False, "error": f"Unknown drive {device}"}), 400
        if not disk["available"]:
            reason = "; ".join(disk["reasons"]) or "drive is in use"
            return jsonify({"ok": False, "error": f"Refusing to use {device}: {reason}"}), 400

    md_device = next_md_device()
    command = [
        "mdadm", "--create", md_device, "--run",
        f"--level={level.removeprefix('raid')}", f"--raid-devices={len(devices)}",
    ]
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "", name)[:32]
    if safe_name:
        command.append(f"--name={safe_name}")
    if chunk and level in {"raid0", "raid5", "raid6", "raid10"}:
        command.append(f"--chunk={chunk}")
    command.extend(devices)

    for device in devices:
        rc, out, err = run(["wipefs", "-a", device])
        if rc != 0:
            return jsonify({"ok": False, "error": (err or out).strip() or f"Could not prepare {device}"}), 500

    rc, out, err = run(command, timeout=120)
    if rc != 0:
        return jsonify({"ok": False, "error": (err or out).strip() or "mdadm could not create the array"}), 500
    persist_config()

    task_id = None
    if do_format or do_mount:
        task_id = f"job-{int(time.time() * 1000)}"
        set_task(task_id, status="running", md=md_device, done=False)
        threading.Thread(
            target=_format_mount_job,
            args=(task_id, md_device, do_format, filesystem, do_mount, mountpoint),
            daemon=True,
        ).start()
    return jsonify({"ok": True, "md": md_device, "task": task_id, "output": out.strip()})


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
            mountpoint = mountpoint or f"/mnt/{os.path.basename(device)}"
            if not valid_mountpoint(mountpoint):
                set_task(task_id, status="error", done=True, error="Invalid mount point")
                return
            os.makedirs(mountpoint, exist_ok=True)
            task_log(task_id, f"Mounting at {mountpoint}…")
            rc, out, err = run(["mount", device, mountpoint])
            if rc != 0:
                set_task(task_id, status="error", done=True, error=(err or out).strip())
                return
            _, uuid, _ = run(["blkid", "-s", "UUID", "-o", "value", device])
            uuid = uuid.strip()
            if uuid:
                fstab = Path("/etc/fstab")
                current = fstab.read_text() if fstab.exists() else ""
                if uuid not in current:
                    with fstab.open("a") as handle:
                        handle.write(f"\n# RAID Studio: {device}\nUUID={uuid} {mountpoint} {filesystem} defaults,nofail 0 2\n")
                    task_log(task_id, "Automatic mounting enabled")
        set_task(task_id, status="done", done=True)
    except Exception as exc:
        set_task(task_id, status="error", done=True, error=str(exc))


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
    if not valid_mountpoint(mountpoint):
        return jsonify({"ok": False, "error": "Mount points must be inside /mnt or /srv"}), 400
    array = next((item for item in get_arrays() if item["name"] == name), None)
    if not array:
        return jsonify({"ok": False, "error": "Array not found"}), 404
    if array.get("mountpoint"):
        return jsonify({"ok": False, "error": f"Already mounted at {array['mountpoint']}"}), 409
    if not array.get("fstype"):
        return jsonify({"ok": False, "error": "This array does not have a filesystem"}), 400
    os.makedirs(mountpoint, exist_ok=True)
    rc, out, err = run(["mount", array["dev"], mountpoint])
    if rc != 0:
        return jsonify({"ok": False, "error": (err or out).strip()}), 500
    _, uuid, _ = run(["blkid", "-s", "UUID", "-o", "value", array["dev"]])
    uuid = uuid.strip()
    if uuid:
        fstab = Path("/etc/fstab")
        current = fstab.read_text() if fstab.exists() else ""
        if uuid not in current:
            with fstab.open("a") as handle:
                handle.write(f"\n# RAID Studio: {array['dev']}\nUUID={uuid} {mountpoint} {array['fstype']} defaults,nofail 0 2\n")
    return jsonify({"ok": True, "mountpoint": mountpoint})


@app.route("/api/array/<name>/unmount", methods=["POST"])
def api_unmount(name):
    if not re.fullmatch(r"md\d+", name):
        return jsonify({"ok": False, "error": "Invalid array"}), 400
    array = next((item for item in get_arrays() if item["name"] == name), None)
    if not array:
        return jsonify({"ok": False, "error": "Array not found"}), 404
    if not array.get("mountpoint"):
        return jsonify({"ok": False, "error": "Array is not mounted"}), 409
    rc, out, err = run(["umount", array["dev"]])
    if rc != 0:
        return jsonify({"ok": False, "error": (err or out).strip()}), 500
    remove_fstab_entry(array["dev"], array["mountpoint"])
    return jsonify({"ok": True})


@app.route("/api/array/<name>/delete", methods=["POST"])
def api_delete(name):
    if not re.fullmatch(r"md\d+", name):
        return jsonify({"ok": False, "error": "Invalid array"}), 400
    data = request.get_json(silent=True) or {}
    if data.get("confirm") != name:
        return jsonify({"ok": False, "error": f"Type {name} to confirm deletion"}), 400
    array = next((item for item in get_arrays() if item["name"] == name), None)
    if not array:
        return jsonify({"ok": False, "error": "Array not found"}), 404
    if array.get("mountpoint"):
        rc, out, err = run(["umount", array["dev"]])
        if rc != 0:
            return jsonify({"ok": False, "error": f"Could not unmount: {(err or out).strip()}"}), 500
        remove_fstab_entry(array["dev"], array["mountpoint"])
    rc, out, err = run(["mdadm", "--stop", array["dev"]])
    if rc != 0:
        return jsonify({"ok": False, "error": (err or out).strip()}), 500
    for member in array["members"]:
        run(["mdadm", "--zero-superblock", member["path"]])
        run(["wipefs", "-a", member["path"]])
    persist_config()
    return jsonify({"ok": True})


@app.route("/api/array/<name>/action", methods=["POST"])
def api_action(name):
    if not re.fullmatch(r"md\d+", name):
        return jsonify({"ok": False, "error": "Invalid array"}), 400
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    device = data.get("device")
    array = next((item for item in get_arrays() if item["name"] == name), None)
    if not array:
        return jsonify({"ok": False, "error": "Array not found"}), 404
    known_members = {member["path"] for member in array["members"]}
    if action in {"fail", "remove"} and device not in known_members:
        return jsonify({"ok": False, "error": "Drive is not a member of this array"}), 400
    if action == "fail":
        rc, out, err = run(["mdadm", array["dev"], "--fail", device])
    elif action == "remove":
        rc, out, err = run(["mdadm", array["dev"], "--remove", device])
    elif action == "add":
        disks = {disk["path"]: disk for disk in get_disks(include_smart=False)}
        if device not in disks or not disks[device]["available"]:
            return jsonify({"ok": False, "error": f"{device} is not available"}), 400
        run(["wipefs", "-a", device])
        rc, out, err = run(["mdadm", array["dev"], "--add", device])
    else:
        return jsonify({"ok": False, "error": "Invalid action"}), 400
    if rc != 0:
        return jsonify({"ok": False, "error": (err or out).strip()}), 500
    persist_config()
    return jsonify({"ok": True, "output": out.strip()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
