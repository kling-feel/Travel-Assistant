"""Check a private production cloud environment without revealing its values."""

import argparse
from datetime import date, datetime
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.container_target import PUBLIC_DOMAIN


PRIVATE_DIR = ROOT / "data" / "generated"
IMAGE_DIGEST = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}\Z")
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")
REQUIRED = (
    "TRAVEL_CLOUD_ENV_FILE", "PUBLIC_HOST", "ACME_EMAIL",
    "POSTGRES_PASSWORD", "JWT_SECRET_KEY", "DEMO_TRAVELER_1_PASSWORD",
    "DEMO_TRAVELER_2_PASSWORD", "DEMO_BATCH_DATE", "AI_PROVIDER",
    "AI_MODEL", "AI_BASE_URL", "AI_API_KEY", "ALIYUN_IQS_ENABLED",
    "ALIYUN_IQS_API_KEY", "PYTHON_BASE_IMAGE", "POSTGRES_IMAGE",
    "CADDY_IMAGE",
)


class PreflightError(ValueError):
    """A configuration failure whose message contains field names only."""


def _read_values(path: Path) -> dict[str, str]:
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise PreflightError("cloud environment file cannot be read") from error
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PreflightError(f"invalid assignment on line {number}")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise PreflightError(f"invalid field name on line {number}")
        if key in values:
            raise PreflightError(f"duplicate field: {key}")
        values[key] = value
    return values


def check_file(path: Path, private_dir: Path = PRIVATE_DIR) -> None:
    path = path.resolve()
    if path.suffix != ".env" or path.parent != private_dir.resolve() or not path.is_file():
        raise PreflightError("choose an existing .env under data/generated")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise PreflightError("cloud environment permissions must be 0600")
    values = _read_values(path)
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        raise PreflightError("missing fields: " + ", ".join(missing))

    host = values["PUBLIC_HOST"]
    if (not PUBLIC_DOMAIN.fullmatch(host)
            or host.startswith("localhost.")
            or host.endswith((".test", ".example", ".invalid"))):
        raise PreflightError("PUBLIC_HOST must be a public DNS hostname")
    if values["TRAVEL_CLOUD_ENV_FILE"] != f"./data/generated/{path.name}":
        raise PreflightError("TRAVEL_CLOUD_ENV_FILE must refer to this file")
    if not EMAIL.fullmatch(values["ACME_EMAIL"]):
        raise PreflightError("ACME_EMAIL is invalid")
    if values.get("CLOUD_ACCEPTANCE_MODE", "false").lower() != "false":
        raise PreflightError("CLOUD_ACCEPTANCE_MODE must be false")
    if values.get("CLOUD_BIND_ADDRESS", "0.0.0.0") != "0.0.0.0":
        raise PreflightError("CLOUD_BIND_ADDRESS must be 0.0.0.0")

    secret_fields = (
        "POSTGRES_PASSWORD", "JWT_SECRET_KEY",
        "DEMO_TRAVELER_1_PASSWORD", "DEMO_TRAVELER_2_PASSWORD",
    )
    secrets = [values[key] for key in secret_fields]
    if any(len(value) < 24 or any(ch.isspace() for ch in value) for value in secrets):
        raise PreflightError("database, JWT and seed passwords need independent strong values")
    if len(set(secrets)) != len(secrets):
        raise PreflightError("database, JWT and seed passwords must differ")
    try:
        batch_date = date.fromisoformat(values["DEMO_BATCH_DATE"])
    except ValueError as error:
        raise PreflightError("DEMO_BATCH_DATE must be YYYY-MM-DD") from error
    if batch_date > datetime.now(ZoneInfo("Asia/Shanghai")).date():
        raise PreflightError("DEMO_BATCH_DATE cannot be in the future")
    if (values["AI_PROVIDER"] != "deepseek"
            or not values["AI_MODEL"].startswith("deepseek-")
            or values["AI_BASE_URL"] != "https://api.deepseek.com"):
        raise PreflightError("DeepSeek provider, model or base URL is invalid")
    if values["ALIYUN_IQS_ENABLED"].lower() != "true":
        raise PreflightError("ALIYUN_IQS_ENABLED must be true")
    for key in ("AI_API_KEY", "ALIYUN_IQS_API_KEY"):
        value = values[key]
        if (len(value) < 20 or any(ch.isspace() for ch in value)
                or value.startswith("local-test-only-")
                or value.lower().startswith(("your-", "replace-", "example-"))):
            raise PreflightError(f"{key} must contain a production credential")
    for key in ("PYTHON_BASE_IMAGE", "POSTGRES_IMAGE", "CADDY_IMAGE"):
        if not IMAGE_DIGEST.fullmatch(values[key]):
            raise PreflightError(f"{key} must pin a sha256 digest")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=PRIVATE_DIR / "cloud.env")
    args = parser.parse_args()
    try:
        check_file(args.env_file)
    except PreflightError as error:
        print(f"Cloud configuration preflight failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    print("Cloud configuration preflight passed; no credential values were printed.")


if __name__ == "__main__":
    main()
