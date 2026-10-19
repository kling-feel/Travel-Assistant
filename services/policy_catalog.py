"""Reviewed fictional platform rules; these alone govern demo transactions."""

FLIGHT_POLICY = {
    "id": "flight-actions-demo-v3", "version": 3,
    "title": "虚构平台机票预订与退改规则 v3", "scope": "flight",
    "body": "本平台仅处理模拟订单。机票改签只补或退新旧模拟票价差额，不收额外改签费；取消收原票价的30%，其余70%仅作模拟应退核算。每笔新增、改签或取消均须旅客逐笔确认。",
}
HOTEL_POLICY_V3 = {
    "id": "hotel-actions-demo-v3", "version": 3,
    "title": "虚构平台酒店订单规则 v3", "scope": "hotel",
    "body": "本平台仅处理模拟订单。预订金额为每晚模拟价乘以晚数；改期按新旧总价差额核算；入住开始后不得改期或取消。取消不收额外手续费，但当前不承诺或计算具体退款额。每笔预订、改期或取消均须旅客逐笔确认。",
}
HOTEL_POLICY = {
    "id": "hotel-actions-demo-v4", "version": 4,
    "title": "虚构平台酒店预订与取消规则 v4", "scope": "hotel",
    "body": "本平台仅处理模拟酒店订单。预订金额为每晚模拟价乘以晚数；改期按新旧总价差额核算。预订和改期可选择产品机场当地入住当日；取消仅能在当地入住日期之前确认，手续费为0，模拟应退原订单总额；从当地入住日期开始不得取消。每笔预订、改期或取消均须旅客逐笔确认，不发生真实付款或退款。",
}
# v009 imports this name, so keep it pinned to its original v2 document.
RENTAL_POLICY = {
    "id": "rental-actions-demo-v2", "version": 2,
    "title": "虚构平台租车预订与取消规则 v2", "scope": "rental",
    "body": "本平台仅处理模拟租车订单。取还车须有明确的机场当地时刻；按实际经过时间每开始24小时计一个模拟日，至少一天，金额为日价乘计费日数。取车时刻之前取消不收手续费，模拟应退原订单全额；到达取车时刻后不得取消。每笔预订或取消均须旅客逐笔确认，不发生真实付款或退款。",
}
CURRENT_RENTAL_POLICY = {
    "id": "rental-actions-demo-v3", "version": 3,
    "title": "虚构平台租车预订、改期与取消规则 v3", "scope": "rental",
    "body": "本平台仅处理模拟租车订单。取还车须有明确的机场当地时刻；按实际经过时间每开始24小时计一个模拟日，金额为日价乘计费日数。原取车时刻前可保持车辆产品不变修改取还车时间，按新旧总价差额模拟补退，不收改期费；原取车时刻前取消模拟全额应退。到达原取车时刻后不得改期或取消。每笔预订、改期或取消均须旅客逐笔确认，不发生真实付款或退款。",
}
CURRENT_POLICIES = {policy["scope"]: policy for policy in
                    (FLIGHT_POLICY, HOTEL_POLICY, CURRENT_RENTAL_POLICY)}

# Keep historical IDs for action audit and for upgrades from the existing demo.
LEGACY_POLICIES = (
    {"id": "flight-change-demo", "version": 1, "title": "演示机票退改规则",
     "scope": "flight", "body": "确认中的机票可提出改签或取消；费用仅为模拟值。最终操作必须逐笔确认。"},
    {"id": "hotel-change-demo", "version": 1, "title": "演示酒店退改规则",
     "scope": "hotel", "body": "演示酒店订单可提出改期或取消；价格仅为模拟值。最终操作必须逐笔确认。"},
    {"id": "flight-actions-demo-v2", "version": 2,
     "title": "演示机票改签与取消规则 v2", "scope": "flight",
     "body": "改签只补或退新旧票价差额，无额外手续费；取消收原票价30%，余款仅作模拟应退核算。所有操作须逐笔确认。"},
    {"id": "hotel-actions-demo-v2", "version": 2,
     "title": "演示酒店订单规则 v2", "scope": "hotel",
     "body": "预订按每晚价乘以晚数；改期按新旧总价差额核算；取消无额外手续费。所有操作须逐笔确认。"},
    {"id": "rental-actions-demo-v1", "version": 1,
     "title": "虚构平台租车规则 v1", "scope": "rental",
     "body": "本平台仅展示模拟租车产品。取车开始前可免费取消，取车开始后不得取消；预订及取消订单功能将在后续阶段开放，任何写入仍须旅客逐笔确认。不发生真实付款或退款。"},
)
# Keep v007's import set stable; v009 introduces the new rental version.
ALL_POLICIES = (*LEGACY_POLICIES, FLIGHT_POLICY, HOTEL_POLICY_V3)
SEED_POLICIES = (*ALL_POLICIES, RENTAL_POLICY, HOTEL_POLICY,
                 CURRENT_RENTAL_POLICY)
