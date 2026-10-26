"""Run the small-host cloud Compose stack with one consistent project target."""

import argparse
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ENV_DIR = ROOT / "data/generated"
DEFAULT_ENV = PRIVATE_ENV_DIR / "cloud.env"
PROJECT = re.compile(r"[a-z][a-z0-9_-]{2,39}\Z")


def compose_command(env_file: Path, project: str, action: str,
                    account_args: list[str] | None = None) -> list[str]:
    if not PROJECT.fullmatch(project):
        raise ValueError("Choose a lowercase Compose project name of 3 to 40 characters")
    if (not env_file.is_file() or env_file.suffix != ".env"
            or env_file.resolve().parent != PRIVATE_ENV_DIR.resolve()):
        raise ValueError("A private cloud .env file under data/generated must exist")
    command = [
        "docker", "compose",
        "-f", "compose.cloud.yaml",
        "-f", "compose.cloud.2g.yaml",
        "--env-file", str(env_file),
        "-p", project,
    ]
    actions = {
        "services": ["config", "--services"],
        "build": ["build", "app", "init", "account-admin"],
        "db-up": ["up", "-d", "--wait", "db"],
        "empty-backup": ["--profile", "setup", "run", "--rm", "backup"],
        "init": ["--profile", "setup", "run", "--rm", "init"],
        "app-up": ["up", "-d", "--wait", "app", "proxy"],
        "status": ["ps"],
        "stop": ["stop", "proxy", "app", "db"],
    }
    if action == "admin":
        if not account_args or account_args[0] not in {
                "create", "rotate", "disable", "disable-seeds"}:
            raise ValueError("Choose a supported account administration action")
        return command + ["--profile", "admin", "run", "--rm", "account-admin"] + account_args
    if account_args:
        raise ValueError("Account arguments are only accepted for the admin action")
    if action not in actions:
        raise ValueError("Unsupported cloud Compose action")
    return command + actions[action]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--project", default="travel-cloud")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the command structure without reading secret values")
    parser.add_argument("action", choices=(
        "services", "build", "db-up", "empty-backup", "init",
        "admin", "app-up", "status", "stop"))
    parser.add_argument("account_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        command = compose_command(args.env_file.resolve(), args.project,
                                  args.action, args.account_args)
    except ValueError as error:
        parser.error(str(error))
    if args.dry_run:
        print(" ".join(command))
        return
    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
