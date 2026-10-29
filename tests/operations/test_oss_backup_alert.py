"""Reminder deduplication and recovery must follow real backup state."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import oss_backup_alert


class BackupAlertTests(unittest.TestCase):
    def test_repeated_failure_is_suppressed(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "last-alert.json"
            with patch.object(oss_backup_alert, "ALERT_STATE", state), patch.object(
                    oss_backup_alert, "send") as send:
                self.assertEqual(oss_backup_alert.notify({}, "backup-failed"), "sent")
                self.assertEqual(oss_backup_alert.notify({}, "backup-failed"),
                                 "suppressed")
                send.assert_called_once()

    def test_old_success_does_not_send_false_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "last-alert.json"
            now = datetime.now(timezone.utc)
            state.write_text(json.dumps({"kind": "backup-failed",
                                         "sent_utc": now.isoformat()}), encoding="utf-8")
            (root / "last-success.json").write_text(json.dumps({
                "completed_utc": (now - timedelta(hours=1)).isoformat()}),
                encoding="utf-8")
            with patch.object(oss_backup_alert, "ALERT_STATE", state), patch.object(
                    oss_backup_alert.oss_backup, "STATE_DIR", root), patch.object(
                    oss_backup_alert.oss_backup, "load_config", return_value={}), patch.object(
                    oss_backup_alert.oss_backup, "check_fresh", return_value={}), patch.object(
                    oss_backup_alert, "send") as send:
                self.assertEqual(oss_backup_alert.check_recovery({}), "healthy")
                send.assert_not_called()

    def test_new_success_sends_one_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "last-alert.json"
            now = datetime.now(timezone.utc)
            state.write_text(json.dumps({"kind": "backup-stale",
                                         "sent_utc": (now - timedelta(hours=1)).isoformat()}),
                             encoding="utf-8")
            (root / "last-success.json").write_text(json.dumps({
                "completed_utc": now.isoformat()}), encoding="utf-8")
            with patch.object(oss_backup_alert, "ALERT_STATE", state), patch.object(
                    oss_backup_alert.oss_backup, "STATE_DIR", root), patch.object(
                    oss_backup_alert.oss_backup, "load_config", return_value={}), patch.object(
                    oss_backup_alert.oss_backup, "check_fresh", return_value={}), patch.object(
                    oss_backup_alert, "send") as send:
                self.assertEqual(oss_backup_alert.check_recovery({}), "sent")
                send.assert_called_once_with({}, "backup-recovered")


if __name__ == "__main__":
    unittest.main()
