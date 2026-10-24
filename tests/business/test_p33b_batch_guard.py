"""Reject misdirected or reused databases before a demo batch writes data."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import URL

from scripts.import_network_data import assert_fresh_batch_target, seed_policies_match
from scripts.batch_environment import GENERATED, checked_path, create, run
from scripts.migrate import MIGRATIONS, assert_empty_batch_database
from travel_assistant.services.policy_catalog import SEED_POLICIES


class FreshBatchGuardTests(unittest.TestCase):
    def setUp(self):
        self.connection = Mock()
        self.connection.dialect.name = "postgresql"
        self.url = URL.create("postgresql+psycopg", username="travel_app",
                              password="unused", host="127.0.0.1",
                              port=5436, database="travel_demo")

    def test_connection_string_override_blocks_migration_and_import(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://other/other"}), \
                patch("scripts.migrate.engine.url", self.url), \
                patch("scripts.import_network_data.engine.url", self.url):
            with self.assertRaisesRegex(RuntimeError, "target"):
                assert_empty_batch_database(self.connection, 5436)
            with self.assertRaisesRegex(RuntimeError, "overrides"):
                assert_fresh_batch_target(self.connection, 5436)
        self.connection.execute.assert_not_called()

    def test_wrong_port_blocks_before_any_query(self):
        with patch.dict(os.environ, {"DATABASE_URL": ""}), \
                patch("scripts.migrate.engine.url", self.url), \
                patch("scripts.import_network_data.engine.url", self.url):
            with self.assertRaisesRegex(RuntimeError, "target"):
                assert_empty_batch_database(self.connection, 5433)
            with self.assertRaisesRegex(RuntimeError, "target"):
                assert_fresh_batch_target(self.connection, 5433)
        self.connection.execute.assert_not_called()

    def test_existing_demo_port_is_never_a_fresh_batch_target(self):
        formal_url = self.url.set(port=5433)
        with patch.dict(os.environ, {"DATABASE_URL": ""}), \
                patch("scripts.migrate.engine.url", formal_url), \
                patch("scripts.import_network_data.engine.url", formal_url):
            with self.assertRaisesRegex(RuntimeError, "existing demo database"):
                assert_empty_batch_database(self.connection, 5433)
            with self.assertRaisesRegex(RuntimeError, "existing demo database"):
                assert_fresh_batch_target(self.connection, 5433)
        self.connection.execute.assert_not_called()

    def test_reused_database_blocks_migration_before_schema_write(self):
        self.connection.execute.return_value.scalar_one.return_value = 1
        with patch.dict(os.environ, {"DATABASE_URL": ""}), \
                patch("scripts.migrate.engine.url", self.url):
            with self.assertRaisesRegex(RuntimeError, "already has tables"):
                assert_empty_batch_database(self.connection, 5436)

    def test_reused_database_blocks_import_before_reset(self):
        versions = Mock()
        versions.scalars.return_value = [migration.VERSION for migration in MIGRATIONS]
        populated = Mock()
        populated.scalar_one.return_value = 1
        self.connection.execute.side_effect = (versions, populated)
        with patch.dict(os.environ, {"DATABASE_URL": ""}), \
                patch("scripts.import_network_data.engine.url", self.url):
            with self.assertRaisesRegex(RuntimeError, "not empty: demo_state"):
                assert_fresh_batch_target(self.connection, 5436)
        self.assertEqual(self.connection.execute.call_count, 2)

    def test_missing_migration_blocks_import_before_reset(self):
        versions = Mock()
        versions.scalars.return_value = [MIGRATIONS[0].VERSION]
        self.connection.execute.return_value = versions
        with patch.dict(os.environ, {"DATABASE_URL": ""}), \
                patch("scripts.import_network_data.engine.url", self.url):
            with self.assertRaisesRegex(RuntimeError, "13 schema migrations"):
                assert_fresh_batch_target(self.connection, 5436)
        self.assertEqual(self.connection.execute.call_count, 1)

    def test_batch_credentials_are_isolated_and_never_overwritten(self):
        GENERATED.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=GENERATED) as folder:
            path = checked_path(str(Path(folder) / "next.env"))
            create(path, 5436, 8015)
            with self.assertRaises(FileExistsError):
                create(path, 5436, 8015)
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://old",
                                              "POSTGRES_PORT": "5433"}), \
                    patch("scripts.batch_environment.subprocess.run") as child:
                child.return_value.returncode = 0
                self.assertEqual(run(path, ["python", "-V"]), 0)
                environment = child.call_args.kwargs["env"]
                self.assertEqual(environment["POSTGRES_PORT"], "5436")
                self.assertEqual(environment["DATABASE_URL"], "")
                self.assertEqual(environment["ALIYUN_IQS_ENABLED"], "false")
                self.assertNotEqual(environment["JWT_SECRET_KEY"], "")

    def test_only_exact_migration_policy_seeds_are_allowed(self):
        self.assertTrue(seed_policies_match(SEED_POLICIES))
        self.assertFalse(seed_policies_match(SEED_POLICIES[:-1]))
        changed = [dict(row) for row in SEED_POLICIES]
        changed[0]["body"] += "unexpected"
        self.assertFalse(seed_policies_match(changed))


if __name__ == "__main__":
    unittest.main()
