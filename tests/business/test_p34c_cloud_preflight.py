"""Cloud preflight must reject unsafe config without exposing credential values."""

from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from scripts.preflight_cloud_env import PreflightError, check_file


class CloudPreflightTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.private_dir = Path(self.directory.name)
        self.file = self.private_dir / "cloud.env"
        self.values = {
            "TRAVEL_CLOUD_ENV_FILE": "./data/generated/cloud.env",
            "PUBLIC_HOST": "tripfolio-lab.com",
            "ACME_EMAIL": "owner@example.org",
            "POSTGRES_PASSWORD": "database" + "1" * 32,
            "JWT_SECRET_KEY": "jwt" + "2" * 44,
            "DEMO_TRAVELER_1_PASSWORD": "first" + "3" * 28,
            "DEMO_TRAVELER_2_PASSWORD": "second" + "4" * 28,
            "DEMO_BATCH_DATE": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            "AI_PROVIDER": "deepseek",
            "AI_MODEL": "deepseek-flash",
            "AI_BASE_URL": "https://api.deepseek.com",
            "AI_API_KEY": "model-secret-" + "5" * 30,
            "ALIYUN_IQS_ENABLED": "true",
            "ALIYUN_IQS_API_KEY": "search-secret-" + "6" * 30,
            "PYTHON_BASE_IMAGE": "python:3.11-slim@sha256:" + "a" * 64,
            "POSTGRES_IMAGE": "postgres:17@sha256:" + "b" * 64,
            "CADDY_IMAGE": "caddy:2@sha256:" + "c" * 64,
        }

    def write_values(self, extra=""):
        self.file.write_text(
            "\n".join(f"{key}={value}" for key, value in self.values.items())
            + "\n" + extra, encoding="utf-8")
        self.file.chmod(0o600)

    def test_complete_private_config_passes(self):
        self.write_values()
        check_file(self.file, self.private_dir)

    def test_duplicate_field_fails_without_secret_in_error(self):
        self.write_values("AI_API_KEY=another-secret-value\n")
        with self.assertRaises(PreflightError) as result:
            check_file(self.file, self.private_dir)
        self.assertIn("duplicate field: AI_API_KEY", str(result.exception))
        self.assertNotIn(self.values["AI_API_KEY"], str(result.exception))

    def test_test_mode_and_placeholder_credential_are_rejected(self):
        self.write_values("CLOUD_ACCEPTANCE_MODE=true\n")
        with self.assertRaisesRegex(PreflightError, "CLOUD_ACCEPTANCE_MODE"):
            check_file(self.file, self.private_dir)
        self.values["AI_API_KEY"] = "local-test-only-" + "5" * 30
        self.write_values()
        with self.assertRaisesRegex(PreflightError, "AI_API_KEY"):
            check_file(self.file, self.private_dir)

    def test_wrong_target_and_unpinned_image_are_rejected(self):
        self.values["TRAVEL_CLOUD_ENV_FILE"] = "./data/generated/another.env"
        self.write_values()
        with self.assertRaisesRegex(PreflightError, "TRAVEL_CLOUD_ENV_FILE"):
            check_file(self.file, self.private_dir)
        self.values["TRAVEL_CLOUD_ENV_FILE"] = "./data/generated/cloud.env"
        self.values["CADDY_IMAGE"] = "caddy:latest"
        self.write_values()
        with self.assertRaisesRegex(PreflightError, "CADDY_IMAGE"):
            check_file(self.file, self.private_dir)


if __name__ == "__main__":
    unittest.main()
