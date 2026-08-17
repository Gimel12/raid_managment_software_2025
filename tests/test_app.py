import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as raid_app


def disk(path="/dev/nvme9n1", *, available=True, maj_min="259:9", serial="SERIAL-9"):
    return {
        "path": path,
        "available": available,
        "reasons": [] if available else ["in use"],
        "maj_min": maj_min,
        "size": 1_000_000,
        "serial": serial,
        "wwn": "WWN-9",
    }


class DiskSafetyTests(unittest.TestCase):
    @patch.object(raid_app, "run")
    def test_partitioned_boot_array_is_protected(self, mock_run):
        def command_result(command, timeout=60):
            if command[:2] == ["findmnt", "-rn"]:
                return 0, "/dev/md0p1\n", ""
            if command[:4] == ["lsblk", "-s", "-r", "-n"]:
                return 0, "md0p1 part\nmd0 raid1\nnvme2n1p2 part\nnvme2n1 disk\n", ""
            return 1, "", "not found"

        mock_run.side_effect = command_result

        self.assertIn("md0", raid_app.protected_array_names())

    @patch.object(raid_app, "smart_health", return_value={"status": "healthy"})
    @patch.object(raid_app, "device_has_holders", return_value=False)
    @patch.object(raid_app, "device_signatures", return_value=[])
    @patch.object(raid_app, "lsblk_tree")
    def test_unmounted_whole_disk_filesystem_is_not_available(
        self, mock_lsblk, _signatures, _holders, _health
    ):
        mock_lsblk.return_value = [{
            "name": "nvme9n1", "kname": "nvme9n1", "path": "/dev/nvme9n1",
            "maj:min": "259:9", "size": 1_000_000, "type": "disk",
            "fstype": "ext4", "mountpoints": [None], "children": [],
            "model": "Test", "serial": "SERIAL-9", "wwn": "WWN-9",
            "rota": False, "ro": False, "rm": False, "tran": "nvme",
        }]

        result = raid_app.get_disks()

        self.assertFalse(result[0]["available"])
        self.assertIn("Contains an existing ext4 filesystem", result[0]["reasons"])

    @patch.object(raid_app, "smart_health", return_value={"status": "healthy"})
    @patch.object(raid_app, "device_has_holders", return_value=False)
    @patch.object(raid_app, "device_signatures", return_value=[])
    @patch.object(raid_app, "lsblk_tree")
    def test_signature_free_empty_disk_is_available(
        self, mock_lsblk, _signatures, _holders, _health
    ):
        mock_lsblk.return_value = [{
            "name": "nvme9n1", "kname": "nvme9n1", "path": "/dev/nvme9n1",
            "maj:min": "259:9", "size": 1_000_000, "type": "disk",
            "fstype": None, "mountpoints": [None], "children": [],
            "model": "Test", "serial": "SERIAL-9", "wwn": "WWN-9",
            "rota": False, "ro": False, "rm": False, "tran": "nvme",
        }]

        self.assertTrue(raid_app.get_disks()[0]["available"])


class ArrayStateTests(unittest.TestCase):
    def test_active_sync_member_is_not_mislabeled_rebuilding(self):
        line = "0 259 8 0 active sync /dev/nvme0n1"
        self.assertEqual(raid_app.classify_member_state(line), "active")

    def test_spare_rebuilding_member_is_rebuilding(self):
        line = "3 259 6 2 spare rebuilding /dev/nvme3n1"
        self.assertEqual(raid_app.classify_member_state(line), "rebuilding")

    def test_initial_raid5_recovery_is_syncing_not_failed(self):
        array = {
            "state": "active, degraded, recovering",
            "failed": 0,
            "sync": {"operation": "recovery", "percent": 0.1},
        }
        self.assertEqual(raid_app.classify_array_health(array), "syncing")

    def test_real_member_failure_is_degraded(self):
        array = {
            "state": "active, degraded",
            "failed": 1,
            "sync": {"operation": "recovery", "percent": 25.0},
        }
        self.assertEqual(raid_app.classify_array_health(array), "degraded")


class MountUnitTests(unittest.TestCase):
    @patch.object(raid_app, "prepare_mountpoint", return_value=None)
    @patch.object(raid_app, "filesystem_uuid", return_value=("fb229004-0cb6-49c7-92aa-78279e8320f7", None))
    @patch.object(raid_app, "mount_unit_name", return_value=("mnt-md1.mount", None))
    @patch.object(raid_app, "run", return_value=(0, "", ""))
    @patch.object(raid_app.os, "makedirs")
    def test_install_mount_unit_starts_then_enables_host_unit(
        self, _makedirs, mock_run, _unit_name, _uuid, _prepare
    ):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(raid_app, "MOUNT_UNIT_DIR", Path(directory)):
                error = raid_app.install_mount_unit("/dev/md1", "/mnt/md1", "ext4")
                unit = Path(directory) / "mnt-md1.mount"
                content = unit.read_text()

        self.assertIsNone(error)
        self.assertIn(raid_app.MANAGED_MOUNT_MARKER, content)
        self.assertIn("What=/dev/disk/by-uuid/fb229004-0cb6-49c7-92aa-78279e8320f7", content)
        self.assertIn("Where=/mnt/md1", content)
        commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertEqual(commands[0], ["systemctl", "daemon-reload"])
        self.assertEqual(commands[1], ["systemctl", "start", "mnt-md1.mount"])
        self.assertEqual(commands[2], ["systemctl", "enable", "mnt-md1.mount"])

    @patch.object(raid_app, "mount_unit_name", return_value=("mnt-md1.mount", None))
    @patch.object(raid_app, "run", return_value=(0, "", ""))
    @patch.object(raid_app, "backup_file")
    def test_remove_mount_unit_stops_before_removing_managed_unit(
        self, _backup, mock_run, _unit_name
    ):
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / "mnt-md1.mount"
            unit.write_text(f"{raid_app.MANAGED_MOUNT_MARKER}\n[Mount]\nWhere=/mnt/md1\n")
            with patch.object(raid_app, "MOUNT_UNIT_DIR", Path(directory)):
                error = raid_app.remove_mount_unit("/mnt/md1")
                exists_after = unit.exists()

        self.assertIsNone(error)
        self.assertFalse(exists_after)
        commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertEqual(commands[0], ["systemctl", "stop", "mnt-md1.mount"])
        self.assertEqual(commands[1], ["systemctl", "disable", "mnt-md1.mount"])
        self.assertEqual(commands[2], ["systemctl", "daemon-reload"])


class ApiSafetyTests(unittest.TestCase):
    def setUp(self):
        raid_app.app.config.update(TESTING=True)
        self.client = raid_app.app.test_client()
        raid_app.PLANS.clear()
        raid_app.OPERATION_STATE["name"] = None
        self.headers = {"X-RAID-Studio": "1"}

    def test_mutation_requires_origin_header(self):
        response = self.client.post("/api/plan", json={})
        self.assertEqual(response.status_code, 403)

    @patch.object(raid_app, "find_array")
    def test_boot_array_cannot_be_deleted(self, mock_find_array):
        mock_find_array.return_value = {
            "name": "md0", "dev": "/dev/md0", "protected": True,
            "mountpoint": None, "members": [],
        }

        response = self.client.post(
            "/api/array/md0/delete",
            json={"confirm": "md0"},
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("running Ubuntu system", response.get_json()["error"])

    @patch.object(raid_app, "available_filesystems", return_value={"ext4": {}})
    @patch.object(raid_app, "get_disks")
    def test_changed_drive_identity_invalidates_one_time_plan(self, mock_disks, _filesystems):
        config = {
            "level": "raid1", "devices": ["/dev/nvme8n1", "/dev/nvme9n1"],
            "name": "safe", "chunk": None, "format": True,
            "fstype": "ext4", "mount": False, "mountpoint": "",
        }
        mock_disks.return_value = [
            disk("/dev/nvme8n1", maj_min="259:8", serial="SERIAL-8"),
            disk("/dev/nvme9n1"),
        ]
        plan_response = self.client.post("/api/plan", json=config, headers=self.headers)
        self.assertEqual(plan_response.status_code, 200)

        mock_disks.return_value = [
            disk("/dev/nvme8n1", maj_min="259:18", serial="REPLACED"),
            disk("/dev/nvme9n1"),
        ]
        create = dict(config)
        create.update(confirm="ERASE", plan=plan_response.get_json()["plan"])
        response = self.client.post("/api/create", json=create, headers=self.headers)

        self.assertEqual(response.status_code, 400)
        self.assertIn("identity changed", response.get_json()["error"])

    @patch.object(raid_app, "available_filesystems", return_value={"ext4": {}})
    def test_array_label_safely_normalizes_command_characters(self, _filesystems):
        config, error = raid_app.normalize_creation_config({
            "level": "raid1", "devices": ["/dev/a", "/dev/b"],
            "name": "bad;touch-pwned", "format": False, "mount": False,
        })
        self.assertIsNone(error)
        self.assertEqual(config["name"], "bad-touch-pwned")

    def test_cockpit_error_envelope_preserves_validation_message(self):
        response = self.client.post(
            "/api/plan",
            json={"level": "invalid"},
            headers={**self.headers, "X-RAID-Studio-Envelope": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], 400)
        self.assertEqual(response.get_json()["error"], "Choose a valid RAID level")


if __name__ == "__main__":
    unittest.main()
