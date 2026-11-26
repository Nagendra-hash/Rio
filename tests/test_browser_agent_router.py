"""Offline tests for the browser agent's routing decision (tool vs http vs browser)."""

from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.browser_agent import router  # noqa: E402
from core.browser_agent.config import BrowserAgentConfig  # noqa: E402

CFG = BrowserAgentConfig()


def kinds(task: str, mode: str = "auto"):
    return router.plan_task(task, mode=mode, cfg=CFG).kind


def test_brief_examples_use_the_browser():
    for task in [
        "open YouTube and search for Python tutorials",
        "open my GitHub repository and inspect the latest issues",
        "research the latest developments in AI agents and prepare a report",
        "open my dashboard and check whether the AlgoTrade application is running",
        "search three websites and compare their information",
        "download the PDF and summarize it",
        "open Gmail and find the latest unread messages",
        "fill the checkout form",
        "log into my bank and read the balance",
    ]:
        assert kinds(task) == "browser", task


def test_single_public_page_prefers_static_http():
    assert kinds("Read https://example.com and summarize it") == "http"
    assert kinds("extract the text from https://example.com/article") == "http"


def test_plain_url_open_is_delegated_to_browser_control():
    plan = router.plan_task("open example.com", cfg=CFG)
    assert plan.kind == "tool"
    assert "browser_control" in plan.reason


def test_structured_data_uses_existing_tools():
    assert kinds("what is bitcoin trading at") == "tool"
    assert kinds("what's the weather today") == "tool"


def test_research_without_browser_vocabulary_is_not_stolen():
    # A plain research question does not need a browser; RESEARCH keeps it.
    assert kinds("what is the capital of france") == "tool"


def test_explicit_modes_win():
    assert kinds("open example.com", mode="interact") == "browser"
    assert kinds("read https://example.com", mode="extract") == "http"
    assert kinds("summarize this", mode="research") == "browser"


def test_greetings_are_not_web_tasks():
    assert kinds("hi") == "tool"
    assert kinds("thanks!") == "tool"


def test_extract_url_handles_plain_and_bare():
    assert router.extract_url("go to https://example.com/x?y=1.") == "https://example.com/x?y=1"
    assert router.extract_url("check example.org please") == "https://example.org"
    assert router.extract_url("no link here") is None


class _FakeResponse:
    def __init__(self, text: str, status: int = 200, content_type: str = "text/html"):
        self.text = text
        self.status_code = status
        self.headers = {"content-type": content_type}


def test_fetch_static_extracts_readable_text(monkeypatch):
    html = """
    <html><head><title>Demo page</title></head>
    <body><script>ignore()</script><h1>Hello</h1><p>Body text here.</p></body></html>
    """
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(html))
    page = router.fetch_static("https://example.com")
    assert page.usable
    assert page.title == "Demo page"
    assert "Hello" in page.text and "Body text here." in page.text
    assert "ignore()" not in page.text


def test_fetch_static_reports_non_html_and_errors(monkeypatch):
    import requests

    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse("%PDF", content_type="application/pdf")
    )
    page = router.fetch_static("https://example.com/file.pdf")
    assert not page.usable
    assert "not html" in page.error

    monkeypatch.setattr(
        requests, "get", lambda *a, **k: _FakeResponse("nope", status=404)
    )
    page = router.fetch_static("https://example.com/missing")
    assert not page.usable
    assert "404" in page.error


def test_fetch_static_never_raises(monkeypatch):
    import requests

    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(requests, "get", _boom)
    page = router.fetch_static("https://example.com")
    assert not page.usable
    assert "network down" in page.error
