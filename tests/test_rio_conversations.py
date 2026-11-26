"""Focused tests for the RIO conversation-history feature (Phase C).

Covers:
  * conversation_journal conv-stamp (set_current_conversation → rows carry
    `conv`; cleared stamp → later rows don't)
  * rio_api session grouping (≥30-min gap rule; stamped groups span gaps)
  * /api/rio/conversations + /{id} endpoint contract through a real FastAPI
    app, with the SAME bearer auth as every other /api/* route (401 without
    a token, 404 for an unknown id)
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memory.conversation_journal as cj          # noqa: E402
import dashboard.rio_api as rio_api               # noqa: E402


@pytest.fixture()
def journal(tmp_path, monkeypatch):
    d = tmp_path / "conversations"
    d.mkdir(parents=True)
    monkeypatch.setattr(cj, "JOURNAL_DIR", d)
    monkeypatch.setattr(cj, "_state_path", d / "_evolution_state.json")
    monkeypatch.setattr(cj, "_current_conv", None)
    return d


@pytest.fixture()
def client(journal, monkeypatch):
    app = FastAPI()
    RioAuth = lambda request: True        # noqa: E731 — auth not under test here
    rio_api.register(app, RioAuth)
    return TestClient(app)


def _write_rows(d, rows):
    months = {}
    for r in rows:
        month = r["ts"][:7]
        months.setdefault(month, []).append(r)
    for month, mrows in months.items():
        with open(d / f"{month}.jsonl", "w", encoding="utf-8") as f:
            for r in mrows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ── conv stamp ────────────────────────────────────────────────────────────────

def test_stamp_written_and_cleared(journal):
    assert cj.append_turn("user", "unstamped turn before anything") is True
    cj.set_current_conversation("c-test-1")
    assert cj.append_turn("user", "stamped turn") is True
    assert cj.append_turn("jarvis", "stamped reply") is True
    cj.set_current_conversation(None)
    assert cj.append_turn("user", "after clearing") is True

    rows = [json.loads(l) for l in
            (journal / "2026-10.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 4
    assert "conv" not in rows[0]
    assert rows[1]["conv"] == "c-test-1"
    assert rows[2]["conv"] == "c-test-1"
    assert "conv" not in rows[3]


def test_stamp_never_breaks_append(journal, monkeypatch):
    # a broken stamp must degrade to stampless journaling, never lose the turn
    def boom(v):
        raise RuntimeError("stamp explode")
    monkeypatch.setattr(cj, "set_current_conversation", boom, raising=False)
    ok = cj.append_turn("user", "still journaled")
    assert ok is True


# ── grouping / endpoints ─────────────────────────────────────────────────────

def test_gap_rule_and_stamp_grouping(journal, client):
    rows = [
        {"ts": "2026-10-10T09:00:00", "speaker": "user", "text": "first hello"},
        {"ts": "2026-10-10T09:00:10", "speaker": "jarvis", "text": "hi"},
        # 31-min gap → new session
        {"ts": "2026-10-10T09:31:30", "speaker": "user", "text": "after a break"},
        # stamped: even a long gap must NOT split its own conversation
        {"ts": "2026-10-10T11:00:00", "speaker": "user", "text": "stamped turn",
         "conv": "c-live"},
        {"ts": "2026-10-10T11:00:10", "speaker": "jarvis", "text": "stamped reply",
         "conv": "c-live"},
        # 29-min gap → same session (boundary case)
        {"ts": "2026-10-10T11:29:00", "speaker": "user", "text": "just inside",
         "conv": "c-live"},
    ]
    _write_rows(journal, rows)

    r = client.get("/api/rio/conversations")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    convs = data["conversations"]
    # c-live group first (newest), then 09:31 session, then 09:00 session
    assert [c["id"] for c in convs] == ["c-live", "g:20261010093130",
                                        "g:20261010090000"]
    assert convs[0]["turns"] == 3
    assert convs[0]["title"] == "stamped turn"
    assert convs[2]["title"].startswith("first hello")

    detail = client.get("/api/rio/conversations/c-live").json()
    assert detail["ok"] and detail["stamped"] is True
    assert len(detail["messages"]) == 3
    assert detail["messages"][0]["speaker"] == "user"


def test_unknown_conversation_404(journal, client):
    r = client.get("/api/rio/conversations/g:0000")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_endpoints_require_auth(journal):
    app = FastAPI()
    rio_api.register(app, lambda request: False)
    tc = TestClient(app)
    assert tc.get("/api/rio/conversations").status_code == 401
    assert tc.get("/api/rio/conversations/xyz").status_code == 401


def test_corrupt_rows_skipped_never_fabricated(journal, client):
    d = journal
    with open(d / "2026-10.jsonl", "w", encoding="utf-8") as f:
        f.write('{"ts": "2026-10-10T09:00:00", "speaker": "user", "text": "good"}\n')
        f.write("this line is {not json at all\n")
        f.write('{"speaker": "jarvis", "text": "no ts → skipped"}\n')
    convs = client.get("/api/rio/conversations").json()["conversations"]
    assert len(convs) == 1
    assert convs[0]["turns"] == 1


def test_multi_month_listing(journal, client):
    rows = [
        {"ts": "2026-09-28T20:00:00", "speaker": "user", "text": "September talk"},
        {"ts": "2026-10-01T08:00:00", "speaker": "user", "text": "October talk"},
    ]
    _write_rows(journal, rows)
    convs = client.get("/api/rio/conversations").json()["conversations"]
    assert [c["title"] for c in convs] == ["October talk", "September talk"]
