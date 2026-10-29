"""Create an encrypted, off-server backup of the active cloud database.

Run on the cloud host as root. Only the public age recipient is present here;
the recovery identity remains with the operator. Credentials are read from a
root-only JSON file and passed to ossutil in environment variables.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tarfile
import tempfile


ROOT = Path("/home/travelops/travel-assistant")
PROJECT = "travel-cloud"
STATE_DIR = Path("/var/lib/travel-assistant-oss-backup")
DEFAULT_CONFIG = Path("/etc/travel-assistant/oss-backup.json")
BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{2,62}\Z")
REGION = re.compile(r"[a-z]{2}-[a-z]+-\d\Z")
PREFIX = re.compile(r"(?:[a-z0-9][a-z0-9-]*/)+\Z")
RECIPIENT = re.compile(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}\Z")


class BackupError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_config(path: Path, *, require_private: bool = True) -> dict:
    if not path.is_file():
        raise BackupError("Backup configuration is missing")
    if require_private and (path.is_symlink() or path.stat().st_uid != 0 or
                            stat.S_IMODE(path.stat().st_mode) & 0o077):
        raise BackupError("Backup configuration must be owner-only")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BackupError("Backup configuration is invalid") from error
    required = ("bucket", "region", "prefix", "recipient", "access_key_id",
                "access_key_secret", "age_path", "ossutil_path")
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or
                                          not value[k] for k in required):
        raise BackupError("Backup configuration fields are incomplete")
    if (not BUCKET.fullmatch(value["bucket"]) or
            not REGION.fullmatch(value["region"]) or
            not PREFIX.fullmatch(value["prefix"]) or
            not RECIPIENT.fullmatch(value["recipient"])):
        raise BackupError("Backup destination or recipient is invalid")
    if value["region"] != "ap-southeast-1":
        raise BackupError("Backup region differs from the approved destination")
    if value["prefix"] != "travel-assistant/backups/":
        raise BackupError("Backup prefix differs from the approved destination")
    for key in ("age_path", "ossutil_path"):
        tool = Path(value[key])
        if not tool.is_absolute() or not tool.is_file():
            raise BackupError("Backup tool is missing")
        if require_private and (tool.is_symlink() or tool.stat().st_uid != 0 or
                                stat.S_IMODE(tool.stat().st_mode) & 0o022):
            raise BackupError("Backup tool ownership or permissions differ")
    return value


def run(command: list[str], *, env: dict | None = None, timeout: int = 90) -> str:
    try:
        process = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                 text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BackupError("Backup subprocess could not complete") from error
    if process.returncode:
        recognized = ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch",
                      "NoSuchBucket", "NoSuchKey", "InvalidArgument", "InvalidSecurityToken")
        category = next((code for code in recognized if code in process.stderr), None)
        message = "Backup subprocess failed: " + Path(command[0]).name
        raise BackupError(message + (" (" + category + ")" if category else ""))
    return process.stdout.strip()


def inspect(command: list[str]) -> dict:
    items = json.loads(run(command))
    if not isinstance(items, list) or len(items) != 1:
        raise BackupError("Unexpected Docker inspection result")
    return items[0]


def check_source() -> tuple[Path, str, str]:
    containers = {}
    for service in ("db", "app", "proxy"):
        item = inspect(["docker", "container", "inspect", f"{PROJECT}-{service}-1"])
        labels = item["Config"]["Labels"]
        if (labels.get("com.docker.compose.project") != PROJECT or
                labels.get("com.docker.compose.service") != service or
                not item["State"]["Running"]):
            raise BackupError("Cloud container identity or state differs")
        containers[service] = item
    if (containers["db"]["State"]["Health"]["Status"] != "healthy" or
            containers["app"]["State"]["Health"]["Status"] != "healthy" or
            containers["db"]["HostConfig"]["PortBindings"] or
            containers["app"]["HostConfig"]["PortBindings"] or
            set(containers["proxy"]["HostConfig"]["PortBindings"] or {}) !=
            {"80/tcp", "443/tcp"}):
        raise BackupError("Cloud service health or exposure differs")
    mounts = {}
    for volume in ("travel_pgdata", "travel_backups", "travel_manifest",
                   "caddy_data", "caddy_config"):
        item = inspect(["docker", "volume", "inspect", f"{PROJECT}_{volume}"])
        if item["Labels"].get("com.docker.compose.project") != PROJECT:
            raise BackupError("Cloud volume identity differs")
        mounts[volume] = Path(item["Mountpoint"])
    manifest = mounts["travel_manifest"] / "batch.json"
    batch = json.loads(manifest.read_text(encoding="utf-8"))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", batch.get("as_of", "")):
        raise BackupError("Batch manifest is invalid")
    if len(batch.get("migration_versions", [])) < 14:
        raise BackupError("Batch manifest migrations differ")
    actual = run(["docker", "exec", f"{PROJECT}-db-1", "psql", "-X", "-U",
                  "travel_app", "-d", "travel_demo", "-At", "-c",
                  "BEGIN READ ONLY; SELECT as_of FROM demo_state LIMIT 1; COMMIT;"])
    if batch["as_of"] not in actual.splitlines():
        raise BackupError("Database and manifest batch dates differ")
    return manifest, batch["as_of"], containers["app"]["Image"]


def postgres_image() -> str:
    source = ROOT / "data/generated/cloud.env"
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.startswith("POSTGRES_IMAGE="):
            image = line.partition("=")[2].strip()
            if "@sha256:" in image:
                return image
    raise BackupError("Pinned PostgreSQL image is missing")


def dump_database(target: Path) -> None:
    try:
        with target.open("xb") as output:
            process = subprocess.run(
                ["docker", "exec", f"{PROJECT}-db-1", "pg_dump", "-U",
                 "travel_app", "-d", "travel_demo", "-Fc"],
                stdout=output, stderr=subprocess.PIPE, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BackupError("Database export could not complete") from error
    if process.returncode or target.stat().st_size < 1000:
        raise BackupError("Database export failed")
    with target.open("rb") as source:
        if source.read(5) != b"PGDMP":
            raise BackupError("Database export format is invalid")
    run(["docker", "run", "--rm", "--network", "none", "--memory", "256m",
         "-v", f"{target.parent}:/backup:ro", "--entrypoint", "pg_restore",
         postgres_image(), "-l", "/backup/database.dump"], timeout=120)


def oss_env(config: dict) -> dict:
    result = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(STATE_DIR)}
    result["OSS_ACCESS_KEY_ID"] = config["access_key_id"]
    result["OSS_ACCESS_KEY_SECRET"] = config["access_key_secret"]
    result["OSS_REGION"] = config["region"]
    result["OSS_ENDPOINT"] = "https://oss-" + config["region"] + ".aliyuncs.com"
    return result


def object_size(value: object) -> int | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower().replace("-", "").replace("_", "") == "contentlength":
                try:
                    if isinstance(item, list) and len(item) == 1:
                        item = item[0]
                    return int(item)
                except (TypeError, ValueError):
                    return None
        for item in value.values():
            found = object_size(item)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = object_size(item)
            if found is not None:
                return found
    return None


def object_digest(value: object) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            name = key.lower().replace("-", "").replace("_", "")
            if name in ("sha256", "xossmetasha256"):
                if isinstance(item, list) and len(item) == 1:
                    item = item[0]
                if isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item):
                    return item
                return None
        for item in value.values():
            found = object_digest(item)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = object_digest(item)
            if found is not None:
                return found
    return None


def upload(ciphertext: Path, config: dict, key: str) -> None:
    if ciphertext.stat().st_size > 5 * 1024**3:
        raise BackupError("Encrypted backup exceeds the single-object upload limit")
    oss = config["ossutil_path"]
    common = [oss, "api"]
    environment = oss_env(config)
    run(common + ["put-object", "--bucket", config["bucket"], "--key", key,
                  "--body", "file://" + str(ciphertext), "--forbid-overwrite=true",
                  "--metadata",
                  "sha256=" + sha256(ciphertext)],
        env=environment, timeout=120)
    size, digest = head_object(config, key)
    if size != ciphertext.stat().st_size or digest != sha256(ciphertext):
        raise BackupError("OSS object metadata differs from encrypted backup")


def head_object(config: dict, key: str) -> tuple[int, str]:
    raw = run([config["ossutil_path"], "api", "head-object", "--bucket",
               config["bucket"], "--key", key, "--output-format", "json"],
              env=oss_env(config), timeout=30)
    try:
        value, offset = json.JSONDecoder().raw_decode(raw)
        trailer = raw[offset:].strip()
        if trailer and not re.fullmatch(r"\d+(?:\.\d+)?\(s\) elapsed", trailer):
            raise ValueError("Unexpected OSS metadata output trailer")
        size = object_size(value)
        digest = object_digest(value)
    except ValueError as error:
        raise BackupError("OSS object metadata was not readable") from error
    if size is None or digest is None:
        raise BackupError("OSS object metadata was incomplete")
    return size, digest


def save_state(value: dict) -> None:
    target = STATE_DIR / "last-success.json"
    temporary = STATE_DIR / ("last-success-" + secrets.token_hex(4) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def backup(config: dict) -> dict:
    if os.name != "posix" or os.geteuid() != 0:
        raise BackupError("Cloud backup must run as root on Linux")
    os.umask(0o077)
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (STATE_DIR.is_symlink() or STATE_DIR.stat().st_uid != 0 or
            stat.S_IMODE(STATE_DIR.stat().st_mode) & 0o077):
        raise BackupError("Backup state directory is not private")
    import fcntl
    with (STATE_DIR / "backup.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BackupError("Another backup is already running") from error
        manifest, batch, image = check_source()
        if os.statvfs(STATE_DIR).f_bavail * os.statvfs(STATE_DIR).f_frsize < 1024**3:
            raise BackupError("Less than 1 GiB of free disk remains")
        with tempfile.TemporaryDirectory(prefix="run-", dir=STATE_DIR) as temp:
            work = Path(temp)
            dump = work / "database.dump"
            dump_database(dump)
            batch_copy = work / "batch.json"
            batch_copy.write_bytes(manifest.read_bytes())
            created = datetime.now(timezone.utc)
            metadata = {
                "format": "travel-cloud-oss-recovery-v1",
                "created_utc": created.isoformat(),
                "source_project": PROJECT,
                "source_app_image": image,
                "batch_date": batch,
                "dump_sha256": sha256(dump),
                "manifest_sha256": sha256(batch_copy),
            }
            meta = work / "metadata.json"
            meta.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
            bundle = work / "bundle.tar"
            with tarfile.open(bundle, "w") as archive:
                for path in (dump, batch_copy, meta):
                    archive.add(path, arcname=path.name, recursive=False)
            ciphertext = work / "backup.tar.age"
            run([config["age_path"], "-r", config["recipient"], "-o",
                 str(ciphertext), str(bundle)], timeout=120)
            with ciphertext.open("rb") as source:
                if source.read(21) != b"age-encryption.org/v1":
                    raise BackupError("Encrypted backup header is invalid")
            stamp = created.strftime("%Y%m%dT%H%M%SZ")
            key = (config["prefix"] + batch + "/" + stamp + "-" +
                   secrets.token_hex(8) + ".tar.age")
            upload(ciphertext, config, key)
            result = {
                "completed_utc": datetime.now(timezone.utc).isoformat(),
                "batch_date": batch,
                "bucket": config["bucket"],
                "object_key": key,
                "bytes": ciphertext.stat().st_size,
                "sha256": sha256(ciphertext),
            }
            save_state(result)
            return result


def check_fresh(config: dict, max_hours: float) -> dict:
    state = json.loads((STATE_DIR / "last-success.json").read_text(encoding="utf-8"))
    finished = datetime.fromisoformat(state["completed_utc"])
    age_hours = (datetime.now(timezone.utc) - finished).total_seconds() / 3600
    if (age_hours < 0 or age_hours > max_hours or
            state["bucket"] != config["bucket"] or
            not state["object_key"].startswith(config["prefix"])):
        raise BackupError("Recent verified backup is missing or stale")
    if head_object(config, state["object_key"]) != (
            state["bytes"], state["sha256"]):
        raise BackupError("Recent OSS backup has changed")
    return {"age_hours": round(age_hours, 2), "object_key": state["object_key"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "run", "check"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--max-hours", type=float, default=26)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.action == "preflight":
            print(json.dumps({"bucket": config["bucket"], "region": config["region"],
                              "prefix": config["prefix"], "config_private": True}))
        elif args.action == "run":
            report = backup(config)
            print(json.dumps(report, sort_keys=True))
        else:
            print(json.dumps(check_fresh(config, args.max_hours), sort_keys=True))
    except (BackupError, OSError, ValueError, KeyError) as error:
        parser.exit(1, "Backup check failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
