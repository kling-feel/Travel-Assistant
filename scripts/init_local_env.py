"""Create ignored local credentials without printing them."""

from pathlib import Path
from secrets import token_urlsafe


root = Path(__file__).resolve().parents[1]
target = root / ".env"
if target.exists():
    print(".env already exists; leaving it unchanged")
else:
    template = (root / ".env.example").read_text(encoding="utf-8")
    template = template.replace("replace-with-a-local-password", token_urlsafe(32))
    template = template.replace("replace-with-a-random-secret", token_urlsafe(48))
    target.write_text(template, encoding="utf-8")
    print("Created ignored .env with local database and JWT secrets")
