"""The small-host launcher must keep one target and avoid destructive actions."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.cloud_compose import compose_command


class CloudComposeTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        private_dir = patch("scripts.cloud_compose.PRIVATE_ENV_DIR", Path(self.directory.name))
        private_dir.start()
        self.addCleanup(private_dir.stop)
        self.env_file = Path(self.directory.name) / "cloud.env"
        self.env_file.touch()

    def command(self, action, account_args=None):
        return compose_command(self.env_file, "travel-cloud", action, account_args)

    def test_all_lifecycle_actions_use_the_same_resource_limits(self):
        for action in ("services", "build", "db-up", "empty-backup",
                       "init", "app-up", "status", "stop"):
            with self.subTest(action=action):
                command = self.command(action)
                self.assertEqual(command[:6], [
                    "docker", "compose", "-f", "compose.cloud.yaml",
                    "-f", "compose.cloud.2g.yaml"])
                self.assertEqual(command[8:10], ["-p", "travel-cloud"])
        self.assertEqual(self.command("stop")[-4:], ["stop", "proxy", "app", "db"])

    def test_admin_keeps_arguments_after_the_account_service(self):
        args = ["create", "reviewer", "Review", "--output",
                "/run/travel-credentials/reviewer.json"]
        command = self.command("admin", args)
        self.assertEqual(command[-(len(args) + 5):], [
            "--profile", "admin", "run", "--rm", "account-admin", *args])

    def test_refuses_destructive_actions_or_wrong_project(self):
        for action in ("down", "rm", "volume"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.command(action)
        with self.assertRaises(ValueError):
            compose_command(self.env_file, "bad project", "status")

    def test_requires_a_private_environment_file(self):
        with self.assertRaises(ValueError):
            compose_command(self.env_file.with_name("missing.env"), "travel-cloud", "status")
        with self.assertRaises(ValueError):
            compose_command(self.env_file.with_suffix(".txt"), "travel-cloud", "status")
        with TemporaryDirectory() as outside:
            other = Path(outside) / "cloud.env"
            other.touch()
            with self.assertRaises(ValueError):
                compose_command(other, "travel-cloud", "status")


if __name__ == "__main__":
    unittest.main()
