"""Verify a decrypted OSS backup bundle before restoring to an isolated volume.

This command reads an already decrypted tar archive. Keep that archive inside a
private temporary directory and remove it after the restore exercise.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile


EXPECTED = {"database.dump", "batch.json", "metadata.json"}


class InvalidBundle(RuntimeError):
    pass


def member_hash(archive: tarfile.TarFile, member: tarfile.TarInfo) -> tuple[str, bytes]:
    source = archive.extractfile(member)
    if source is None:
        raise InvalidBundle("Bundle member is not a regular file")
    digest = hashlib.sha256()
    first = b""
    with source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            if not first:
                first = block[:5]
            digest.update(block)
    return digest.hexdigest(), first


def verify(path: Path) -> dict:
    if not path.is_file():
        raise InvalidBundle("Decrypted bundle is missing")
    with tarfile.open(path, "r:") as archive:
        members = archive.getmembers()
        if ({item.name for item in members} != EXPECTED or
                len(members) != 3 or any(not item.isfile() for item in members)):
            raise InvalidBundle("Unexpected backup bundle entries")
        indexed = {item.name: item for item in members}
        if indexed["batch.json"].size > 1024 * 1024 or indexed["metadata.json"].size > 1024 * 1024:
            raise InvalidBundle("Backup metadata is unexpectedly large")
        batch_raw = archive.extractfile(indexed["batch.json"]).read()
        metadata_raw = archive.extractfile(indexed["metadata.json"]).read()
        dump_hash, header = member_hash(archive, indexed["database.dump"])
    try:
        batch = json.loads(batch_raw)
        metadata = json.loads(metadata_raw)
    except (UnicodeError, ValueError) as error:
        raise InvalidBundle("Backup metadata is unreadable") from error
    if not isinstance(batch, dict) or not isinstance(metadata, dict):
        raise InvalidBundle("Backup metadata has an unexpected shape")
    batch_hash = hashlib.sha256(batch_raw).hexdigest()
    if (header != b"PGDMP" or metadata.get("format") !=
            "travel-cloud-oss-recovery-v1" or metadata.get("source_project") !=
            "travel-cloud" or metadata.get("dump_sha256") != dump_hash or
            metadata.get("manifest_sha256") != batch_hash or
            metadata.get("batch_date") != batch.get("as_of") or
            not re.fullmatch(r"\d{4}-\d{2}-\d{2}", batch.get("as_of", ""))):
        raise InvalidBundle("Backup content does not match its manifest")
    return {"batch_date": batch["as_of"], "dump_sha256": dump_hash,
            "manifest_sha256": batch_hash, "archive_bytes": path.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.bundle), sort_keys=True))
    except (InvalidBundle, OSError, tarfile.TarError) as error:
        parser.exit(1, "Bundle verification failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
