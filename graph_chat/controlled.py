"""New read-only LangGraph, isolated from the legacy SQLite workflow."""

import json
import re
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from services.dialogue_boundaries import boundary_intent


INTENTS = ("general", "itinerary", "policy", "flight_search", "hotel_search",
           "rental_search", "transaction", "recommendation", "action_guidance")
ROUTER_PROMPT = """你是虚构旅行平台的主助手。只输出一个 JSON 对象，字段为 intent、params、answer。
intent 只能是 general、itinerary、policy、flight_search、hotel_search、rental_search、transaction、recommendation。
航班/酒店/租车预订、改期、取消属于 transaction；景点或旅行地点推荐属于 recommendation。
政策查询用 policy，params.scope 只能是 flight/hotel/rental。本人订单用 itinerary。
搜索参数：flight_search 用 departure/arrival 机场代码、travel_date YYYY-MM-DD、可选 sort=price；
hotel_search 用 city/check_in/check_out；rental_search 用 city 或 airport、pickup_date/drop_off_date。
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
演示基准日：{demo_date}。旅客与助手最近消息：\n{history}"""


class TurnState(TypedDict, total=False):
    history: list[dict]
    demo_date: str
    intent: str
    params: dict
    draft: str
    answer: str
    references: list[dict]
    transaction_plan: list[dict]


def _safe_general(draft):
    if (not isinstance(draft, str) or not 1 <= len(draft.strip()) <= 500
            or re.search(r"\d|已预订|已改签|已取消|付款成功|退款成功|票价|航司政策|开放时间", draft)):
        return "我可以帮你查询本人模拟行程、航班、酒店、租车和虚构平台政策。请告诉我具体需求。"
    return draft.strip()


def build_graph(model, read_evidence, checkpointer):
    def route(state: TurnState):
        history = state["history"][-10:]
        user_text = history[-1]["content"]
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
        prompt = ROUTER_PROMPT.format(demo_date=state["demo_date"], history=safe_history)
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
        # Keep the evidence boundary independent of the model's JSON decision.
        if boundary_intent(user_text) in ("external_policy", "ambiguous_policy") and intent == "policy":
            return {"intent": "general", "params": {}, "draft":
                    "目前无法核实这家航空公司的实际规则，请以航空公司官方渠道为准。"}
        place_request = bool(re.search(r"景点|经典地点|游览|旅行推荐|旅游推荐|去哪玩|好玩的地方", user_text))
        excursion_action = bool(re.search(r"景点|游览|门票", user_text)
                                and re.search(r"预订|订|购买|改期|取消", user_text))
        business_write = bool(re.search(r"机票|航班|酒店|租车", user_text)
                              and re.search(r"预订|订房|订票|改签|改期|取消|租车", user_text))
        if place_request and (excursion_action or not business_write):
            intent, params, draft = "recommendation", params, ""
        if intent == "general":
            if re.search(r"预订|改签|取消|退款|下单|订票|订房|景点|推荐|政策|订单|航班|酒店|租车", user_text):
                draft = "请说明你要查询本人行程、虚构政策、模拟候选，还是希望办理交易。"
        return {"intent": intent, "params": params, "draft": draft or ""}

    def general(state: TurnState):
        return {"answer": _safe_general(state.get("draft")), "references": []}

    def lookup(intent):
        def node(state: TurnState):
            return read_evidence(intent, state.get("params", {}))
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
        for query in queries[:2]:
            if not isinstance(query, dict) or query.get("intent") not in (
                    "itinerary", "flight_search", "hotel_search", "rental_search"):
                continue
            query_params = query.get("params")
            result = read_evidence(query["intent"],
                                   query_params if isinstance(query_params, dict) else {})
            answers.append(result["answer"])
            references.extend(result["references"])
        if not answers:
            answers.append("请说明要办理的订单或具体候选，以及日期和时间；有多个候选时请指定其中一个。")
        if len(plans) > 2:
            answers.append("本轮最多处理两笔；其余需求请在下一轮提出。")
        return {"answer": "\n".join(answers), "references": references[:15],
                "transaction_plan": plans[:2]}

    def recommendation(state: TurnState):
        params = state.get("params", {})
        return read_evidence("recommendation", {**params,
                "user_text": state["history"][-1]["content"]})

    builder = StateGraph(TurnState)
    builder.add_node("route", route)
    builder.add_node("general", general)
    builder.add_node("transaction", transaction)
    builder.add_node("recommendation", recommendation)
    builder.add_node("action_guidance", lookup("action_guidance"))
    for intent in ("itinerary", "policy", "flight_search", "hotel_search", "rental_search"):
        builder.add_node(intent, lookup(intent))
    builder.add_edge(START, "route")
    builder.add_conditional_edges("route", lambda state: state["intent"],
                                  {intent: intent for intent in INTENTS})
    for intent in INTENTS:
        builder.add_edge(intent, END)
    return builder.compile(checkpointer=checkpointer)
