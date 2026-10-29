"""Safety checks for the standalone encrypted cloud backup command."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import oss_backup


def config() -> dict:
    return {
        "bucket": "private-travel-backups", "region": "ap-southeast-1",
        "prefix": "travel-assistant/backups/", "recipient": "age1" + "q" * 58,
        "access_key_id": "private-id", "access_key_secret": "private-secret",
        "age_path": "age", "ossutil_path": "ossutil",
    }


def container(service: str, *, port_bindings=None, project="travel-cloud") -> dict:
    return {
        "Config": {"Labels": {"com.docker.compose.project": project,
                              "com.docker.compose.service": service}},
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "HostConfig": {"PortBindings": port_bindings}, "Image": "sha256:dummy",
    }


class BackupSafetyTests(unittest.TestCase):
    def test_subprocess_error_exposes_only_known_category(self):
        failed = subprocess.CompletedProcess(
            ["ossutil"], 1, "", "AccessDenied private-secret")
        with patch.object(oss_backup.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(oss_backup.BackupError,
                                        r"ossutil \(AccessDenied\)") as caught:
                oss_backup.run(["ossutil"])
        self.assertNotIn("private-secret", str(caught.exception))

    def test_rejects_wrong_region_and_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            tool = Path(folder) / "tool"
            tool.write_text("tool", encoding="utf-8")
            path = Path(folder) / "config.json"
            value = config()
            value["age_path"] = str(tool)
            value["ossutil_path"] = str(tool)
            value["region"] = "cn-hongkong"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(oss_backup.BackupError):
                oss_backup.load_config(path, require_private=False)
            value["region"] = "ap-southeast-1"
            value["prefix"] = "../other/"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(oss_backup.BackupError):
                oss_backup.load_config(path, require_private=False)

    def test_rejects_database_host_port(self):
        items = [container("db", port_bindings={"5432/tcp": [{"HostPort": "5432"}]}),
                 container("app"), container("proxy", port_bindings={
                     "80/tcp": [{}], "443/tcp": [{}]})]
        with patch.object(oss_backup, "inspect", side_effect=items):
            with self.assertRaisesRegex(oss_backup.BackupError, "exposure"):
                oss_backup.check_source()

    def test_rejects_unexpected_project(self):
        with patch.object(oss_backup, "inspect", return_value=container(
                "db", project="another-project")):
            with self.assertRaisesRegex(oss_backup.BackupError, "identity"):
                oss_backup.check_source()

    def test_upload_forbids_overwrite_and_never_puts_secret_in_args(self):
        with tempfile.TemporaryDirectory() as folder:
            ciphertext = Path(folder) / "backup.tar.age"
            ciphertext.write_bytes(b"age-encryption.org/v1" + b"encrypted")
            calls = []

            def fake_run(command, **kwargs):
                calls.append((command, kwargs))
                if "head-object" in command:
                    return json.dumps({"Header": {"Content-Length": [str(
                        ciphertext.stat().st_size)], "X-Oss-Meta-Sha256": [
                            oss_backup.sha256(ciphertext)]}}) + "\n0.12(s) elapsed"
                return ""

            with patch.object(oss_backup, "run", side_effect=fake_run):
                oss_backup.upload(ciphertext, config(), "travel-assistant/backups/a.age")
            self.assertEqual(len(calls), 2)
            self.assertIn("--forbid-overwrite=true", calls[0][0])
            self.assertEqual(calls[0][1]["env"]["OSS_ACCESS_KEY_SECRET"],
                             "private-secret")
            self.assertNotIn("private-secret", " ".join(calls[0][0]))
            self.assertNotIn("private-id", " ".join(calls[0][0]))

    def test_object_length_mismatch_rejects_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            ciphertext = Path(folder) / "backup.tar.age"
            ciphertext.write_bytes(b"encrypted")
            with patch.object(oss_backup, "run", side_effect=(
                    "", '{"ContentLength": 1, "Metadata": {"sha256": "' +
                    "a" * 64 + '"}}')):
                with self.assertRaisesRegex(oss_backup.BackupError, "metadata"):
                    oss_backup.upload(ciphertext, config(), "travel-assistant/backups/a.age")

    def test_oversized_object_is_rejected_before_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            ciphertext = Path(folder) / "backup.tar.age"
            ciphertext.write_bytes(b"encrypted")
            with patch.object(Path, "stat", return_value=SimpleNamespace(
                    st_size=5 * 1024**3 + 1)), patch.object(
                    oss_backup, "run") as command:
                with self.assertRaisesRegex(oss_backup.BackupError, "limit"):
                    oss_backup.upload(ciphertext, config(), "travel-assistant/backups/a.age")
                command.assert_not_called()

    def test_stale_or_missing_remote_backup_fails_monitor(self):
        with tempfile.TemporaryDirectory() as folder:
            state_dir = Path(folder)
            state = {"completed_utc": (datetime.now(timezone.utc) -
                       timedelta(hours=27)).isoformat(),
                     "bucket": config()["bucket"],
                     "object_key": config()["prefix"] + "one.age", "bytes": 100,
                     "sha256": "a" * 64}
            (state_dir / "last-success.json").write_text(json.dumps(state),
                                                        encoding="utf-8")
            with patch.object(oss_backup, "STATE_DIR", state_dir), patch.object(
                    oss_backup, "head_object", return_value=(100, "a" * 64)):
                with self.assertRaisesRegex(oss_backup.BackupError, "stale"):
                    oss_backup.check_fresh(config(), 26)
            state["completed_utc"] = datetime.now(timezone.utc).isoformat()
            (state_dir / "last-success.json").write_text(json.dumps(state),
                                                        encoding="utf-8")
            with patch.object(oss_backup, "STATE_DIR", state_dir), patch.object(
                    oss_backup, "head_object", return_value=(99, "a" * 64)):
                with self.assertRaisesRegex(oss_backup.BackupError, "changed"):
                    oss_backup.check_fresh(config(), 26)


if __name__ == "__main__":
    unittest.main()
