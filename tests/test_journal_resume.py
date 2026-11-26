"""
tests/test_journal_resume.py — the fresh-session resume block.

History (2026-10-17): a JARVIS restart injects the last journal turns into the
system prompt so the conversation continues where it stopped. The journal also
receives unsolicited notices — proactive alerts and monitors — as JARVIS turns.
On the real journal the last eight rows were almost all `[SYSTEM_ALERT]` /
`[MONITOR_ALERT]` lines, so the resume block opened on an alert thread instead
of the user's actual request.

What this proves against the real memory/conversation_journal.py (JOURNAL_DIR
pointed at a tmp dir — no real history touched):

  1. `is_system_notice` recognises the bracketed notice tags and nothing else.
  2. `recent_journal_tail` drops notices, keeps the newest conversational
     turns, and still honours its character budget (newest survives).
  3. `last_user_request` returns the user's newest substantive turn.

Run:  .venv/bin/python -m pytest tests/test_journal_resume.py -q
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import memory.conversation_journal as J  # noqa: E402


def _write_month(tmp: Path, rows: list[dict]) -> None:
    (tmp / "2026-10.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")


def test_is_system_notice():
    assert J.is_system_notice("[SYSTEM_ALERT] CPU usage is very high")
    assert J.is_system_notice("[MONITOR_ALERT] market update")
    assert J.is_system_notice("[EMERGENCY]")
    assert not J.is_system_notice("hello there")
    assert not J.is_system_notice("the log is at [PATH] somewhere")  # mid-text
    assert not J.is_system_notice("[note] a lower-case tag")
    assert not J.is_system_notice("")


def test_tail_drops_notices_and_last_user_request_anchors(tmp_path):
    rows = [
        {"ts": "2026-10-01T10:00:00", "speaker": "user",
         "text": "build me an analog watch app please"},
        {"ts": "2026-10-01T10:01:00", "speaker": "jarvis",
         "text": "Starting on it now."},
        {"ts": "2026-10-01T10:02:00", "speaker": "jarvis",
         "text": "[SYSTEM_ALERT] CPU usage 92%"},
        {"ts": "2026-10-01T10:03:00", "speaker": "user",
         "text": "also add a settings screen"},
        {"ts": "2026-10-01T10:04:00", "speaker": "jarvis",
         "text": "[MONITOR_ALERT] market update"},
        {"ts": "2026-10-01T10:05:00", "speaker": "jarvis",
         "text": "Added the settings screen."},
    ]
    with patch.object(J, "JOURNAL_DIR", tmp_path):
        _write_month(tmp_path, rows)
        tail = J.recent_journal_tail(max_chars=100000)
        assert "analog watch" in tail and "settings screen" in tail
        assert "SYSTEM_ALERT" not in tail and "MONITOR_ALERT" not in tail
        # the anchor is the user's newest request, never a notice
        assert J.last_user_request() == "also add a settings screen"


def test_tail_respects_the_budget_and_keeps_the_newest(tmp_path):
    rows = [{"ts": f"2026-10-01T10:0{i}:00", "speaker": "user",
             "text": f"message number {i} " + "x" * 50} for i in range(6)]
    with patch.object(J, "JOURNAL_DIR", tmp_path):
        _write_month(tmp_path, rows)
        tail = J.recent_journal_tail(max_chars=120)
        assert len(tail) <= 120
        assert "message number 5" in tail        # newest survives the trim
        assert "message number 0" not in tail


def test_empty_journal_is_safe(tmp_path):
    with patch.object(J, "JOURNAL_DIR", tmp_path):
        assert J.recent_journal_tail() == ""
        assert J.last_user_request() == ""


def test_notices_do_not_hide_a_user_request(tmp_path):
    # A user turn sandwiched between notices must still be found.
    rows = [
        {"ts": "2026-10-01T09:00:00", "speaker": "jarvis",
         "text": "[SYSTEM_ALERT] disk almost full"},
        {"ts": "2026-10-01T09:05:00", "speaker": "user",
         "text": "please fix the build error in the watch app"},
        {"ts": "2026-10-01T09:06:00", "speaker": "jarvis",
         "text": "[MONITOR_ALERT] news digest"},
    ]
    with patch.object(J, "JOURNAL_DIR", tmp_path):
        _write_month(tmp_path, rows)
        assert J.last_user_request() == "please fix the build error in the watch app"
        assert "fix the build error" in J.recent_journal_tail()
