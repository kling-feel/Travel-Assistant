"""Email the operator when an encrypted OSS backup fails or becomes stale."""

import argparse
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import stat

try:
    from . import oss_backup
except ImportError:
    import oss_backup


ALERT_CONFIG = Path("/etc/travel-assistant/oss-alert.json")
ALERT_STATE = oss_backup.STATE_DIR / "last-alert.json"
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")


class AlertError(RuntimeError):
    pass


def load_alert_config(path: Path) -> dict:
    if (not path.is_file() or path.is_symlink() or path.stat().st_uid != 0 or
            stat.S_IMODE(path.stat().st_mode) & 0o077):
        raise AlertError("Alert configuration must be a root-only file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AlertError("Alert configuration is unreadable") from error
    if (not isinstance(value, dict) or value.get("smtp_host") != "smtp.qq.com" or
            value.get("smtp_port") != 465 or
            not all(isinstance(value.get(k), str) and value[k] for k in
                    ("sender", "recipient", "smtp_authorization_code")) or
            not EMAIL.fullmatch(value["sender"]) or
            not EMAIL.fullmatch(value["recipient"])):
        raise AlertError("Alert configuration fields are incomplete")
    return value


def send(config: dict, kind: str) -> None:
    subjects = {
        "backup-failed": "旅行助手：异地备份执行失败",
        "backup-stale": "旅行助手：超过 26 小时没有可核验的异地备份",
        "backup-recovered": "旅行助手：异地备份已恢复",
        "test": "旅行助手：异地备份邮件测试",
    }
    if kind not in subjects:
        raise AlertError("Unknown backup alert type")
    message = EmailMessage()
    message["From"] = config["sender"]
    message["To"] = config["recipient"]
    message["Subject"] = subjects[kind]
    message.set_content(
        "这是旅行助手异地加密备份的状态提醒：" + subjects[kind] +
        "。请检查服务器的 travel-oss-backup 服务状态和备份验收记录。"
        "本邮件不包含订单、密钥或备份内容。")
    try:
        with smtplib.SMTP_SSL(config["smtp_host"], config["smtp_port"],
                              context=ssl.create_default_context(), timeout=20) as client:
            client.login(config["sender"], config["smtp_authorization_code"])
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise AlertError("Backup alert email could not be delivered") from error


def read_state() -> dict:
    if not ALERT_STATE.is_file():
        return {}
    try:
        value = json.loads(ALERT_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_state(kind: str, now: datetime) -> None:
    temporary = ALERT_STATE.with_name("last-alert-" + os.urandom(4).hex() + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump({"kind": kind, "sent_utc": now.isoformat()}, output)
        os.replace(temporary, ALERT_STATE)
    finally:
        temporary.unlink(missing_ok=True)


def notify(config: dict, kind: str, *, force: bool = False) -> str:
    now = datetime.now(timezone.utc)
    previous = read_state()
    if not force and previous.get("kind") == kind:
        try:
            last = datetime.fromisoformat(previous["sent_utc"])
            if now - last < timedelta(hours=12):
                return "suppressed"
        except (KeyError, TypeError, ValueError):
            pass
    send(config, kind)
    if kind != "test":
        write_state(kind, now)
    return "sent"


def check_recovery(config: dict) -> str:
    try:
        oss_backup.check_fresh(oss_backup.load_config(oss_backup.DEFAULT_CONFIG), 26)
    except (oss_backup.BackupError, OSError, ValueError, KeyError):
        return notify(config, "backup-stale")
    previous = read_state()
    if previous.get("kind") in ("backup-failed", "backup-stale"):
        try:
            sent = datetime.fromisoformat(previous["sent_utc"])
            latest = json.loads((oss_backup.STATE_DIR / "last-success.json").read_text(
                encoding="utf-8"))
            completed = datetime.fromisoformat(latest["completed_utc"])
        except (OSError, ValueError, KeyError, TypeError):
            return "healthy"
        if completed > sent:
            return notify(config, "backup-recovered")
    return "healthy"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("failure", "check", "test"))
    args = parser.parse_args()
    try:
        if os.name != "posix" or os.geteuid() != 0:
            raise AlertError("Alert command must run as root on Linux")
        import fcntl
        config = load_alert_config(ALERT_CONFIG)
        oss_backup.STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (oss_backup.STATE_DIR / "alert.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.action == "test":
                result = notify(config, "test", force=True)
            elif args.action == "failure":
                result = notify(config, "backup-failed")
            else:
                result = check_recovery(config)
        print(json.dumps({"action": args.action, "result": result}))
    except (AlertError, OSError, ValueError) as error:
        parser.exit(1, "Backup alert failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
