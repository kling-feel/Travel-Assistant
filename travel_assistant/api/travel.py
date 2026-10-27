"""P1 traveler API. Client input never selects a traveler account."""

import asyncio
import json
import logging
import re
from threading import BoundedSemaphore
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from travel_assistant.core.config import settings
from travel_assistant.db.travel_models import airports, travelers
from travel_assistant.services.location_labels import airport_zh, city_zh, product_zh, amount_zh
from travel_assistant.services.travel import (OrderError, find_flights, find_hotels, find_rentals,
                             itinerary, policies)
from travel_assistant.services.sessions import (SessionNotFound, append_message,
                               create_session as create_chat_session,
                               get_session_history, latest_session_references,
                               list_sessions, _owned_session)
from travel_assistant.services.actions import (ActionError, ActionNotFound, create_proposal,
                              get_proposal, list_proposals)
from travel_assistant.services.confirmations import confirm_action, reject_action, list_action_history
from travel_assistant.services.agent_reads import demo_date, read_action_guidance, read_for_assistant
from travel_assistant.services.dialogue_context import (is_read_only_action_request,
                                                        local_today, requested_actions)
from travel_assistant.services.chat_turns import (TurnConflict, _public as public_turn, begin_turn,
                                 fail_turn, finish_turn, get_turn, latest_turn, has_graph_turns,
                                 lock_processing_turn)
from travel_assistant.services.agent_proposals import SelectionNeeded, prepare_actions, verify_shown_quotes
from travel_assistant.services.agent_feedback import (bulk_cancellation_requested, cancellation_overview, explain_action_error,
                                     explain_selection_needed)
from travel_assistant.services.checkpoints import open_checkpointer
from travel_assistant.services.model_config import ModelUnavailable, get_chat_model
from travel_assistant.services.recommendations import (get_recommendation_provider,
                                      read_recommendation)
from travel_assistant.agents.controlled import build_graph
from travel_assistant.core.dependencies import get_db
from travel_assistant.core.password_hash import get_hashed_password, verify_password
from travel_assistant.core.request_limits import cloud_limits
from travel_assistant.services.provider_usage import (
    ProviderBalanceError, current_traveler as usage_traveler, summary as usage_summary,
)


router = APIRouter()
bearer = HTTPBearer()
logger = logging.getLogger(__name__)
turn_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="travel-turn")
turn_slots = BoundedSemaphore(16)
_DUMMY_HASH = get_hashed_password("no-such-traveler")


class _MemoryOnlyModel:
    """The memory route can answer from the checkpoint without an API call."""

    def invoke(self, _messages):
        raise ModelUnavailable("model unavailable for this request")


def _card_brief(card):
    """A short, database-backed summary; the review card holds full policy details."""
    after = card.get("after") or {}
    kind = card["action_type"].split("_", 1)[0]
    if kind == "flight":
        departure_local = after.get("departure_local")
        try:
            when = datetime.fromisoformat(departure_local).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            when = "待核对"
        route = (f"{airport_zh(after.get('departure_airport', ''), '')} → "
                 f"{airport_zh(after.get('arrival_airport', ''), '')}")
        target = f"{route}，航班 {after.get('flight_no', '—')}，当地出发 {when}"
    elif kind == "hotel":
        target = (f"{product_zh(after.get('hotel_name', ''), city=after.get('hotel_city', ''))}，"
                  f"{after.get('check_in', '—')} 入住至 {after.get('check_out', '—')} 退房")
    else:
        target = (f"{product_zh(after.get('product_name', ''), airport=after.get('airport_code', ''))}，"
                  f"{after.get('pickup_local', '—')} 取车至 {after.get('dropoff_local', '—')} 还车")
    amount = (card.get("price") or {}).get("new_amount")
    price = f"，参考总价 {amount_zh(amount)}" if card["action_type"].endswith("_book") and isinstance(amount, int) else ""
    return target + price


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=4000)


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=4000)
    request_key: str = Field(min_length=8, max_length=80,
                             pattern=r"^[A-Za-z0-9._-]+$")


class EmptyActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidateProposalRequest(BaseModel):
    """A visible search choice; the server looks up every business fact again."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["flight", "hotel", "rental"]
    candidate_id: int | str
    unit_price: int = Field(ge=0)
    offer_version: int | None = Field(default=None, ge=1)
    departure: str | None = Field(default=None, min_length=3, max_length=8)
    arrival: str | None = Field(default=None, min_length=3, max_length=8)
    travel_date: date | None = None
    city: str | None = Field(default=None, min_length=1, max_length=80)
    airport: str | None = Field(default=None, min_length=3, max_length=8)
    check_in: date | None = None
    check_out: date | None = None
    pickup_date: date | None = None
    drop_off_date: date | None = None
    pickup_clock: time | None = None
    dropoff_clock: time | None = None


def current_traveler(credentials: HTTPAuthorizationCredentials = Depends(bearer),
                     session: Session = Depends(get_db)):
    if not settings.JWT_SECRET_KEY or settings.JWT_SECRET_KEY.startswith("replace-with"):
        raise HTTPException(503, "JWT secret is not configured")
    try:
        claims = jwt.decode(credentials.credentials, settings.JWT_SECRET_KEY,
                            algorithms=[settings.ALGORITHM])
        if claims.get("type") != "traveler_access":
            raise JWTError("wrong token type")
        traveler_id = claims["sub"]
    except (JWTError, KeyError):
        raise HTTPException(401, "invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"})
    traveler = session.execute(select(travelers).where(
        travelers.c.id == traveler_id, travelers.c.active == 1
    )).mappings().one_or_none()
    if traveler is None:
        raise HTTPException(401, "invalid or inactive traveler")
    return traveler


@router.post("/login")
def login(data: LoginRequest, session: Session = Depends(get_db)):
    if not settings.JWT_SECRET_KEY or settings.JWT_SECRET_KEY.startswith("replace-with"):
        raise HTTPException(503, "JWT secret is not configured")
    if settings.CLOUD_MODE and not cloud_limits.allow_login(data.username):
        raise HTTPException(429, "too many login attempts", headers={"Retry-After": "60"})
    traveler = session.execute(select(travelers).where(
        travelers.c.username == data.username, travelers.c.active == 1
    )).mappings().one_or_none()
    valid = verify_password(data.password,
                            traveler["password_hash"] if traveler else _DUMMY_HASH)
    if settings.CLOUD_MODE:
        cloud_limits.login_result(data.username, bool(traveler and valid))
    if traveler is None or not valid:
        if settings.CLOUD_MODE:
            logger.warning("cloud_login_failed known_account=%s", traveler is not None)
        raise HTTPException(401, "invalid credentials")
    if settings.CLOUD_MODE:
        logger.info("cloud_login_succeeded traveler_id=%s", traveler["id"])
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    token = jwt.encode({"sub": traveler["id"], "type": "traveler_access",
                        "exp": expires}, settings.JWT_SECRET_KEY,
                       algorithm=settings.ALGORITHM)
    return {"access_token": token, "token_type": "bearer", "expires_at": expires}


@router.get("/me")
def me(traveler=Depends(current_traveler)):
    return {"id": traveler["id"], "username": traveler["username"],
            "display_name": traveler["display_name"]}


@router.get("/me/provider-usage")
def my_provider_usage(days: int = Query(default=7, ge=1, le=31),
                      traveler=Depends(current_traveler),
                      session: Session = Depends(get_db)):
    if not settings.CLOUD_MODE:
        raise HTTPException(404, "usage report is available in cloud mode")
    return usage_summary(session, traveler["id"], days)


@router.get("/demo-state")
def current_demo_state(traveler=Depends(current_traveler),
                       session: Session = Depends(get_db)):
    as_of = demo_date(session)
    return {"as_of": as_of, "bookable_from": as_of + timedelta(days=1),
            "bookable_until": as_of + timedelta(days=30),
            "hotel_checkout_until": as_of + timedelta(days=31),
            "display_currency": "CNY",
            "simulation_notice": "虚构平台与模拟数据；不连接真实供应商或付款"}


@router.get("/locations")
def location_options(traveler=Depends(current_traveler),
                     session: Session = Depends(get_db)):
    """Authenticated display choices; codes and city values stay canonical."""
    rows = session.execute(select(airports.c.code, airports.c.name,
                                  airports.c.city, airports.c.timezone)).mappings()
    result = [{"code": row["code"], "city": row["city"],
               "city_zh": city_zh(row["city"]),
               "name": row["name"],
               "name_zh": airport_zh(row["code"], row["city"]),
               "timezone": row["timezone"]} for row in rows]
    return sorted(result, key=lambda item: (item["city_zh"], item["code"]))


@router.get("/me/itinerary")
def my_itinerary(traveler=Depends(current_traveler),
                 session: Session = Depends(get_db)):
    return itinerary(session, traveler["id"])


@router.get("/flights")
def flights_search(departure: str = Query(min_length=3, max_length=8),
                   arrival: str = Query(min_length=3, max_length=8),
                   travel_date: date = Query(),
                   limit: int = Query(default=20, ge=1, le=100),
                   offset: int = Query(default=0, ge=0, le=10000),
                   sort: str = Query(default="departure", pattern="^(departure|price)$"),
                   traveler=Depends(current_traveler),
                   session: Session = Depends(get_db)):
    return find_flights(session, departure.upper(), arrival.upper(), travel_date,
                        limit=limit, offset=offset, sort=sort)


@router.get("/hotels")
def hotels_search(city: str = Query(min_length=1, max_length=80),
                  check_in: date = Query(), check_out: date = Query(),
                  airport: str | None = Query(default=None, min_length=3, max_length=8),
                  limit: int = Query(default=20, ge=1, le=100),
                  offset: int = Query(default=0, ge=0, le=10000),
                  sort: str = Query(default="price", pattern="^(price|price_desc|distance)$"),
                  traveler=Depends(current_traveler),
                  session: Session = Depends(get_db)):
    try:
        return find_hotels(session, city, check_in, check_out,
                           airport=airport.upper() if airport else None,
                           limit=limit, offset=offset, sort=sort)
    except OrderError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/rentals")
def rentals_search(pickup_date: date, drop_off_date: date,
                   airport: str | None = Query(default=None, min_length=3, max_length=8),
                   city: str | None = Query(default=None, min_length=1, max_length=80),
                   max_daily_price: int | None = Query(default=None, ge=0),
                   limit: int = Query(default=20, ge=1, le=100),
                   offset: int = Query(default=0, ge=0, le=10000),
                   sort: str = Query(default="price", pattern="^(price|price_desc|distance)$"),
                   traveler=Depends(current_traveler),
                   session: Session = Depends(get_db)):
    try:
        return find_rentals(session, pickup_date, drop_off_date,
                            airport=airport.upper() if airport else None, city=city,
                            max_daily_price=max_daily_price, limit=limit,
                            offset=offset, sort=sort)
    except OrderError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/policies/{scope}")
def policy_search(scope: str, traveler=Depends(current_traveler),
                  session: Session = Depends(get_db)):
    if scope not in ("flight", "hotel", "rental"):
        raise HTTPException(404, "policy scope not found")
    try:
        return policies(session, scope)
    except OrderError as error:
        raise HTTPException(503, str(error)) from error


@router.post("/sessions", status_code=201)
def start_session(data: SessionCreate, traveler=Depends(current_traveler),
                  session: Session = Depends(get_db)):
    created = create_chat_session(session, traveler["id"])
    session.commit()
    return created


@router.get("/sessions")
def my_sessions(traveler=Depends(current_traveler),
                session: Session = Depends(get_db)):
    return list_sessions(session, traveler["id"])


@router.get("/sessions/{session_id}")
def session_history(session_id: str, traveler=Depends(current_traveler),
                    session: Session = Depends(get_db)):
    try:
        return get_session_history(session, traveler["id"], session_id)
    except SessionNotFound as error:
        raise HTTPException(404, str(error)) from error


@router.post("/sessions/{session_id}/candidate-proposals", status_code=201)
def propose_visible_candidate(session_id: str, data: CandidateProposalRequest,
                              traveler=Depends(current_traveler),
                              session: Session = Depends(get_db)):
    """Selecting a candidate makes a review card, never an order."""
    try:
        _owned_session(session, traveler["id"], session_id)
        if isinstance(data.candidate_id, str) and len(data.candidate_id) > 80:
            raise ValueError("candidate identifier is invalid")
        if data.kind == "flight":
            if (type(data.candidate_id) is not int or not data.departure or
                    not data.arrival or not data.travel_date or data.offer_version is None):
                raise ValueError("flight selection is incomplete")
            rows = find_flights(session, data.departure.upper(), data.arrival.upper(),
                                data.travel_date, limit=100)
            row = next((item for item in rows if item["id"] == data.candidate_id), None)
            if (row is None or row["demo_fare_amount"] != data.unit_price or
                    row["offer_version"] != data.offer_version):
                raise ValueError("flight candidate or quote changed; search again")
            action_type, parameters = "flight_book", {"flight_id": row["id"]}
        elif data.kind == "hotel":
            if (not isinstance(data.candidate_id, str) or not data.city or
                    not data.check_in or not data.check_out):
                raise ValueError("hotel selection is incomplete")
            rows = find_hotels(session, data.city, data.check_in, data.check_out,
                               airport=data.airport, limit=100)
            row = next((item for item in rows
                        if item["product_id"] == data.candidate_id), None)
            if row is None or row["nightly_price"] != data.unit_price:
                raise ValueError("hotel candidate or quote changed; search again")
            action_type = "hotel_book"
            parameters = {"product_id": row["product_id"],
                          "check_in": data.check_in.isoformat(),
                          "check_out": data.check_out.isoformat()}
        else:
            if (not isinstance(data.candidate_id, str) or
                    not (data.airport or data.city) or not data.pickup_date or
                    not data.drop_off_date or not data.pickup_clock or
                    not data.dropoff_clock or data.pickup_clock.second or
                    data.dropoff_clock.second or data.pickup_clock.microsecond or
                    data.dropoff_clock.microsecond or data.pickup_clock.tzinfo or
                    data.dropoff_clock.tzinfo):
                raise ValueError("rental selection needs local pickup and return times")
            rows = find_rentals(session, data.pickup_date, data.drop_off_date,
                                airport=data.airport, city=data.city, limit=100)
            row = next((item for item in rows if item["id"] == data.candidate_id), None)
            if row is None or row["daily_price"] != data.unit_price:
                raise ValueError("rental candidate or quote changed; search again")
            action_type = "rental_book"
            parameters = {"product_id": row["id"],
                          "pickup_at": f"{data.pickup_date}T{data.pickup_clock:%H:%M}",
                          "dropoff_at": f"{data.drop_off_date}T{data.dropoff_clock:%H:%M}"}
        card = create_proposal(session, traveler["id"], session_id,
                               action_type, parameters, reuse_pending=True)
        session.commit()
        return card
    except SessionNotFound as error:
        session.rollback()
        raise HTTPException(404, str(error)) from error
    except (ValueError, OrderError, ActionError) as error:
        session.rollback()
        raise HTTPException(409, str(error)) from error


@router.get("/sessions/{session_id}/references")
def session_references(session_id: str, traveler=Depends(current_traveler),
                       session: Session = Depends(get_db)):
    try:
        return latest_session_references(session, traveler["id"], session_id)
    except SessionNotFound as error:
        raise HTTPException(404, str(error)) from error


@router.post("/sessions/{session_id}/messages", status_code=201)
def add_user_message(session_id: str, data: UserMessage,
                     traveler=Depends(current_traveler),
                     session: Session = Depends(get_db)):
    try:
        _owned_session(session, traveler["id"], session_id, lock=True)
        if has_graph_turns(session, session_id):
            raise HTTPException(409, "use /turns after conversation begins")
        message = append_message(session, traveler["id"], session_id,
                                 "user", data.content)
    except SessionNotFound as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    session.commit()
    return message


@router.post("/sessions/{session_id}/turns")
def conversation_turn(session_id: str, data: TurnRequest,
                      traveler=Depends(current_traveler),
                      session: Session = Depends(get_db)):
    if settings.CLOUD_MODE:
        with cloud_limits.turn(traveler["id"]) as allowed:
            if not allowed:
                raise HTTPException(429, "too many conversation requests",
                                    headers={"Retry-After": "60"})
            return _process_turn(session_id, data, traveler["id"], session)
    return _process_turn(session_id, data, traveler["id"], session)


def _process_turn(session_id, data, traveler_id, session, progress=None):
    token = usage_traveler.set(traveler_id)
    try:
        return _process_turn_inner(session_id, data, traveler_id, session, progress)
    finally:
        usage_traveler.reset(token)


def _process_turn_inner(session_id, data, traveler_id, session, progress=None):
    try:
        get_session_history(session, traveler_id, session_id)
    except SessionNotFound as error:
        raise HTTPException(404, str(error)) from error
    content = data.content.strip()
    if not content:
        raise HTTPException(422, "message cannot be blank")
    try:
        turn, created = begin_turn(session, traveler_id, session_id,
                                   data.request_key, content)
        if created:
            try:
                model = get_chat_model()
            except ModelUnavailable:
                if not re.search(r"你记得|还记得|前面说过什么|刚才聊了什么|我们聊过什么",
                                 content):
                    raise
                model = _MemoryOnlyModel()
        else:
            model = None
        session.commit()
        if progress:
            progress("accepted")
    except SessionNotFound as error:
        session.rollback()
        raise HTTPException(404, str(error)) from error
    except (TurnConflict, ValueError) as error:
        session.rollback()
        raise HTTPException(409, str(error)) from error
    except ModelUnavailable as error:
        session.rollback()
        raise HTTPException(503, str(error)) from error
    if not created:
        outcome = public_turn(turn)
        if outcome["status"] == "processing":
            raise HTTPException(409, "turn is already processing")
        if outcome["status"] == "failed":
            raise HTTPException(503, outcome)
        return outcome

    try:
        history = [{"role": row["role"], "content": row["content"]}
                   for row in get_session_history(session, traveler_id,
                                                  session_id)["messages"][-12:]]
        today = demo_date(session).isoformat()
        bind = session.get_bind()
        session.commit()  # release the read transaction before the model call

        def read_evidence(intent, params):
            with Session(bind=bind) as read_db:
                if intent == "action_guidance":
                    return read_action_guidance(read_db, traveler_id, session_id,
                                                params.get("mode"))
                if intent == "memory_cards":
                    return {"cards": list_proposals(read_db, traveler_id, session_id)}
                if intent == "recommendation":
                    return read_recommendation(
                        read_db, traveler_id, params.get("user_text", ""),
                        params, get_recommendation_provider())
                return read_for_assistant(read_db, traveler_id, intent, params)

        if progress:
            progress("checking")
        with open_checkpointer() as saver:
            graph = build_graph(model, read_evidence, saver)
            result = graph.invoke(
                {"history": history, "demo_date": today, "answer": "",
                 "references": [], "transaction_plan": []},
                {"configurable": {"thread_id": session_id}, "recursion_limit": 5},
            )
        answer = result.get("answer", "")
        references = result.get("references", [])
        if (not isinstance(answer, str) or not 1 <= len(answer) <= 4000
                or not isinstance(references, list) or len(references) > 15):
            raise ValueError("invalid graph result")
        actions = []
        plans = result.get("transaction_plan") or []
        flight_task = (result.get("task_contexts") or {}).get("flight_search") or {}
        logger.info("dialogue_route intent=%s action_types=%s reference_count=%d "
                    "flight_fields=%s flight_candidate_count=%d",
                    result.get("intent"),
                    [item.get("action_type") for item in plans if isinstance(item, dict)],
                    len(references),
                    sorted(key for key in ("departure", "arrival", "travel_date",
                                           "not_before", "sort") if flight_task.get(key)),
                    len((result.get("candidates") or {}).get("flight") or []))
        mass_cancel = bulk_cancellation_requested(content)
        overview = ""
        if mass_cancel:
            with Session(bind=bind) as read_db:
                overview, plans = cancellation_overview(read_db, traveler_id, content)
            answer, references = overview, []
        if progress:
            progress("verifying")
        if plans:
            try:
                explicit_actions = requested_actions(content)
                read_only_request = is_read_only_action_request(content)
                if not mass_cancel and (
                        read_only_request or
                        explicit_actions and any(
                            not isinstance(plan, dict) or
                            plan.get("action_type") not in explicit_actions
                            for plan in plans)):
                    raise SelectionNeeded("本轮订单业务与旅客明确要求不一致，请重新核对对象。")
                lock_processing_turn(session, traveler_id, session_id, turn["id"])
                candidates = result.get("candidates") or {}
                flight_task = (result.get("task_contexts") or {}).get("flight_search") or {}
                if not candidates.get("flight") and any(
                        isinstance(plan, dict) and plan.get("action_type") == "flight_book"
                        for plan in plans):
                    if flight_task.get("departure") or flight_task.get("arrival"):
                        raise SelectionNeeded("当前航班任务的候选已变化，请按现有路线重新查询并选择。")
                    if re.search(r"前面|刚才|上面", content):
                        raise SelectionNeeded("本会话尚未查询过该航班，请先查询并核对班次。")
                shown = [sum((list(items) for items in candidates.values()
                              if isinstance(items, list)), [])] if isinstance(candidates, dict) else []
                resolved = prepare_actions(session, traveler_id, plans,
                                           shown, content, content, local_today(),
                                           result.get("context"), bulk_cancel=mass_cancel)
                verify_shown_quotes(session, resolved, shown)
                reused_count = 0
                for index, (action_type, parameters) in enumerate(resolved):
                    key = str(uuid5(UUID(turn["id"]), str(index)))
                    card = create_proposal(
                        session, traveler_id, session_id, action_type,
                        parameters, idempotency_key=key, reuse_pending=True,
                    )
                    reused_count += card["id"] != key
                    actions.append(jsonable_encoder(card))
                references = references + [{"type": "action", "id": card["id"]}
                                           for card in actions]
                queried = [item.get("intent", "").split("_")[0]
                           for item in (result.get("params", {}).get("queries") or [])
                           if isinstance(item, dict)]
                action_kinds = {kind.split("_")[0] for kind, _ in resolved}
                read_answer = (answer if not mass_cancel and any(
                    kind not in action_kinds for kind in queried) else "")
                card_notice = (f"沿用已有的 {reused_count} 张待确认卡。" if reused_count == len(actions)
                               else f"已生成 {len(actions) - reused_count} 张待确认卡，"
                                    f"沿用已有 {reused_count} 张。" if reused_count else
                                    f"已生成 {len(actions)} 张待确认卡。")
                if mass_cancel:
                    overview = overview.replace("订单尚未改变；本轮可办理的项目仍须逐张确认。", "").rstrip()
                    if "本轮仅为" in overview:
                        card_notice = ""
                answer = (overview + "\n" if overview else "") + (
                          card_notice +
                          "请在下方逐张核对对象、时间、金额和政策，再选择确认或放弃。"
                          "你确认前，订单不会改变。")
                if len(actions) == 1:
                    answer += "\n本轮对象：" + _card_brief(actions[0]) + "。"
                if read_answer:
                    answer += "\n同时查到：" + read_answer
            except (SelectionNeeded, ActionError) as error:
                logger.info("dialogue_proposal_rejected reason=%s", type(error).__name__)
                session.rollback()  # any partial card and audit are discarded
                actions = []
                if isinstance(error, SelectionNeeded):
                    with Session(bind=bind) as read_db:
                        answer = explain_selection_needed(read_db, traveler_id, plans, error)
                else:
                    with Session(bind=bind) as read_db:
                        answer = explain_action_error(read_db, traveler_id, plans, error)
                if overview:
                    answer = (overview + "\n" + answer)[:4000]
                elif result.get("params", {}).get("queries"):
                    answer = (answer + "\n查询结果：" + result.get("answer", ""))[:4000]
        if len(references) > 15:
            references = references[:15]
        composer = getattr(model, "compose", None)
        if (not plans and not actions and not mass_cancel and callable(composer)):
            try:
                composed = composer(result.get("intent"), content, answer, references)
                if isinstance(composed, str) and 1 <= len(composed) <= 4000:
                    answer = composed
            except Exception as error:
                logger.warning("dialogue_presentation_fallback reason=%s",
                               type(error).__name__)
        outcome = finish_turn(session, traveler_id, session_id, turn["id"],
                              answer, references, actions)
        session.commit()
        return outcome
    except Exception as error:
        logger.warning("dialogue_turn_failed reason=%s", type(error).__name__)
        session.rollback()
        try:
            fail_turn(session, session_id, turn["id"], "turn_failed")
            session.commit()
        except Exception:
            session.rollback()
        detail = "provider_balance" if isinstance(error, ProviderBalanceError) else "conversation turn failed"
        raise HTTPException(503, detail) from error


def _turn_event(name, payload):
    return f"event: {name}\ndata: {json.dumps({'v': 1, **payload}, ensure_ascii=False)}\n\n"


@router.get("/sessions/{session_id}/turns/status/latest")
def latest_turn_status(session_id: str, traveler=Depends(current_traveler),
                       session: Session = Depends(get_db)):
    try:
        return latest_turn(session, traveler["id"], session_id)
    except SessionNotFound as error:
        raise HTTPException(404, str(error)) from error


@router.get("/sessions/{session_id}/turns/{request_key}")
def turn_status(session_id: str, request_key: str,
                traveler=Depends(current_traveler),
                session: Session = Depends(get_db)):
    if not 8 <= len(request_key) <= 80 or not re.fullmatch(r"[A-Za-z0-9._-]+", request_key):
        raise HTTPException(422, "invalid request key")
    try:
        outcome = get_turn(session, traveler["id"], session_id, request_key)
    except SessionNotFound as error:
        raise HTTPException(404, str(error)) from error
    if outcome is None:
        raise HTTPException(404, "turn not found")
    return outcome


@router.post("/sessions/{session_id}/turns/stream")
def conversation_turn_stream(session_id: str, data: TurnRequest,
                             traveler=Depends(current_traveler),
                             session: Session = Depends(get_db)):
    """Stream honest stages, then only the committed, verified answer."""
    try:
        get_session_history(session, traveler["id"], session_id)
    except SessionNotFound as error:
        raise HTTPException(404, str(error)) from error
    if not data.content.strip():
        raise HTTPException(422, "message cannot be blank")
    bind = session.get_bind()
    traveler_id = traveler["id"]
    session.rollback()

    async def events():
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()

        if settings.CLOUD_MODE and not cloud_limits.acquire_turn(traveler_id):
            yield _turn_event("failed", {"code": "rate_limited", "status": 429,
                                         "request_key": data.request_key})
            return
        if not turn_slots.acquire(blocking=False):
            if settings.CLOUD_MODE:
                cloud_limits.release_turn(traveler_id)
            yield _turn_event("failed", {"code": "busy", "status": 503,
                                         "request_key": data.request_key})
            return

        def send(name, payload):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, (name, payload))
            except RuntimeError:
                pass  # client loop closed; the worker still persists its turn

        def work():
            try:
                with Session(bind=bind) as work_db:
                    outcome = _process_turn(
                        session_id, data, traveler_id, work_db,
                        progress=lambda stage: send(
                            "accepted" if stage == "accepted" else "progress",
                            {"stage": stage, "request_key": data.request_key}),
                    )
                send("result", outcome)
            except HTTPException as error:
                code = ("processing" if error.status_code == 409 else
                        "not_found" if error.status_code == 404 else
                        "invalid_input" if error.status_code == 422 else
                        "provider_balance" if error.detail == "provider_balance" else
                        "unavailable")
                send("failed", {"code": code, "status": error.status_code,
                                "request_key": data.request_key})
            except Exception as error:
                logger.warning("stream_turn_failed reason=%s", type(error).__name__)
                send("failed", {"code": "unavailable", "status": 503,
                                "request_key": data.request_key})
            finally:
                turn_slots.release()
                if settings.CLOUD_MODE:
                    cloud_limits.release_turn(traveler_id)

        try:
            loop.run_in_executor(turn_executor, work)
        except RuntimeError:
            turn_slots.release()
            if settings.CLOUD_MODE:
                cloud_limits.release_turn(traveler_id)
            yield _turn_event("failed", {"code": "busy", "status": 503,
                                         "request_key": data.request_key})
            return
        yield _turn_event("progress", {"stage": "received", "request_key": data.request_key})
        while True:
            try:
                name, payload = await asyncio.wait_for(queue.get(), timeout=12)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if name == "result":
                answer = payload["answer"] or ""
                for start in range(0, len(answer), 96):
                    yield _turn_event("delta", {"text": answer[start:start + 96]})
                    await asyncio.sleep(0)
                yield _turn_event("completed", {
                    "request_key": data.request_key, "status": payload["status"],
                    "action_count": len(payload["actions"]),
                })
                return
            yield _turn_event(name, payload)
            if name == "failed":
                return

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/sessions/{session_id}/actions")
def session_actions(session_id: str, traveler=Depends(current_traveler),
                    session: Session = Depends(get_db)):
    try:
        return list_proposals(session, traveler["id"], session_id)
    except SessionNotFound as error:
        raise HTTPException(404, str(error)) from error


@router.get("/sessions/{session_id}/actions/{action_id}")
def action_detail(session_id: str, action_id: str,
                  traveler=Depends(current_traveler),
                  session: Session = Depends(get_db)):
    try:
        return get_proposal(session, traveler["id"], session_id, action_id)
    except (SessionNotFound, ActionNotFound) as error:
        raise HTTPException(404, str(error)) from error


@router.post("/sessions/{session_id}/actions/{action_id}/confirm")
def confirm_saved_action(session_id: str, action_id: str,
                         data: EmptyActionRequest | None = None,
                         traveler=Depends(current_traveler),
                         session: Session = Depends(get_db)):
    try:
        outcome = confirm_action(session, traveler["id"], session_id, action_id)
        session.commit()
    except IntegrityError as error:
        session.rollback()
        constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        if constraint != "uq_flight_orders_active_traveler_flight":
            if constraint != "uq_rental_orders_active_slot":
                raise
            raise HTTPException(409, "traveler already holds this rental period") from error
        raise HTTPException(409, "traveler already holds this flight") from error
    except (SessionNotFound, ActionNotFound) as error:
        raise HTTPException(404, str(error)) from error
    if outcome["status"] != "confirmed":
        raise HTTPException(409, outcome)
    return outcome


@router.post("/sessions/{session_id}/actions/{action_id}/reject")
def reject_saved_action(session_id: str, action_id: str,
                        data: EmptyActionRequest | None = None,
                        traveler=Depends(current_traveler),
                        session: Session = Depends(get_db)):
    try:
        outcome = reject_action(session, traveler["id"], session_id, action_id)
        session.commit()
    except (SessionNotFound, ActionNotFound) as error:
        raise HTTPException(404, str(error)) from error
    if outcome["status"] != "rejected":
        raise HTTPException(409, outcome)
    return outcome


@router.get("/me/action-history")
def my_action_history(traveler=Depends(current_traveler),
                      session: Session = Depends(get_db)):
    return list_action_history(session, traveler["id"])
