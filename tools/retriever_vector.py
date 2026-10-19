"""Read-only lookup over reviewed fictional policy text, without an API call."""

import re
from pathlib import Path

from langchain_core.tools import tool


POLICY_PATH = Path(__file__).resolve().parent.parent / "data" / "policies" / "fictional_platform.md"
sections = [part.strip() for part in re.split(
    r"(?=^## )", POLICY_PATH.read_text(encoding="utf-8"), flags=re.M)
    if part.strip().startswith("## ")]


@tool
def lookup_policy(query: str) -> str:
    """Return one current fictional rule with ID/version, never legacy FAQ text."""
    lower = query.lower()
    scopes = (("flight", ("航班", "机票", "改签", "flight", "ticket")),
              ("hotel", ("酒店", "房间", "入住", "hotel")),
              ("rental", ("租车", "取车", "车辆", "car", "rental")))
    matched = [scope for scope, words in scopes if any(word in lower for word in words)]
    if len(matched) != 1:
        return "无法从已审核的虚构平台政策确定唯一规则；请明确询问机票、酒店或租车。本内容不适用于真实供应商。"
    section = next((part for part in sections if part.startswith(f"## {matched[0]} |")), None)
    return ("以下仅为虚构平台模拟规则，不适用于真实供应商：\n" + section
            if section else "未找到已审核的虚构平台规则，无法确认。")
