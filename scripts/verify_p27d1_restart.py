"""Verify reviewed-place turns persist across an isolated database restart."""

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import delete

from db import engine
from db.travel_models import chat_messages, chat_sessions, chat_turns
from scripts.p26d_eval_guard import assert_eval_database
from scripts.verify_p26a_dialogue import FixedModel, _app, _auth, _counts
from services.checkpoints import open_checkpointer


STATE = Path(__file__).resolve().parents[1] / "data/generated/p27d1-restart-state.json"


def _password():
    value = os.getenv("DEMO_TRAVELER_1_PASSWORD")
    if not value:
        raise RuntimeError("demo traveler password is not configured")
    return value


def prepare():
    assert_eval_database()
    if STATE.exists():
        raise RuntimeError("pending restart test already exists")
    before = _counts()
    if any(before[key] for key in ("chat_sessions", "chat_turns", "pending_actions")):
        raise RuntimeError("isolated database is not at a clean demo baseline")
    model = FixedModel(['{"intent":"recommendation","params":{"city":"Basel"},"answer":""}'])
    with patch("api.travel.get_chat_model", return_value=model):
        with TestClient(_app()) as client:
            auth = _auth(client, "demo1", _password())
            created = client.post("/api/v1/sessions", headers=auth, json={})
            assert created.status_code == 201, created.text
            sid = created.json()["id"]
            turn = client.post(f"/api/v1/sessions/{sid}/turns", headers=auth,
                               json={"request_key": "p27d1-restart-001",
                                     "content": "推荐 Basel 的经典地点"})
            assert turn.status_code == 200, turn.text
            assert len(turn.json()["references"]) == 2
            assert not turn.json()["actions"]
    with open_checkpointer() as saver:
        assert saver.get_tuple({"configurable": {"thread_id": sid}}) is not None
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"session_id": sid, "before": before}), encoding="utf-8")
    print("P2.7d1 isolated recommendation checkpoint prepared")


def finish():
    assert_eval_database()
    data = json.loads(STATE.read_text(encoding="utf-8"))
    sid, before = data["session_id"], data["before"]
    with open_checkpointer() as saver:
        assert saver.get_tuple({"configurable": {"thread_id": sid}}) is not None
    model = FixedModel(['{"intent":"recommendation","params":{"city":"Basel"},"answer":""}'])
    with patch("api.travel.get_chat_model", return_value=model):
        with TestClient(_app()) as client:
            auth = _auth(client, "demo1", _password())
            turn = client.post(f"/api/v1/sessions/{sid}/turns", headers=auth,
                               json={"request_key": "p27d1-restart-002",
                                     "content": "再推荐巴塞尔的建筑景点"})
            assert turn.status_code == 200, turn.text
            assert len(turn.json()["references"]) == 1
            assert not turn.json()["actions"]
            history = client.get(f"/api/v1/sessions/{sid}", headers=auth)
            assert history.status_code == 200, history.text
            assert len(history.json()["messages"]) == 4
    with open_checkpointer() as saver:
        saver.delete_thread(sid)
    with engine.begin() as db:
        db.execute(delete(chat_turns).where(chat_turns.c.session_id == sid))
        db.execute(delete(chat_messages).where(chat_messages.c.session_id == sid))
        db.execute(delete(chat_sessions).where(chat_sessions.c.id == sid))
    assert _counts() == before, "isolated database baseline changed"
    STATE.unlink()
    print("P2.7d1 isolated restart, history, checkpoint and cleanup: OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "finish"))
    args = parser.parse_args()
    prepare() if args.phase == "prepare" else finish()
