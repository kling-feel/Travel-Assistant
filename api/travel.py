"""P1 traveler API. Client input never selects a traveler account."""

import logging
import re
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import settings
from db.travel_models import airports, travelers
from services.location_labels import airport_zh, city_zh
from services.travel import (OrderError, find_flights, find_hotels, find_rentals,
                             itinerary, policies)
from services.sessions import (SessionNotFound, append_message,
                               create_session as create_chat_session,
                               get_session_history, list_sessions, _owned_session)
from services.actions import (ActionError, ActionNotFound, create_proposal,
                              get_proposal, list_proposals)
from services.confirmations import confirm_action, reject_action, list_action_history
from services.agent_reads import demo_date, read_action_guidance, read_for_assistant
from services.dialogue_context import local_today
from services.chat_turns import (TurnConflict, _public as public_turn, begin_turn,
                                 fail_turn, finish_turn, has_graph_turns,
                                 lock_processing_turn)
from services.agent_proposals import SelectionNeeded, prepare_actions, verify_shown_quotes
from services.agent_feedback import (cancellation_overview, explain_action_error,
                                     explain_selection_needed)
from services.checkpoints import open_checkpointer
from services.model_config import ModelUnavailable, get_chat_model
from services.recommendations import (get_recommendation_provider,
                                      read_recommendation)
from graph_chat.controlled import build_graph
from utils.dependencies import get_db
from utils.password_hash import verify_password


router = APIRouter()
bearer = HTTPBearer()
logger = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1)


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
    traveler = session.execute(select(travelers).where(
        travelers.c.username == data.username, travelers.c.active == 1
    )).mappings().one_or_none()
    if traveler is None or not verify_password(data.password, traveler["password_hash"]):
        raise HTTPException(401, "invalid credentials")
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
    try:
        get_session_history(session, traveler["id"], session_id)
    except SessionNotFound as error:
        raise HTTPException(404, str(error)) from error
    content = data.content.strip()
    if not content:
        raise HTTPException(422, "message cannot be blank")
    try:
        turn, created = begin_turn(session, traveler["id"], session_id,
                                   data.request_key, content)
        model = get_chat_model() if created else None
        session.commit()
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
                   for row in get_session_history(session, traveler["id"],
                                                  session_id)["messages"][-12:]]
        today = demo_date(session).isoformat()
        bind = session.get_bind()
        session.commit()  # release the read transaction before the model call

        def read_evidence(intent, params):
            with Session(bind=bind) as read_db:
                if intent == "action_guidance":
                    return read_action_guidance(read_db, traveler["id"], session_id,
                                                params.get("mode"))
                if intent == "recommendation":
                    return read_recommendation(
                        read_db, traveler["id"], params.get("user_text", ""),
                        params, get_recommendation_provider())
                return read_for_assistant(read_db, traveler["id"], intent, params)

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
        logger.info("dialogue_route intent=%s action_types=%s reference_count=%d",
                    result.get("intent"),
                    [item.get("action_type") for item in plans if isinstance(item, dict)],
                    len(references))
        mass_cancel = ("取消" in content and bool(re.search(r"所有|全部", content))
                       and "机票" in content and "酒店" in content)
        overview = ""
        if mass_cancel:
            with Session(bind=bind) as read_db:
                overview, plans = cancellation_overview(read_db, traveler["id"])
            answer, references = overview, []
        if plans:
            try:
                lock_processing_turn(session, traveler["id"], session_id, turn["id"])
                candidates = result.get("candidates") or {}
                shown = [sum((list(items) for items in candidates.values()
                              if isinstance(items, list)), [])] if isinstance(candidates, dict) else []
                resolved = prepare_actions(session, traveler["id"], plans,
                                           shown, content, content, local_today(),
                                           result.get("context"))
                verify_shown_quotes(session, resolved, shown)
                reused_count = 0
                for index, (action_type, parameters) in enumerate(resolved):
                    key = str(uuid5(UUID(turn["id"]), str(index)))
                    card = create_proposal(
                        session, traveler["id"], session_id, action_type,
                        parameters, idempotency_key=key, reuse_pending=True,
                    )
                    reused_count += card["id"] != key
                    actions.append(jsonable_encoder(card))
                references = references + [{"type": "action", "id": card["id"]}
                                           for card in actions]
                read_answer = answer if result.get("params", {}).get("queries") else ""
                card_notice = (f"沿用已有的 {reused_count} 张待确认卡。" if reused_count == len(actions)
                               else f"已生成 {len(actions) - reused_count} 张待确认卡，"
                                    f"沿用已有 {reused_count} 张。" if reused_count else
                                    f"已生成 {len(actions)} 张待确认卡。")
                answer = (overview + "\n" if overview else "") + (
                          card_notice +
                          "请逐张核对对象、时间、金额和政策，并通过对应动作接口确认或拒绝；"
                          "目前订单尚未改变。")
                if read_answer:
                    answer += "\n查询结果：" + read_answer
            except (SelectionNeeded, ActionError) as error:
                logger.info("dialogue_proposal_rejected reason=%s", type(error).__name__)
                session.rollback()  # any partial card and audit are discarded
                actions = []
                if isinstance(error, SelectionNeeded):
                    with Session(bind=bind) as read_db:
                        answer = explain_selection_needed(read_db, traveler["id"], plans, error)
                else:
                    with Session(bind=bind) as read_db:
                        answer = explain_action_error(read_db, traveler["id"], plans, error)
                if overview:
                    answer = (overview + "\n" + answer)[:4000]
                elif result.get("params", {}).get("queries"):
                    answer = (answer + "\n查询结果：" + result.get("answer", ""))[:4000]
        if len(references) > 15:
            references = references[:15]
        outcome = finish_turn(session, traveler["id"], session_id, turn["id"],
                              answer, references, actions)
        session.commit()
        return outcome
    except Exception as error:
        session.rollback()
        try:
            fail_turn(session, session_id, turn["id"], "turn_failed")
            session.commit()
        except Exception:
            session.rollback()
        raise HTTPException(503, "conversation turn failed") from error


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
