"""Deterministic boundaries before model routing or transaction proposals."""

import re


POLICY_WORDS = re.compile(
    r"政策|规则|条款|费用|手续费|退改|改签|退票|退款|行李|托运|取消|"
    r"\b(?:policy|rule|fee|change|refund|cancel|baggage|luggage)\b",
    re.IGNORECASE,
)
EXTERNAL_SOURCE = re.compile(
    r"真实|实际|官网|官方网站|官方渠道|现实中|具体航司|其他航司|"
    r"汉莎|达美|阿联酋|美联航|联合航空|美国航空|英国航空|国航|东航|南航|"
    r"海航|春秋航空|厦门航空|卡塔尔航空|新加坡航空|"
    r"\b(?:Delta|Emirates|United|Lufthansa|American Airlines|British Airways|"
    r"Qatar Airways|Singapore Airlines|Air France|KLM)\b|"
    r"\b[A-Za-z][A-Za-z ]{1,35}(?:Airlines|Airways|Air)\b|"
    r"(?:[\u4e00-\u9fff]{2,8}航空)(?:公司)?",
    re.IGNORECASE,
)
CARD_WORDS = re.compile(r"卡片?|待确认|提案|刚才|上面|上一张|这张|这些|两张")
WITHDRAW_WORDS = re.compile(r"撤回|拒绝|不执行|不要执行|先不订|不订了|不订|不办理|作废")
CARD_CANCEL = re.compile(
    r"取消(?:刚才的?|上面的?|这张|这些|两张|所有|该)?(?:待确认)?(?:卡片?|提案)|"
    r"(?:卡片?|待确认卡|提案)(?:先)?(?:取消|撤销)"
)
APPROVE_WORDS = re.compile(r"我同意|同意了|直接付款|替我点确认|帮我点确认|确认上面|确认这张|确认刚才")


def boundary_intent(text):
    """Return a guarded intent, or None for ordinary model routing."""
    card_reference = CARD_WORDS.search(text)
    if ((card_reference and WITHDRAW_WORDS.search(text)) or
            CARD_CANCEL.search(text) or
            re.search(r"先不订|不订了", text)):
        return "withdraw"
    if APPROVE_WORDS.search(text):
        return "approve"
    if POLICY_WORDS.search(text):
        if EXTERNAL_SOURCE.search(text):
            return "external_policy"
        if (re.search(r"航司|航空公司|\bairline\b", text, re.IGNORECASE)
                and not re.search(r"本平台|虚构平台|模拟平台", text)):
            return "ambiguous_policy"
    return None
