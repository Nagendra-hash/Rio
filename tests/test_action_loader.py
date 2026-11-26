"""tests/test_action_loader.py — pin the canonical action set.

The action loader discovers every `actions/*.py` that declares a module-level
TOOL dict. Two things must stay true as the Mark-LVII port copies are removed:

  1. The canonical maintained actions are all discovered (no silent loss).
  2. No Mark-LVII_* copy shadows a maintained action (no name collision).

Run after any change to actions/ to catch regressions.
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.action_loader import discover_actions

ACTIONS_DIR = BASE / "actions"

# The 63 maintained action TOOL names (canonical set, post-Mark-LVII-removal).
# If you add a new action, add its name here. If you remove one, remove it here
# and explain why in the commit message.
CANONICAL_ACTIONS = {
    "android_control", "astrology_reading", "background_tasks", "backtest_agent",
    "binance_analyzer", "browser_agent", "browser_control", "browser_use_relogin",
    "browser_use_task", "calendar_control", "camera_status", "code_helper",
    "computer_control", "computer_settings", "delta_exchange", "desktop_control",
    "dev_agent", "engine_installer", "file_controller", "file_processor",
    "flight_finder", "game_updater", "gmail_control", "google_drive",
    "hermes_health", "hermes_skills", "hermes_task", "hermes_telegram",
    "knowledge_base", "liquidity_sweep", "media_play", "model_control",
    "music_player", "open_app", "palm_reading", "paper_trading",
    "paper_trading_bridge", "phone_device", "pipeline", "price_levels", "reminder",
    "replica_agent", "repo_agent", "routines", "run_agent", "scientific_agent",
    "send_message", "six_agent_desk", "skill_manager",
    "trader_research", "trading_agents", "trading_monitor",
    "video_creator", "video_director", "video_player", "video_rag",
    "voice_command", "weather_report", "web_search", "web_search_install",
    "youtube_agent", "youtube_transcribe", "youtube_video",
}

# Maintained action files that have no TOOL dict (helpers, not skills).
HELPER_FILES = {
    "background_monitor.py", "camera_feed.py", "news_briefing.py",
    "news_feed.py", "proactive.py", "screen_processor.py", "system_monitor.py",
    "_google_auth.py",
}


def test_discovery_count():
    reg = discover_actions(ACTIONS_DIR, logger=lambda *_: None)
    reg = reg
    assert len(reg.names()) == len(CANONICAL_ACTIONS), (
        f"expected {len(CANONICAL_ACTIONS)} actions, got {len(reg.names())}: "
        f"{sorted(set(CANONICAL_ACTIONS) ^ set(reg.names()))}"
    )
    print(f"✓ discovery: {len(reg.names())} canonical actions found")


def test_canonical_actions_all_present():
    reg = discover_actions(ACTIONS_DIR, logger=lambda *_: None)
    reg = reg
    names = set(reg.names())
    missing = CANONICAL_ACTIONS - names
    assert not missing, f"missing canonical actions: {sorted(missing)}"
    print(f"✓ all {len(CANONICAL_ACTIONS)} canonical actions present")


def test_no_mark_lvii_shadowing():
    reg = discover_actions(ACTIONS_DIR, logger=lambda *_: None)
    reg = reg
    for r in reg._all_records:
        assert "Mark-LVII" not in r.file, (
            f"Mark-LVII file {r.file} still in actions/ — it may shadow a "
            f"maintained action"
        )
    print("✓ no Mark-LVII files shadowing maintained actions")


def test_no_name_collisions():
    reg = discover_actions(ACTIONS_DIR, logger=lambda *_: None)
    reg = reg
    # Every valid record should have a unique name
    names = [r.name for r in reg._all_records if r.valid]
    assert len(names) == len(set(names)), f"duplicate names: {names}"
    print(f"✓ no name collisions among {len(names)} valid records")


def test_malformed_tool_rejected():
    """A file with a malformed TOOL dict must be rejected, not crash discovery."""
    import tempfile, os
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "bad_action.py"
        p.write_text('TOOL = {"name": "bad", "description": "", "parameters": {}, "handler": 42}\n')
        r = discover_actions(Path(d), logger=lambda *_: None)
        assert "bad" not in r.names(), "malformed TOOL was accepted"
    print("✓ malformed TOOL rejected cleanly")


def test_handlers_are_callable():
    reg = discover_actions(ACTIONS_DIR, logger=lambda *_: None)
    reg = reg
    for r in reg._all_records:
        if r.valid:
            assert callable(r.handler), f"{r.name} handler not callable"
    print(f"✓ all {len(reg.names())} handlers callable")


if __name__ == "__main__":
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"✗ {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
