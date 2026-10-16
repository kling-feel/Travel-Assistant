"""Lightweight lookup for the legacy demo FAQ.

The first project milestone must start without an embedding API call. A
versioned fictional policy knowledge base will replace this in phase P1/P2.
"""

import re
from pathlib import Path

from langchain_core.tools import tool


FAQ_PATH = Path(__file__).resolve().parent.parent / "order_faq.md"
sections = [part.strip() for part in re.split(r"(?=^## )", FAQ_PATH.read_text(encoding="utf-8"), flags=re.M) if part.strip()]


@tool
def lookup_policy(query: str) -> str:
    """Find related text in the legacy local demo FAQ; not a transaction rule."""
    terms = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}", query.lower())
    scored = sorted(
        ((sum(term in section.lower() for term in terms), section) for section in sections),
        key=lambda item: item[0],
        reverse=True,
    )
    matches = [section for score, section in scored[:2] if score > 0]
    if not matches:
        return "演示 FAQ 中未找到可靠依据；无法确认该规则。"
    return "\n\n".join(matches)
