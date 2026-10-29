"""Prepare private OSS backup configuration and a narrow RAM policy."""

import argparse
import json
import os
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "data/generated"
RECIPIENT = PRIVATE / "cloud-backups/recovery-recipient.txt"
BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{2,62}\Z")
PREFIX = "travel-assistant/backups/"


def create(bucket: str) -> tuple[Path, Path]:
    if not BUCKET.fullmatch(bucket):
        raise ValueError("Invalid private bucket name")
    recipient = RECIPIENT.read_text(encoding="ascii").strip()
    if not recipient.startswith("age1") or len(recipient) != 62:
        raise ValueError("Existing recovery public key is invalid")
    config_path = PRIVATE / "p34c3c2-oss-config.json"
    policy_path = PRIVATE / "p34c3c2-ram-policy.json"
    if config_path.exists() or policy_path.exists():
        raise FileExistsError("Private OSS setup files already exist; refusing overwrite")
    config = {
        "bucket": bucket, "region": "ap-southeast-1", "prefix": PREFIX,
        "recipient": recipient, "access_key_id": "", "access_key_secret": "",
        "age_path": "/usr/local/bin/age", "ossutil_path": "/usr/local/bin/ossutil",
    }
    policy = {
        "Version": "1",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["oss:PutObject", "oss:GetObject"],
            "Resource": ["acs:oss:*:*:" + bucket + "/" + PREFIX + "*"],
        }],
    }
    previous = os.umask(0o077)
    try:
        with config_path.open("x", encoding="utf-8") as output:
            json.dump(config, output, ensure_ascii=False, indent=2)
            output.write("\n")
        with policy_path.open("x", encoding="utf-8") as output:
            json.dump(policy, output, ensure_ascii=False, indent=2)
            output.write("\n")
    finally:
        os.umask(previous)
    return config_path, policy_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bucket")
    args = parser.parse_args()
    config_path, policy_path = create(args.bucket)
    print("Private configuration template:", config_path)
    print("Narrow RAM policy:", policy_path)
    print("Only enter the AccessKey in the private configuration; do not paste it into chat.")


if __name__ == "__main__":
    main()
