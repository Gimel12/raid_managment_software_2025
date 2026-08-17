# RAID Studio

RAID Studio is a modern local-network web interface for Linux software RAID. It uses `mdadm` directly, so the drives do not need to be attached to a hardware RAID controller.

## Features

- Create RAID 0, 1, 5, 6, and 10 arrays
- Select only safe, unused physical drives
- Protect mounted and operating-system drives from destructive actions
- Create ext4, XFS, or Btrfs filesystems when the matching tools are installed
- Mount and unmount arrays, with optional automatic mounting after reboot
- Delete arrays with typed destructive confirmation
- View SMART/NVMe health, temperature, lifetime, and media-error information
- Persist mdadm assembly configuration across reboots
- Run continuously through systemd and Gunicorn

## Install on Ubuntu or Debian

Clone the repository, enter it, and run:

```bash
sudo bash install.sh
```

The installer adds the required system packages, creates a Python virtual environment, installs the application dependencies, and enables the `raid-webui.service` systemd service.

Open Cockpit and choose **RAID Studio** in the sidebar:

```text
https://SERVER_IP:9090/raid_studio
```

The RAID API is not exposed on a network port. It listens on a root-only Unix
socket and is reached through Cockpit's authenticated administrative channel.
RAID Studio creates native systemd mount units for managed filesystems, so mounts
are applied in Ubuntu's host namespace and enabled again automatically at boot.

## Service management

```bash
sudo systemctl status raid-webui.service
sudo systemctl restart raid-webui.service
sudo journalctl -u raid-webui.service -f
```

The service starts automatically after networking is ready and restarts after an unexpected failure.

## Safety

RAID creation and deletion permanently erase drive data. RAID Studio requires a
short-lived, one-time creation plan and rechecks every selected device's identity
immediately before running `mdadm`. It refuses mounted, partitioned, signed,
read-only, removable, system, or existing RAID-member drives. Arrays that host
the running operating system are visible but cannot be mounted, unmounted,
modified, or deleted. Always keep independent backups of important data.
