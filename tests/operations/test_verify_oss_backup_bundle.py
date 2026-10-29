"""The offline verifier must reject a bundle whose contents changed."""

import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts.verify_oss_backup_bundle import InvalidBundle, verify


def write_bundle(path: Path, *, corrupt: bool = False) -> None:
    folder = path.parent
    dump = folder / "database.dump"
    batch = folder / "batch.json"
    metadata = folder / "metadata.json"
    dump.write_bytes(b"PGDMP" + b"archive")
    batch.write_text('{"as_of":"2026-10-27"}', encoding="utf-8")
    metadata.write_text(json.dumps({
        "format": "travel-cloud-oss-recovery-v1", "source_project": "travel-cloud",
        "batch_date": "2026-10-27",
        "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(batch.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    if corrupt:
        dump.write_bytes(b"PGDMP" + b"changed")
    with tarfile.open(path, "w") as archive:
        for item in (dump, batch, metadata):
            archive.add(item, arcname=item.name)


class BundleVerificationTests(unittest.TestCase):
    def test_accepts_matching_dump_and_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "backup.tar"
            write_bundle(archive)
            self.assertEqual(verify(archive)["batch_date"], "2026-10-27")

    def test_rejects_tampered_dump(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "backup.tar"
            write_bundle(archive, corrupt=True)
            with self.assertRaisesRegex(InvalidBundle, "does not match"):
                verify(archive)


if __name__ == "__main__":
    unittest.main()
