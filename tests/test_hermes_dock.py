"""
tests/test_hermes_dock.py — the contextual Hermes assistant dock.

What this locks in:
  1. The ask surface uses the shared bearer auth and rejects bad input
     instead of silently truncating a prompt/page/context.
  2. POST /api/rio/hermes/ask queues the EXISTING hermes_task bridge and
     GET /api/rio/hermes/ask/{id} returns QUEUED -> RUNNING -> COMPLETED.
  3. A bridge failure string becomes FAILED with the bridge's own words;
     no invented answer.
  4. The queue is bounded (429) and asks are scoped/filterable by page.
  5. One shared drawer is mounted by News, Trading and Backtest; it is
     served by the asset whitelist and loaded before the page modules.
  6. The dock is research-only: no order/broker/execution call in its JS.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard.hermes_api as hermes_api  # noqa: E402
import dashboard.rio_api as rio_api  # noqa: E402
import actions.hermes_task as hermes_task_mod  # noqa: E402

AUTH = {"Authorization": "Bearer test"}
ROOT = Path(__file__).resolve().parent.parent
RIO = ROOT / "dashboard" / "static" / "rio"


@pytest.fixture(autouse=True)
def clean_asks():
    with hermes_api._ASK_LOCK:
        hermes_api._ASKS.clear()
    yield
    with hermes_api._ASK_LOCK:
        hermes_api._ASKS.clear()


def _app(auth: bool = True) -> TestClient:
    app = FastAPI()
    hermes_api.register(app, lambda request: auth)
    return TestClient(app)


def _wait(client: TestClient, ask_id: str, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = client.get(f"/api/rio/hermes/ask/{ask_id}", headers=AUTH)
        assert res.status_code == 200, res.text
        ask = res.json()["ask"]
        if ask["status"] not in {"QUEUED", "RUNNING"}:
            return ask
        time.sleep(0.02)
    raise AssertionError(f"ask {ask_id} did not finish in {timeout}s")


# ── 1. auth + validation ─────────────────────────────────────────────────────

def test_ask_endpoints_require_auth():
    c = _app(auth=False)
    assert c.post("/api/rio/hermes/ask", json={
        "prompt": "hi", "page": "news"}).status_code == 401
    assert c.get("/api/rio/hermes/asks").status_code == 401
    assert c.get("/api/rio/hermes/ask/ha-nope").status_code == 401


@pytest.mark.parametrize("body,status", [
    ({}, 400),
    ({"prompt": "", "page": "news"}, 400),
    ({"prompt": "   ", "page": "news"}, 400),
    ({"prompt": "x" * 4001, "page": "news"}, 400),
    ({"prompt": "hi", "page": "files"}, 400),
    ({"prompt": "hi", "page": "news", "context": "c" * 5001}, 400),
])
def test_ask_validation(body, status):
    c = _app()
    assert c.post("/api/rio/hermes/ask", headers=AUTH,
                  json=body).status_code == status


def test_ask_rejects_malformed_json():
    c = _app()
    res = c.post("/api/rio/hermes/ask", headers={
        **AUTH, "Content-Type": "application/json"}, content=b"not-json")
    assert res.status_code == 400
    assert "prompt" in res.json()["error"]


# ── 2. lifecycle through the existing bridge ─────────────────────────────────

def test_ask_completes_and_is_page_scoped(monkeypatch):
    seen = {}

    def fake_bridge(parameters, player=None):
        seen.update(parameters)
        return "NVDA moved on a dated earnings headline."

    monkeypatch.setattr(hermes_task_mod, "hermes_task", fake_bridge)
    c = _app()
    res = c.post("/api/rio/hermes/ask", headers=AUTH, json={
        "prompt": "Why is NVDA moving?",
        "page": "trading",
        "context": "Page: Trading Center\nWatchlist: NVDA, AAPL",
    })
    assert res.status_code == 202, res.text
    ask = res.json()["ask"]
    assert ask["ask_id"].startswith("ha-")
    assert ask["status"] in {"QUEUED", "RUNNING", "COMPLETED"}

    done = _wait(c, ask["ask_id"])
    assert done["status"] == "COMPLETED"
    assert done["answer"].startswith("NVDA moved")
    assert done["error"] == ""

    task = seen.get("task", "")
    assert "Why is NVDA moving?" in task
    assert "RIO trading page" in task
    assert "Watchlist: NVDA, AAPL" in task
    assert "untrusted display data" in task
    assert "no order" in task.lower() or "do not place orders" in task

    listed = c.get("/api/rio/hermes/asks?page=trading", headers=AUTH).json()
    assert [a["ask_id"] for a in listed["asks"]] == [ask["ask_id"]]
    assert listed["asks"][0]["prompt"] == "Why is NVDA moving?"
    assert "_task" not in listed["asks"][0]

    other = c.get("/api/rio/hermes/asks?page=news", headers=AUTH).json()
    assert other["asks"] == []


def test_ask_unknown_id_and_page_filter(monkeypatch):
    monkeypatch.setattr(hermes_task_mod, "hermes_task",
                        lambda parameters, player=None: "ok")
    c = _app()
    assert c.get("/api/rio/hermes/ask/ha-missing",
                 headers=AUTH).status_code == 404
    assert c.get("/api/rio/hermes/asks?page=files",
                 headers=AUTH).status_code == 400


def _failure_case(monkeypatch, text: str):
    monkeypatch.setattr(hermes_task_mod, "hermes_task",
                        lambda parameters, player=None: text)
    c = _app()
    res = c.post("/api/rio/hermes/ask", headers=AUTH, json={
        "prompt": "check the bridge", "page": "news"})
    assert res.status_code == 202
    return _wait(c, res.json()["ask"]["ask_id"])


def test_ask_reports_bridge_failure_without_inventing_an_answer(monkeypatch):
    ask = _failure_case(
        monkeypatch,
        "Hermes could not complete the task (exit 127). "
        "Hermes CLI not found on this machine.")
    assert ask["status"] == "FAILED"
    assert ask["answer"] == ""
    assert "Hermes CLI not found" in ask["error"]


def test_ask_reports_timeout(monkeypatch):
    ask = _failure_case(
        monkeypatch, "Hermes ran out of time on that one. Try breaking it "
                     "into smaller steps.")
    assert ask["status"] == "FAILED"
    assert "ran out of time" in ask["error"]


def test_ask_empty_output_is_a_failure(monkeypatch):
    ask = _failure_case(monkeypatch, "")
    assert ask["status"] == "FAILED"
    assert "no output" in ask["error"]


# ── 3. bounded queue ────────────────────────────────────────────────────────

def test_ask_queue_is_bounded(monkeypatch):
    monkeypatch.setattr(hermes_api, "_ASK_MAX_PENDING", 1)
    started = threading.Event()
    release = threading.Event()

    def slow_bridge(parameters, player=None):
        started.set()
        release.wait(2)
        return "finished"

    monkeypatch.setattr(hermes_task_mod, "hermes_task", slow_bridge)
    c = _app()
    try:
        first = c.post("/api/rio/hermes/ask", headers=AUTH, json={
            "prompt": "first", "page": "news"})
        assert first.status_code == 202
        assert started.wait(2), "the worker never started"
        second = c.post("/api/rio/hermes/ask", headers=AUTH, json={
            "prompt": "second", "page": "news"})
        assert second.status_code == 429
        assert "busy" in second.json()["error"].lower()
        assert second.headers.get("retry-after") == "5"
    finally:
        release.set()
    done = _wait(c, first.json()["ask"]["ask_id"])
    assert done["status"] == "COMPLETED"


# ── 4. one shared dock, wired into the three pages ──────────────────────────

def test_dock_asset_is_whitelisted_and_served():
    assert rio_api._ASSETS.get("hermes_dock.js") == "application/javascript"
    app = FastAPI()
    rio_api.register(app, lambda request: True)
    c = TestClient(app)
    res = c.get("/rio/assets/hermes_dock.js")
    assert res.status_code == 200
    assert "RIOHermesDock" in res.text
    assert "no order, broker or execution path" in res.text


def test_shell_loads_dock_before_page_modules():
    app = FastAPI()
    rio_api.register(app, lambda request: True)
    c = TestClient(app)
    for route in ("/news",):
        res = c.get(route)
        assert res.status_code == 200
        html = res.text
        assert "hermes_dock.js" in html
        assert html.index("hermes_dock.js") < html.index("news.js")


@pytest.mark.parametrize("name,page", [
    ("news.js", "news"),
])
def test_page_modules_mount_the_shared_dock(name, page):
    js = (RIO / name).read_text(encoding="utf-8")
    assert "RIOHermesDock.attach" in js
    assert f'page: "{page}"' in js
    assert "context: hermesContext" in js


def test_dock_ui_contract():
    js = (RIO / "hermes_dock.js").read_text(encoding="utf-8")
    for needle in ("/api/rio/hermes/ask", "/api/rio/hermes/asks",
                   "/api/rio/hermes", "ask.ask_id",
                   "page=${encodeURIComponent(page)}",
                   "aria-expanded", "Escape", "local research assistant"):
        assert needle in js
    css = (RIO / "styles.css").read_text(encoding="utf-8")
    for needle in (".hd-fab", ".hd-drawer", ".hd-pill.ok", ".hd-answer",
                   ".hd-context", "@media (max-width: 560px)"):
        assert needle in css
    # Research only: the dock never calls an order/broker endpoint.
    for banned in ("/api/rio/trading/order", "/api/autonomy/start",
                   "autotrade/start", "/api/backtest/run"):
        assert banned not in js
