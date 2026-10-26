"""Keep the public redirect and local-only TLS fixture compatible."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import create_cloud_2g_env


class CloudCaddyTests(unittest.TestCase):
    def test_public_hosts_and_local_certificate_fixture(self):
        source = Path("deploy/Caddyfile").read_text(encoding="utf-8")
        self.assertIn("www.{$PUBLIC_HOST} {", source)
        self.assertIn("redir https://{$PUBLIC_HOST}{uri} permanent", source)
        self.assertNotIn("tls internal", source)

        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "deploy").mkdir()
            (root / "deploy/Caddyfile").write_text(source, encoding="utf-8")
            generated = root / "data/generated"
            with patch.object(create_cloud_2g_env, "ROOT", root), \
                    patch.object(create_cloud_2g_env, "GENERATED", generated), \
                    patch.object(create_cloud_2g_env, "ENV", generated / "test.env"), \
                    patch.object(create_cloud_2g_env, "OVERRIDE", generated / "test.yaml"), \
                    patch.object(create_cloud_2g_env, "CADDY", generated / "test.Caddyfile"):
                create_cloud_2g_env.main()
            local = (generated / "test.Caddyfile").read_text(encoding="utf-8")
            self.assertEqual(local.count("tls internal"), 2)
            self.assertIn("redir https://{$PUBLIC_HOST}{uri} permanent", local)


if __name__ == "__main__":
    unittest.main()
