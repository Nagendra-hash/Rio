"""Live browser tests for the RIO Browser Agent.

These launch a REAL local Chrome through browser-use 1.0.5 and a REAL model.
They are opt-in and never run by default:

    JARVIS_BROWSER_LIVE=1 .venv/bin/python -m pytest tests/test_browser_agent_live.py -q -m browser_live

Every assertion is about observed evidence (returned text, a file on disk), not
about a call not raising. A test that cannot run is SKIPPED with the reason, and
that is reported separately from a pass.

Most tasks run against a local HTTP server, so the suite needs no internet for
navigation/click/form/download/tabs and can also prove localhost works.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

RUNNER = BASE / "scripts" / "browser_use_run.py"
BROWSER_PY = Path.home() / ".jarvis-browser-venv" / "bin" / "python"

pytestmark = [
    pytest.mark.browser_live,
    pytest.mark.skipif(
        os.environ.get("JARVIS_BROWSER_LIVE") != "1",
        reason="live browser tests are opt-in: set JARVIS_BROWSER_LIVE=1",
    ),
    pytest.mark.skipif(
        not BROWSER_PY.exists(),
        reason=f"browser venv missing: {BROWSER_PY}",
    ),
]


# ── a local site for deterministic page work ────────────────────────────────

_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj
trailer<</Root 1 0 R>>
%%EOF
"""

_PAGES = {
    "/": (
        "<title>RIO Home</title><h1>RIO Test Page</h1>"
        '<p>This page exists to prove the browser agent can read it.</p>'
        '<a href="/second">Second page</a>'
    ),
    "/second": "<title>RIO Second</title><h1>Second Page</h1><p>Arrived by link.</p>",
    "/tab1": "<title>Tab One</title><h1>Tab One</h1>",
    "/tab2": "<title>Tab Two</title><h1>Tab Two</h1>",
    "/form": (
        "<title>RIO Form</title><h1>Contact form</h1>"
        '<form action="/submitted" method="get">'
        '<label for="name">Name</label>'
        '<input id="name" name="name" type="text">'
        '<button id="send" type="submit">Submit</button>'
        "</form>"
    ),
    "/submitted": "<title>Submitted</title><h1>You submitted the form</h1>",
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        return

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/sample.pdf":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(_PDF)))
            self.send_header("Content-Disposition", 'attachment; filename="rio-test.pdf"')
            self.end_headers()
            self.wfile.write(_PDF)
            return
        if path == "/slow":
            time.sleep(8)
            body = "<title>Slow</title><h1>Slow page</h1>"
        else:
            body = _PAGES.get(path)
        if body is None:
            self.send_error(404)
            return
        data = f"<html><body>{body}</body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


_PROFILE_DIR = str(Path.home() / ".local" / "share" / "jarvis-browser-profile" / "bu")
_RUN_LOCK = threading.Lock()


def _chrome_using_profile() -> bool:
    try:
        import psutil

        for proc in psutil.process_iter(["name", "cmdline"]):
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if _PROFILE_DIR in cmdline:
                return True
    except Exception:
        return False
    return False


def _wait_profile_free(timeout: float = 25.0) -> None:
    """Chrome takes a moment to release the profile after a kill."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _chrome_using_profile():
            return
        time.sleep(0.5)


@pytest.fixture(autouse=True)
def _cancel_leftover_jobs():
    """Never let one test's browser hold the shared profile for the next."""
    yield
    try:
        from core.browser_agent import runtime as rt_mod

        rt = rt_mod.shared_runtime()
        for job in rt.active():
            rt.cancel(job["job_id"])
    except Exception:
        pass
    _wait_profile_free()


@pytest.fixture(scope="module")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


# ── runner helper ───────────────────────────────────────────────────────────


def _runner(task: str, tmp_path: Path, *extra: str, timeout: int = 300):
    """Run the real runner; return (exit code, parsed json, stderr)."""
    cmd = [
        str(BROWSER_PY),
        str(RUNNER),
        task,
        "--json",
        "--max-steps", "14",
        "--timeout", "200",
        "--downloads", str(tmp_path / "downloads"),
        *extra,
    ]
    with _RUN_LOCK:
        _wait_profile_free()
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(tmp_path)
        )
    payload = None
    for line in reversed((proc.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except Exception:
                continue
    return proc.returncode, payload, (proc.stderr or "")


def _text(payload: dict) -> str:
    return str((payload or {}).get("final") or "").lower()


# ── 1-7: navigation, reading, clicking, forms, downloads, tabs ──────────────


def test_launch_open_and_extract(site, tmp_path):
    """1-2, 7: launch the browser, open a page, extract its information."""
    code, payload, err = _runner(
        f"Open {site}/ and report the main heading text exactly as written.",
        tmp_path,
        "--no-judge",
    )
    assert code == 0, err[-1500:]
    assert payload and payload["ok"] is True
    assert "rio test page" in _text(payload), payload


def test_navigate_by_link_click(site, tmp_path):
    """4-5: navigate, then click a link to a second page."""
    code, payload, err = _runner(
        f"Open {site}/ then click the 'Second page' link and report the heading "
        "of the page you land on.",
        tmp_path,
        "--no-judge",
    )
    assert code == 0, err[-1500:]
    assert "second page" in _text(payload), payload


def test_fill_form_without_submitting(site, tmp_path):
    """6: fill a test form and do NOT submit it."""
    code, payload, err = _runner(
        f"Open {site}/form. Set the value of the input with id 'name' to exactly "
        "'RIO test' (use js() if a normal type does not stick). Do NOT click "
        "Submit. Then read that input's value back and report it.",
        tmp_path,
        "--no-judge",
    )
    assert code == 0, err[-1500:]
    assert "rio test" in _text(payload), payload
    # The form must not have been submitted, and the agent must not claim
    # success it did not have.
    assert "you submitted" not in _text(payload), payload
    assert payload.get("success") is not False, payload


def test_download_pdf(site, tmp_path):
    """8: download a file and verify it exists on disk."""
    code, payload, err = _runner(
        f"Download the PDF at {site}/sample.pdf and confirm the download finished.",
        tmp_path,
        "--no-judge",
    )
    assert code == 0, err[-1500:]
    downloaded = [Path(p) for p in (payload or {}).get("downloads") or []]
    files = [p for p in downloaded if p.exists()]
    if not files:
        files = [p for p in (tmp_path / "downloads").rglob("*") if p.is_file()]
    assert files, f"no downloaded file on disk; payload={payload}"
    assert any(p.suffix.lower() == ".pdf" or p.read_bytes().startswith(b"%PDF") for p in files)


def test_manage_two_tabs(site, tmp_path):
    """9: open two tabs and report both."""
    code, payload, err = _runner(
        f"Open {site}/tab1 in one tab and {site}/tab2 in a second tab, then "
        "report the titles of both tabs.",
        tmp_path,
        "--no-judge",
    )
    assert code == 0, err[-1500:]
    text = _text(payload)
    assert "tab one" in text and "tab two" in text, payload


def test_public_search_engine(tmp_path):
    """3: perform a real search-engine query (needs internet)."""
    try:
        import urllib.request

        urllib.request.urlopen("https://duckduckgo.com", timeout=10)
    except Exception as exc:
        pytest.skip(f"no internet for the search-engine test ({type(exc).__name__})")

    code, payload, err = _runner(
        "Search DuckDuckGo for 'browser automation' and report the title of the "
        "first organic result.",
        tmp_path,
        "--no-judge",
    )
    assert code == 0, err[-1500:]
    assert payload and payload["ok"] is True
    assert len(_text(payload)) > 3, payload


# ── 10-12: timeouts, unavailable models, cancellation ──────────────────────


def test_timeout_is_reported_and_a_later_run_recovers(site, tmp_path):
    """10: a too-tight budget fails honestly; the next run still works."""
    code, payload, err = _runner(
        f"Open {site}/slow and report the heading.",
        tmp_path,
        "--timeout", "1",
        "--no-judge",
    )
    assert code == 1, (code, payload, err[-800:])
    assert payload and payload["ok"] is False
    assert payload["status"] in ("timeout", "max_steps", "error"), payload

    # The recovery run must actually work. The task names the exact read
    # (js on the h1) so a miss is a real failure, not model phrasing variance.
    code2, payload2, err2 = _runner(
        f"Open {site}/ and report the exact text of the h1 element. If the "
        "accessibility tree is unclear, use js('document.querySelector(\"h1\")"
        ".textContent').",
        tmp_path,
        "--no-judge",
    )
    assert code2 == 0, err2[-1200:]
    assert payload2 and payload2["status"] == "completed", payload2
    assert "rio test page" in _text(payload2), payload2


def test_unavailable_model_fails_honestly(tmp_path):
    """11: no usable brain -> a clear failure, never a fake success."""
    code, payload, err = _runner(
        "open example.com",
        tmp_path,
        "--brain", "nvidia",
        "--model", "definitely/not-a-real-model",
        "--no-judge",
        timeout=180,
    )
    assert code == 2, (code, payload, err[-800:])
    assert payload and payload["ok"] is False
    assert "no compatible model" in payload["error"].lower(), payload
    assert "definitely/not-a-real-model" in payload["error"]


def test_cancel_a_running_job(site):
    """12: a running job can be cancelled and stops occupying the registry."""
    from core.browser_agent import runtime as rt_mod

    rt = rt_mod.shared_runtime()
    started = rt.start(
        f"Open {site}/slow and read every paragraph on the page", mode="interact"
    )
    assert started.status == "running", started
    time.sleep(1.0)
    cancelled = rt.cancel(started.job_id)
    assert cancelled.status == "cancelled", cancelled
    assert all(j["job_id"] != started.job_id for j in rt.active())


# ── 13: the voice path must not block ──────────────────────────────────────


class _FakePlayer:
    def __init__(self):
        self.logs: list[str] = []
        self.shown: list[tuple[str, str]] = []
        self.inbound_target = ""

    def write_log(self, text: str):
        self.logs.append(str(text))

    def show_content(self, title: str, body: str):
        self.shown.append((str(title), str(body)))


def test_action_returns_before_the_browser_finishes(site):
    """13: the tool call returns immediately; the result arrives later."""
    from actions.browser_agent import browser_agent

    player = _FakePlayer()
    t0 = time.monotonic()
    reply = browser_agent(
        {"task": f"Open {site}/slow and report the heading", "mode": "interact"},
        player=player,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0, f"tool blocked the caller for {elapsed:.1f}s"
    assert "status=started" in reply, reply

    from core.browser_agent import runtime as rt_mod

    rt = rt_mod.shared_runtime()
    active = rt.active()
    assert active, "the job should be running in the background"
    rt.cancel(active[0]["job_id"])


# ── 14: normal orchestration ───────────────────────────────────────────────


def test_orchestrator_dispatch(site):
    """14: the same task through the orchestrator's own agent pipeline."""
    from orchestrator.contracts import TaskStatus
    from orchestrator.integration import shared_orchestrator

    orch = shared_orchestrator()
    result = orch.submit(
        f"open {site}/ and report the main heading",
        channel="rio_agent_center",
        metadata={"browser_execute": True, "wait_s": 180},
    )
    assert result.agent == "browser_agent", result
    assert result.status is TaskStatus.COMPLETED, result
    assert "rio test page" in result.text.lower(), result.text


# ── 15: Telegram delivery path ─────────────────────────────────────────────


def test_telegram_result_is_pushed_to_the_originating_chat(site, monkeypatch):
    """15: a long result is delivered to the chat that asked.

    The sender is captured (not mocked away) so this proves the plumbing; a
    real Bot API send is a separate, BLOCKED item unless Hermes creds exist.
    """
    from actions import browser_agent as action

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(action, "_send_channel", lambda target, text: sent.append((target, text)) or True)

    player = _FakePlayer()
    player.inbound_target = "telegram:424242"
    # mode="interact" forces the BROWSER path: this test is about the async
    # result being pushed later, not about the static-http fast path (which
    # answers inline and is covered by the runtime/router tests).
    reply = action.run_confirmed(
        f"Open {site}/ and report the main heading", player, None, mode="interact"
    )
    assert "status=started" in reply, reply

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and not sent:
        time.sleep(0.5)
    assert sent, "the result was never pushed to the originating chat"
    target, body = sent[0]
    assert target == "telegram:424242"
    assert "rio test page" in body.lower(), body
