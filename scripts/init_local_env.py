"""Create ignored local credentials without printing them."""

from pathlib import Path
from secrets import token_urlsafe


root = Path(__file__).resolve().parents[1]
target = root / ".env"
if target.exists():
    current = target.read_text(encoding="utf-8")
    additions = []
    for number in (1, 2):
        key = f"DEMO_TRAVELER_{number}_PASSWORD"
        if not any(line.startswith(f"{key}=") for line in current.splitlines()):
            additions.append(f"{key}={token_urlsafe(18)}")
    if additions:
        with target.open("a", encoding="utf-8") as env_file:
            env_file.write("\n" + "\n".join(additions) + "\n")
        print("Added two demo account passwords to ignored .env")
    else:
        print(".env already has demo account passwords; leaving it unchanged")
else:
    template = (root / ".env.example").read_text(encoding="utf-8")
    template = template.replace("replace-with-a-local-password", token_urlsafe(32))
    template = template.replace("replace-with-a-random-secret", token_urlsafe(48))
    template = template.replace("replace-with-demo-password-1", token_urlsafe(18))
    template = template.replace("replace-with-demo-password-2", token_urlsafe(18))
    target.write_text(template, encoding="utf-8")
    print("Created ignored .env with local database and JWT secrets")
