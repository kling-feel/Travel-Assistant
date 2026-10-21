"""New read-only LangGraph, isolated from the legacy SQLite workflow."""

import json
import re
from datetime import date, timedelta
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from services.dialogue_boundaries import boundary_intent
from services.dialogue_context import (declared_current_city, local_today,
                                       mentioned_cities, mentioned_dates,
                                       read_request, requested_actions,
                                       stay_nights)
from services.location_labels import city_zh
from config import settings


INTENTS = ("general", "itinerary", "policy", "flight_search", "hotel_search",
           "rental_search", "transaction", "recommendation", "action_guidance",
           "weather", "capabilities", "hotel_coverage", "multi_read")
ROUTER_PROMPT = """你是虚构旅行平台的主助手。只输出一个 JSON 对象，字段为 intent、params、answer。
intent 只能是 general、itinerary、policy、flight_search、hotel_search、rental_search、transaction、recommendation。
航班/酒店/租车预订（含“预定”）、改期、取消属于 transaction；景点或旅行地点推荐属于 recommendation。
政策查询用 policy，params.scope 只能是 flight/hotel/rental。本人订单用 itinerary。
搜索参数：flight_search 用 departure/arrival 城市或机场原文、travel_date YYYY-MM-DD、可选 sort=price；
hotel_search 用 city/check_in/check_out；rental_search 用 city 或 airport、pickup_date/drop_off_date。中文地名和相对日期由服务端规范化，不要猜机场代码。
天气查询用 weather；能否联网用 capabilities；哪些机场有酒店用 hotel_coverage。
“我在上海”是出发地或所在地，“想去其他国家”不是要推荐上海。旅客补充日期时沿用最近明确的查询任务。
recommendation 的 params 可含 city（用户明确提到的城市原文）和 preference；没有明确城市时留空，不能从记忆编造地点。
游览/景点预订不支持，仍走 recommendation 说明与推荐，不能生成 transaction 动作。
transaction 的 params 可含 actions（最多两项）和 queries（最多两项）。每项 action 只有 action_type 与 parameters；
action_type 只能是 flight_book/change/cancel、hotel_book/change/cancel、rental_book/change/cancel。
action_type 必须写完整英文名，例如 flight_book、hotel_change；不是 book、change。
每种 parameters 只允许以下字段，缺少的可不写：
flight_book: flight_id；flight_change: order_id, new_flight_id；flight_cancel: order_id；
hotel_book: product_id, check_in, check_out；hotel_change: order_id, product_id, check_in, check_out；hotel_cancel: order_id；
rental_book: product_id, pickup_at, dropoff_at；rental_change: order_id, pickup_at, dropoff_at；rental_cancel: order_id。
绝不填 expected_version、旅客 ID、金额、政策、状态或确认决定；订单 ID/版本由服务端核实和补齐。
用户明确要求预订、改期、取消时，必须用 transaction 并在 actions 放对应动作，即使还需查询本人订单或产品；
不能把明确交易请求只转为 itinerary 或只有 queries。本人某类订单未指明 ID，可省略 order_id，服务端会判断是否唯一。
用户明确写 SIM-15890 时，航班 ID 为整数 15890；写产品 room-1 或 rental-BSL-1 时，原样复制 product_id。
日期/时刻只复制用户明确提供的值；租车时刻要带当地时区偏移，如 2026-10-22T12:00:00+02:00，不能自行猜测。
例如“取消我的机票”输出 {{"intent":"transaction","params":{{"actions":[{{"action_type":"flight_cancel","parameters":{{}}}}]}},"answer":""}}。
例如“预订航班 SIM-15890”输出 {{"intent":"transaction","params":{{"actions":[{{"action_type":"flight_book","parameters":{{"flight_id":15890}}}}]}},"answer":""}}。
queries 每项只有 intent 和 params，intent 只能是 flight_search、hotel_search、rental_search、itinerary。
若对象或条件不明确，仍写出用户明确要求的动作并省略未知字段，可提出 queries 以展示候选；绝不声称已预订。
不知道参数时留空，不能编造机场、日期、订单、价格或政策。general 的 answer 只回答问候、偏好澄清等普通话题，
不得声称已预订或给出未经核实的地点、票价、真实航司政策。其他 intent 的 answer 留空。
当前中国日期：{current_date}；演示数据基准日：{demo_date}。相对日期以当前中国日期为准。旅客与助手最近消息：\n{history}"""


class TurnState(TypedDict, total=False):
    history: list[dict]
    demo_date: str
    intent: str
    params: dict
    draft: str
    answer: str
    references: list[dict]
    transaction_plan: list[dict]
    context: dict
    candidates: dict
    task_contexts: dict
    location_context: dict
    nearby_city: str


def _safe_general(draft):
    if (not isinstance(draft, str) or not 1 <= len(draft.strip()) <= 500
            or re.search(r"\d|已预订|已改签|已取消|付款成功|退款成功|票价|航司政策|开放时间", draft)):
        return "我可以帮你查询行程、航班、酒店、租车和本平台政策。请告诉我具体需求。"
    return draft.strip()


def _scoped_candidates(intent, references, params, *, earliest_id=None,
                       extreme_id=None, flight_details=()):
    """Keep task scope in the checkpoint without changing public citations."""
    rows = []
    for item in references:
        if not isinstance(item, dict):
            continue
        scoped = dict(item)
        if extreme_id is not None and item.get("id") == extreme_id:
            scoped["extreme_unique"] = True
        if intent == "flight_search" and item.get("type") == "flight":
            detail = next((row for row in flight_details if row.get("id") == item.get("id")), {})
            scoped.update({key: value for key, value in detail.items() if key != "id"})
            if earliest_id is not None and item.get("id") == earliest_id:
                scoped["earliest_unique"] = True
            scoped["departure"] = (mentioned_cities(str(params.get("departure") or "")) or
                                   [params.get("departure")])[0]
            scoped["arrival"] = (mentioned_cities(str(params.get("arrival") or "")) or
                                 [params.get("arrival")])[0]
            scoped["arrival_airport"] = scoped.get("arrival_airport")
        elif intent == "hotel_search" and item.get("type") == "hotel_product":
            scoped["city"] = params.get("city")
        elif intent == "rental_search" and item.get("type") == "rental_product":
            scoped["city"] = params.get("city")
            scoped["airport"] = params.get("airport")
        rows.append(scoped)
    return rows


def _chosen_landing(candidates, text):
    flights = [item for item in candidates if isinstance(item, dict)
               and item.get("type") == "flight" and item.get("arrival_airport")]
    match = re.search(r"SIM-(\d+)", text, re.I)
    if match:
        return next((item for item in flights if str(item.get("id")) == match.group(1)), None)
    match = re.search(r"第\s*([一二三123])\s*(?:个|班|趟)?", text)
    order = {"一": 0, "二": 1, "三": 2, "1": 0, "2": 1, "3": 2}
    if match and order[match.group(1)] < len(flights):
        return flights[order[match.group(1)]]
    if match:
        return None
    if "最早" in text:
        marked = [item for item in flights if item.get("earliest_unique")]
        if len(marked) == 1:
            return marked[0]
    if len(flights) == 1:
        return flights[0]
    return None


def build_graph(model, read_evidence, checkpointer):
    def locate(state: TurnState):
        text = state["history"][-1]["content"]
        location = dict(state.get("location_context") or {})
        claimed = declared_current_city(text)
        if claimed:
            location["current_city"] = claimed
        return {"location_context": location}

    def route(state: TurnState):
        history = state["history"][-10:]
        user_text = history[-1]["content"]
        previous = history[-2]["content"] if len(history) > 1 else ""
        prior_context = state.get("context", {})
        location = state.get("location_context") or {}
        base = local_today()
        writes = requested_actions(user_text)
        direct_intent, direct_params, direct_context = read_request(
            user_text, "general", {}, prior_context, base)
        if (not writes and re.search(r"(?:取消|改签|改期).{0,18}(?:多少|几成|几元|费用|收费)",
                                     user_text)):
            scope = ("flight" if re.search(r"机票|航班|班机", user_text) else
                     "hotel" if re.search(r"酒店|房间|住宿", user_text) else
                     "rental" if re.search(r"租车|车辆", user_text) else None)
            if scope:
                return {"intent": "policy", "params": {"scope": scope}, "draft": ""}
        landing = bool(re.search(r"落地|到达后|目的地", user_text))
        nearby = "附近" in user_text or "周边" in user_text or landing
        lodging = bool(re.search(r"酒店|住宿|住哪|订房", user_text))
        sight = bool(re.search(r"景点|经典地点|游览|好玩的地方", user_text))
        if nearby and (lodging or sight) and not writes:
            explicit = mentioned_cities(user_text)
            current, destination = location.get("current_city"), location.get("destination_city")
            if landing:
                flight_candidates = (state.get("candidates") or {}).get("flight", [])
                if not flight_candidates:
                    flight_candidates = read_evidence("landing_context", {})["references"]
                selected = _chosen_landing(flight_candidates, user_text)
                if not selected and not re.search(r"SIM-\d+|第\s*[一二三123]\s*(?:个|班|趟)?", user_text, re.I):
                    hotel_task = (state.get("task_contexts") or {}).get("hotel_search") or {}
                    landing_id = hotel_task.get("_landing_flight_id")
                    selected = next((item for item in flight_candidates
                                     if item.get("id") == landing_id), None)
                if not selected:
                    return {"intent": "general", "params": {}, "draft":
                            "请先从已查到的航班中明确选择班次，我才能核对落地机场和当地到达日。"}
                if lodging:
                    hotel = {"intent": "hotel_search", "city": selected["arrival_city"],
                             "airport": selected["arrival_airport"],
                             "_landing_flight_id": selected["id"],
                             "suggested_check_in": selected["arrival_date"],
                             "sort": "distance"}
                    dates = mentioned_dates(user_text, base)
                    same_day = bool(re.search(r"(?:落地|到达)(?:当|那)天", user_text))
                    if same_day and dates and dates[0].isoformat() != selected["arrival_date"]:
                        return {"intent": "general", "params": {}, "draft":
                                "你说的落地当天与已核对航班的当地到达日不一致，请确认入住日期。"}
                    if dates or same_day and re.search(r"入住|住", user_text):
                        hotel["check_in"] = (dates[0].isoformat() if dates
                                             else selected["arrival_date"])
                    nights = stay_nights(user_text, dates)
                    if nights and hotel.get("check_in"):
                        hotel["nights"] = nights
                        hotel["check_out"] = (date.fromisoformat(hotel["check_in"])
                                              + timedelta(days=nights)).isoformat()
                    tasks = dict(state.get("task_contexts") or {})
                    tasks["hotel_search"] = hotel
                    return {"intent": "hotel_search", "params": hotel, "context": hotel,
                            "task_contexts": tasks, "draft": ""}
                city = selected["arrival_city"]
            elif explicit:
                city = explicit[-1]
            elif current and destination and current != destination:
                return {"intent": "general", "params": {}, "draft":
                        f"你是想了解现在所在的 {city_zh(current)}，"
                        f"还是航班目的地 {city_zh(destination)}？请选一处。"}
            else:
                city = current
            if not city:
                return {"intent": "general", "params": {}, "draft":
                        "请告诉我你现在所在的城市，或说明要查哪座目的地城市。"}
            if sight:
                return {"intent": "recommendation", "params": {},
                        "nearby_city": city, "draft": ""}
            hotel = {"intent": "hotel_search", "city": city}
            for key in ("check_in", "check_out", "nights"):
                if key in direct_params:
                    hotel[key] = direct_params[key]
            tasks = dict(state.get("task_contexts") or {})
            tasks["hotel_search"] = hotel
            return {"intent": "hotel_search", "params": hotel,
                    "context": hotel, "task_contexts": tasks, "draft": ""}
        hotel_task = (state.get("task_contexts") or {}).get("hotel_search") or {}
        if (hotel_task.get("_landing_flight_id") and
                re.search(r"(?:到达|落地)(?:当|那)天.{0,6}(?:入住|住)|"
                          r"(?:入住|住).{0,6}(?:到达|落地)(?:当|那)天", user_text)
                and not writes):
            latest_flights = (state.get("candidates") or {}).get("flight", [])
            if latest_flights and not any(
                    item.get("id") == hotel_task["_landing_flight_id"]
                    for item in latest_flights):
                return {"intent": "general", "params": {}, "draft":
                        "刚查询的航班已变化；请重新选择班次，再确认落地酒店日期。"}
            hotel = dict(hotel_task)
            hotel["check_in"] = hotel["suggested_check_in"]
            nights = stay_nights(user_text, mentioned_dates(user_text, base))
            if nights:
                hotel["nights"] = nights
                hotel["check_out"] = (date.fromisoformat(hotel["check_in"]) +
                                      timedelta(days=nights)).isoformat()
            tasks = dict(state.get("task_contexts") or {})
            tasks["hotel_search"] = hotel
            return {"intent": "hotel_search", "params": hotel, "context": hotel,
                    "task_contexts": tasks, "draft": ""}
        if direct_intent == "flight_search" and direct_params.get("sort") == "earliest":
            tasks = dict(state.get("task_contexts") or {})
            tasks["flight_search"] = direct_context
            book_earliest = bool(re.search(
                r"预订|预定|下单|订票|订.{0,8}最早|买票|买.{0,8}机票", user_text))
            if book_earliest:
                return {"intent": "transaction", "params": {
                    "actions": [{"action_type": "flight_book", "parameters": {}}],
                    "queries": [{"intent": "flight_search", "params": direct_params}]},
                    "context": direct_context, "task_contexts": tasks, "draft": ""}
            return {"intent": "flight_search", "params": direct_params,
                    "context": direct_context, "task_contexts": tasks, "draft": ""}
        rank_request = bool(re.search(r"最贵|贵的那个|价格最高|最便宜|便宜的那个|价格最低", user_text))
        if rank_request and direct_intent in ("hotel_search", "rental_search"):
            tasks = dict(state.get("task_contexts") or {})
            tasks[direct_intent] = direct_context
            book = bool(re.search(
                r"预订|预定|下单|订房|订车|订.{0,8}(?:最贵|最便宜)|我要住|我想住|租.{0,8}车",
                user_text))
            if book:
                kind = direct_intent.split("_")[0]
                return {"intent": "transaction", "params": {
                    "actions": [{"action_type": f"{kind}_book", "parameters": {}}],
                    "queries": [{"intent": direct_intent, "params": direct_params}]},
                    "context": direct_context, "task_contexts": tasks, "draft": ""}
            return {"intent": direct_intent, "params": direct_params,
                    "context": direct_context, "task_contexts": tasks, "draft": ""}
        if (not writes and direct_intent == "rental_search"
                and re.search(r"\d{1,2}(?::\d{2}|点)", user_text)
                and re.search(r"取|还", user_text)):
            tasks = dict(state.get("task_contexts") or {})
            tasks["rental_search"] = direct_context
            return {"intent": "rental_search", "params": direct_params,
                    "context": direct_context, "task_contexts": tasks, "draft": ""}
        if not writes and direct_intent in ("weather", "capabilities", "hotel_coverage", "multi_read"):
            return {"intent": direct_intent, "params": direct_params,
                    "context": direct_context, "draft": ""}
        if (re.search(r"(?:现在|还有|最近|近期|接下来|未来).{0,12}(?:行程|安排|机票|航班|出发)|"
                      r"(?:行程|机票|航班).{0,12}(?:还有|要出发|安排)", user_text)
                and not writes):
            return {"intent": "itinerary", "params": {},
                    "context": prior_context, "draft": ""}
        if re.search(r"所有订单|历史订单|订单历史|行程历史", user_text) and not writes:
            return {"intent": "itinerary", "params": {},
                    "context": prior_context, "draft": ""}
        if re.search(r"什么模型|模型内核|底层模型|使用.{0,5}模型", user_text):
            return {"intent": "general", "params": {"mode": "model_info"}, "draft": ""}
        if re.search(r"你听不懂|没听懂|为什么不行|为什么不能|刚才为什么", user_text):
            if "已生成" in previous and "待确认卡" in previous:
                clarification = ("上一轮只生成了待确认卡，订单尚未改变。"
                                 "请在页面查看每张卡的当前状态，分别确认或拒绝。")
            elif "已过起飞时间" in previous:
                clarification = "上一轮的机票已过起飞时间，服务端拒绝取消或改签；订单没有改变。"
            elif "已过取消截止" in previous or "入住日期已到" in previous:
                clarification = "上一轮的酒店已过取消截止日期，服务端拒绝取消；订单没有改变。"
            elif "改签还需要选择" in previous:
                clarification = "上一轮已找到你的原机票；还缺同航线的新班次。你可以先给新日期，我再列候选。"
            elif "酒店还需要明确" in previous:
                clarification = "上一轮还缺入住、退房日期及酒店房型；补齐后我才能生成待确认卡。"
            elif previous:
                clarification = "请看上一轮的具体结果；你可以告诉我想核对的订单或缺少的条件。"
            else:
                clarification = "请指出你想了解的订单或时间，我会核对后解释。"
            return {"intent": "general", "params": {}, "draft":
                    "抱歉，刚才没有解释清楚。" + clarification}
        if re.search(r"起飞时间|航班时间|退房时间|入住时间", user_text) or (
                "时间" in user_text and "我的行程" in previous):
            return {"intent": "itinerary", "params": {}, "draft": ""}
        if re.search(r"(?:中国|日本|美国|法国|英国|瑞士|韩国|加拿大|意大利|阿联酋).{0,15}(?:旅游|旅行|推荐|去哪)", user_text):
            return {"intent": "recommendation", "params": direct_params,
                    "context": direct_context, "draft": ""}
        boundary = boundary_intent(user_text)
        if boundary == "external_policy":
            return {"intent": "general", "params": {}, "draft":
                    "目前无法核实这家航空公司的实际退改规则，请以航空公司官方渠道为准。"
                    "我可以查询本平台的虚构规则。"}
        if boundary == "ambiguous_policy":
            return {"intent": "general", "params": {}, "draft":
                    "你要查询本平台的虚构规则，还是某家真实航司的规则？"
                    "如需真实规则，请说明航司并以其官方渠道为准。"}
        if boundary in ("withdraw", "approve"):
            return {"intent": "action_guidance", "params": {"mode": boundary}, "draft": ""}
        safe_history = "\n".join(f"{row['role']}: {str(row['content'])[:1000]}"
                                 for row in history)
        prompt = ROUTER_PROMPT.format(demo_date=state["demo_date"],
                                      current_date=base.isoformat(), history=safe_history)
        reply = model.invoke([SystemMessage(content=prompt),
                              HumanMessage(content="请按 JSON 格式选择本轮处理路径。")])
        try:
            decision = json.loads(reply.content)
        except (TypeError, ValueError):
            decision = {}
        if not isinstance(decision, dict):
            decision = {}
        intent = decision.get("intent")
        params = decision.get("params")
        draft = decision.get("answer")
        if intent not in INTENTS:
            intent = "general"
            draft = "我暂时无法确认你的需求。请换一种说法，或明确查询的业务类型。"
        if not isinstance(params, dict):
            params = {}
        if intent == "transaction" and re.search(r"景点|游览|门票", user_text) and re.search(
                r"预订|订|购买|购票|买|预约|改期|取消", user_text):
            return {"intent": "recommendation", "params": {}, "draft": ""}
        if intent == "transaction" and not writes:
            if direct_intent in ("flight_search", "hotel_search", "recommendation"):
                return {"intent": direct_intent, "params": direct_params,
                        "context": direct_context, "draft": ""}
            return {"intent": "general", "params": {}, "draft":
                    "请说明本轮想查询什么；我不会根据上一轮的话自动重试订单操作。"}
        # Keep the evidence boundary independent of the model's JSON decision.
        if boundary_intent(user_text) in ("external_policy", "ambiguous_policy") and intent == "policy":
            return {"intent": "general", "params": {}, "draft":
                    "目前无法核实这家航空公司的实际规则，请以航空公司官方渠道为准。"}
        place_request = bool(re.search(r"景点|经典地点|游览|旅行推荐|旅游推荐|去哪玩|好玩的地方", user_text))
        excursion_action = bool(re.search(r"景点|游览|门票", user_text)
                                and re.search(r"预订|订|购买|改期|取消", user_text))
        business_write = bool(writes)
        if place_request and (excursion_action or not business_write):
            intent, params, draft = "recommendation", params, ""
        if intent == "general":
            if re.search(r"预订|改签|取消|退款|下单|订票|订房|景点|推荐|政策|订单|航班|酒店|租车", user_text):
                draft = "请说明你要查询行程、平台政策、航班酒店租车候选，还是希望办理订单变更。"
        if writes and not excursion_action:
            suggested = params.get("actions", []) if intent == "transaction" else []
            matched = {item.get("action_type"): item for item in suggested
                       if isinstance(item, dict) and isinstance(item.get("parameters"), dict)}
            queries = params.get("queries", []) if intent == "transaction" else []
            if not isinstance(queries, list):
                queries = []
            if ("flight_book" in writes
                    and re.search(r"(?:顺便|再|同时).{0,8}(?:找|查|看看).{0,18}酒店", user_text)
                    and not any(isinstance(query, dict) and query.get("intent") == "hotel_search"
                                for query in queries)):
                cities = mentioned_cities(user_text)
                queries.append({"intent": "hotel_search", "params":
                                {"city": cities[-1]} if cities else {}})
            params = {"actions": [matched.get(kind, {"action_type": kind, "parameters": {}})
                                  for kind in writes[:2]], "queries": queries[:2]}
            intent = "transaction"
        if intent != "transaction":
            intent, params, context = read_request(user_text, intent, params,
                                                   prior_context, base)
        else:
            context = (direct_context if direct_intent in
                       ("hotel_search", "rental_search", "flight_search") else prior_context)
        tasks = dict(state.get("task_contexts") or {})
        if intent in ("flight_search", "hotel_search", "rental_search"):
            tasks[intent] = context
        return {"intent": intent, "params": params, "context": context,
                "task_contexts": tasks,
                "draft": draft or ""}

    def general(state: TurnState):
        if state.get("params", {}).get("mode") == "model_info":
            provider = {"deepseek": "DeepSeek", "gpt": "GPT", "local": "本地模型"}.get(
                settings.AI_PROVIDER.lower().strip(), "已配置模型")
            model_name = settings.AI_MODEL.strip()
            detail = f"（模型标识：{model_name}）" if re.fullmatch(
                r"[A-Za-z0-9._/-]{1,80}", model_name) else ""
            return {"answer": f"当前旅行助手使用 {provider}{detail}。", "references": []}
        return {"answer": _safe_general(state.get("draft")), "references": []}

    def lookup(intent):
        def node(state: TurnState):
            params = state.get("params", {})
            if intent == "itinerary":
                params = {**params, "_user_text": state["history"][-1]["content"]}
            result = read_evidence(intent, params)
            if intent in ("flight_search", "hotel_search", "rental_search"):
                groups = dict(state.get("candidates") or {})
                groups[intent.split("_")[0]] = _scoped_candidates(
                    intent, result.get("references", []), params,
                    earliest_id=result.get("earliest_id"),
                    extreme_id=result.get("extreme_unique_id"),
                    flight_details=result.get("flight_details", ()))
                if intent == "flight_search":
                    location = dict(state.get("location_context") or {})
                    tasks = dict(state.get("task_contexts") or {})
                    if (tasks.get("hotel_search") or {}).get("_landing_flight_id"):
                        tasks.pop("hotel_search", None)
                    selected = _chosen_landing(groups["flight"], "")
                    if selected:
                        location.update(destination_city=selected.get("arrival_city"),
                                        arrival_airport=selected.get("arrival_airport"),
                                        arrival_date=selected.get("arrival_date"))
                    elif groups["flight"]:
                        destinations = {item.get("arrival_city") for item in groups["flight"]}
                        location["destination_city"] = (destinations.pop()
                                                        if len(destinations) == 1 else None)
                        location.pop("arrival_airport", None)
                        location.pop("arrival_date", None)
                    else:
                        for key in ("destination_city", "arrival_airport", "arrival_date"):
                            location.pop(key, None)
                    return {**result, "candidates": groups,
                            "location_context": location,
                            "task_contexts": tasks}
                return {**result, "candidates": groups}
            return result
        return node

    def transaction(state: TurnState):
        params = state.get("params", {})
        plans = params.get("actions", [])
        queries = params.get("queries", [])
        if not isinstance(plans, list):
            plans = []
        if not isinstance(queries, list):
            queries = []
        answers, references = [], []
        groups = dict(state.get("candidates") or {})
        tasks = dict(state.get("task_contexts") or {})
        for query in queries[:2]:
            if not isinstance(query, dict) or query.get("intent") not in (
                    "itinerary", "flight_search", "hotel_search", "rental_search"):
                continue
            query_params = query.get("params")
            query_params = dict(query_params) if isinstance(query_params, dict) else {}
            query_params.pop("_landing_flight_id", None)
            query_params.pop("suggested_check_in", None)
            if (query["intent"] == "hotel_search" and
                    re.search(r"落地|到达后", state["history"][-1]["content"])):
                selected = _chosen_landing(groups.get("flight", []),
                                           state["history"][-1]["content"])
                if not selected:
                    answers.append("请先明确要乘坐的航班，我才能核对落地机场及当地到达日，再找周边酒店。")
                    continue
                query_params = {"city": selected["arrival_city"],
                                "airport": selected["arrival_airport"],
                                "_landing_flight_id": selected["id"],
                                "sort": "distance"}
            elif query["intent"] == "hotel_search" and not query_params.get("city"):
                flights = groups.get("flight", [])
                proposed = next((item.get("parameters", {}).get("flight_id") for item in plans
                                 if isinstance(item, dict) and item.get("action_type") == "flight_book"), None)
                chosen = [item for item in flights if item.get("type") == "flight"
                          and (proposed is None or item.get("id") == proposed)]
                if len(chosen) == 1:
                    destination = chosen[0].get("arrival_airport") or chosen[0].get("arrival")
                    if destination:
                        query_params["airport" if len(destination) == 3 else "city"] = destination
            result = read_evidence(query["intent"],
                                   query_params)
            answers.append(result["answer"])
            references.extend(result["references"])
            groups[query["intent"].split("_")[0]] = _scoped_candidates(
                query["intent"], result["references"], query_params,
                earliest_id=result.get("earliest_id"),
                extreme_id=result.get("extreme_unique_id"),
                flight_details=result.get("flight_details", ()))
            if query["intent"] == "hotel_search" and query_params.get("_landing_flight_id"):
                tasks["hotel_search"] = {"intent": "hotel_search", **query_params,
                                          "suggested_check_in": selected["arrival_date"]}
            if (query["intent"] == "flight_search" and query_params.get("sort") == "earliest"
                    and any(isinstance(item, dict) and item.get("action_type") == "flight_book"
                            for item in plans) and result.get("earliest_id") is None):
                plans = [item for item in plans if not isinstance(item, dict)
                         or item.get("action_type") != "flight_book"]
            if (query["intent"] in ("hotel_search", "rental_search")
                    and query_params.get("rank") in ("max", "min")
                    and result.get("extreme_unique_id") is None):
                kind = query["intent"].split("_")[0]
                plans = [item for item in plans if not isinstance(item, dict)
                         or item.get("action_type") != f"{kind}_book"]
        if not answers:
            answers.append("请说明要办理的订单或具体候选，以及日期和时间；有多个候选时请指定其中一个。")
        if len(plans) > 2:
            answers.append("本轮最多处理两笔；其余需求请在下一轮提出。")
        return {"answer": "\n".join(answers), "references": references[:15],
                "candidates": groups,
                "transaction_plan": plans[:2],
                "task_contexts": tasks}

    def recommendation(state: TurnState):
        params = dict(state.get("params", {}))
        params.pop("_resolved_city", None)
        text = state["history"][-1]["content"]
        if ("附近" in text or "周边" in text) and state.get("nearby_city"):
            params["_resolved_city"] = state["nearby_city"]
        return read_evidence("recommendation", {**params, "user_text": text})

    def multi_read(state: TurnState):
        params = state.get("params", {})
        flight = read_evidence("flight_search", params.get("flight", {}))
        hotel = read_evidence("hotel_search", params.get("hotel", {}))
        return {"answer": f"航班：{flight['answer']}\n酒店：{hotel['answer']}",
                "references": (flight["references"] + hotel["references"])[:15],
                "candidates": {**(state.get("candidates") or {}),
                               "flight": _scoped_candidates("flight_search", flight["references"],
                                                            params.get("flight", {}),
                                                            flight_details=flight.get("flight_details", ())),
                               "hotel": _scoped_candidates("hotel_search", hotel["references"],
                                                           params.get("hotel", {}))}}

    builder = StateGraph(TurnState)
    builder.add_node("locate", locate)
    builder.add_node("route", route)
    builder.add_node("general", general)
    builder.add_node("transaction", transaction)
    builder.add_node("recommendation", recommendation)
    builder.add_node("multi_read", multi_read)
    builder.add_node("action_guidance", lookup("action_guidance"))
    for intent in ("itinerary", "policy", "flight_search", "hotel_search", "rental_search",
                   "weather", "capabilities", "hotel_coverage"):
        builder.add_node(intent, lookup(intent))
    builder.add_edge(START, "locate")
    builder.add_edge("locate", "route")
    builder.add_conditional_edges("route", lambda state: state["intent"],
                                  {intent: intent for intent in INTENTS})
    for intent in INTENTS:
        builder.add_edge(intent, END)
    return builder.compile(checkpointer=checkpointer)
