"""Verify PostgreSQL dialogue checkpoints survive a database container restart.

Run ``prepare``, restart the database, then run ``finish`` as separate processes.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from travel_assistant.db import engine
from travel_assistant.db.travel_models import chat_messages, chat_sessions, chat_turns
from scripts.verify_p26a_dialogue import FixedModel, _app, _auth, _counts
from services.checkpoints import open_checkpointer

STATE = ROOT / "data" / "generated" / "p26a-restart-state.json"


def prepare():
    if STATE.exists():
        raise RuntimeError(f"Pending restart check already exists: {STATE}")
    password = os.getenv("DEMO_TRAVELER_1_PASSWORD")
    if not password:
        raise RuntimeError("Configure demo traveler password in ignored .env")
    before = _counts()
    if before["chat_sessions"] or before["chat_turns"] or before["pending_actions"]:
        raise RuntimeError("Restart check requires a clean demo snapshot")
    model = FixedModel(['{"intent":"policy","params":{"scope":"flight"},"answer":""}'])
    with patch("api.travel.get_chat_model", return_value=model):
        with TestClient(_app()) as client:
            auth = _auth(client, "demo1", password)
            created = client.post("/api/v1/sessions", headers=auth, json={})
            assert created.status_code == 201, created.text
            sid = created.json()["id"]
            turn = client.post(f"/api/v1/sessions/{sid}/turns", headers=auth,
                               json={"request_key": "restart-policy-001",
                                     "content": "机票取消政策是什么？"})
            assert turn.status_code == 200, turn.text
            assert turn.json()["references"][0]["type"] == "policy"
    with open_checkpointer() as saver:
        assert saver.get_tuple({"configurable": {"thread_id": sid}}) is not None
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"session_id": sid, "before": before}), encoding="utf-8")
    print("P2.6a restart checkpoint prepared; restart the database before finish")


def finish():
    data = json.loads(STATE.read_text(encoding="utf-8"))
    sid, before = data["session_id"], data["before"]
    password = os.getenv("DEMO_TRAVELER_1_PASSWORD")
    if not password:
        raise RuntimeError("Configure demo traveler password in ignored .env")
    with open_checkpointer() as saver:
        assert saver.get_tuple({"configurable": {"thread_id": sid}}) is not None
    model = FixedModel(['{"intent":"itinerary","params":{},"answer":""}'])
    with patch("api.travel.get_chat_model", return_value=model):
        with TestClient(_app()) as client:
            auth = _auth(client, "demo1", password)
            turn = client.post(f"/api/v1/sessions/{sid}/turns", headers=auth,
                               json={"request_key": "restart-trip-002",
                                     "content": "我的行程有哪些？"})
            assert turn.status_code == 200, turn.text
            assert "本人模拟行程" in turn.json()["answer"]
            history = client.get(f"/api/v1/sessions/{sid}", headers=auth)
            assert history.status_code == 200, history.text
            assert len(history.json()["messages"]) == 4
    with open_checkpointer() as saver:
        saver.delete_thread(sid)
    with engine.begin() as db:
        db.execute(delete(chat_turns).where(chat_turns.c.session_id == sid))
        db.execute(delete(chat_messages).where(chat_messages.c.session_id == sid))
        db.execute(delete(chat_sessions).where(chat_sessions.c.id == sid))
    assert _counts() == before, "Restart check did not restore the demo baseline"
    STATE.unlink()
    print("P2.6a database restart, recovered history and checkpoint: OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "finish"))
    phase = parser.parse_args().phase
    prepare() if phase == "prepare" else finish()
