"""
tests/test_voice_router.py — Unit tests for voice command router
"""
from __future__ import annotations

import pytest
import tempfile
import yaml
from pathlib import Path

from core.voice_router import get_voice_router, VoiceIntent
from core.voice_session import get_session_manager, VoiceSession


def test_voice_router_basic():
    """Test basic intent parsing."""
    router = get_voice_router()

    # Test media play
    intent = router.parse("play some jazz music")
    assert intent.intent == "media.play"
    assert intent.action == "music_player"
    assert "query" in intent.params
    assert "jazz" in intent.params["query"]

    # Test media pause
    intent = router.parse("pause music")
    assert intent.intent == "media.pause"
    assert intent.params.get("command") == "pause"

    # Test volume up
    intent = router.parse("turn up the volume")
    assert intent.intent == "system.volume"
    assert intent.action == "computer_settings"

    # Test weather
    intent = router.parse("what's the weather")
    assert intent.intent == "info.weather"
    assert intent.action == "web_search"

    # Test timer
    intent = router.parse("set timer for 5 minutes")
    assert intent.intent == "timer.set"
    assert "5 minutes" in intent.params.get("duration", "")

    # Test search
    intent = router.parse("search for python tutorials")
    assert intent.intent == "search.web"
    assert "python tutorials" in intent.params.get("query", "")

    print("✓ Basic intent parsing works")


def test_voice_router_macros():
    """Test macro recognition."""
    # Create temp macro file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump({
            "macros": {
                "test_macro": {
                    "trigger_phrases": ["test macro", "run test"],
                    "steps": [
                        {"action": "music_player", "params": {"command": "play"}}
                    ],
                    "description": "Test macro"
                }
            }
        }, f)
        macro_file = f.name

    try:
        from core.voice_router import VoiceRouter
        router = VoiceRouter(macro_file=macro_file)

        intent = router.parse("run test macro")
        assert intent.intent == "macro.test_macro"
        assert intent.action == "macro"
        assert intent.params.get("macro") == "test_macro"
        print("✓ Macro recognition works")
    finally:
        Path(macro_file).unlink()


def test_voice_session():
    """Test voice session context management."""
    mgr = get_session_manager()
    session = mgr.get_session("test_session_123")

    # Add a turn
    from core.voice_session import VoiceTurn
    import time

    turn = VoiceTurn(
        timestamp=time.time(),
        transcript="play jazz",
        intent="media.play",
        entities={"query": "jazz"},
        action="music_player",
        params={"query": "jazz", "source": "jiosaavn"},
        result={"success": True, "track": "Jazz Vibes"},
        success=True
    )
    session.add_turn(turn)

    # Check context
    assert session.last_intent == "media.play"
    assert session.last_params["query"] == "jazz"

    # Test followup resolution
    resolved = session.resolve_followup("play it again", {"query": "rock"})
    assert resolved["query"] == "jazz"  # Should use last query

    # Test ordinal followup (requires results list in last_result)
    session.last_result = {
        "results": [
            {"title": "Song 1", "artist": "Artist A"},
            {"title": "Song 2", "artist": "Artist B"},
            {"title": "Song 3", "artist": "Artist C"}
        ]
    }
    resolved = session.resolve_followup("the second one", {"query": "test"})
    assert resolved.get("selected_index") == 1

    print("✓ Voice session context works")


def test_voice_session_followup():
    """Test follow-up resolution with results."""
    mgr = get_session_manager()
    session = mgr.get_session("test_session_456")

    # Simulate search result
    session.last_result = {
        "results": [
            {"title": "Song 1", "artist": "Artist A"},
            {"title": "Song 2", "artist": "Artist B"},
            {"title": "Song 3", "artist": "Artist C"}
        ]
    }
    session.last_params = {"query": "jazz"}

    # "the second one"
    resolved = session.resolve_followup("play the second one", {})
    assert resolved.get("selected_index") == 1
    assert resolved.get("selected_item") == {"title": "Song 2", "artist": "Artist B"}

    # "another one" / "different"
    resolved = session.resolve_followup("play another one", {})
    assert resolved.get("selected_index") == 2  # Cycles to next

    print("✓ Follow-up resolution works")


def test_voice_router_entities():
    """Test entity extraction."""
    router = get_voice_router()

    intent = router.parse("play jazz music at 5 pm for 30 minutes")
    assert intent.entities.get("time_mentioned") is not None
    assert intent.entities.get("duration") is not None
    assert 5 in intent.entities.get("numbers", [])

    print("✓ Entity extraction works")


if __name__ == "__main__":
    test_voice_router_basic()
    test_voice_router_macros()
    test_voice_session()
    test_voice_session_followup()
    test_voice_router_entities()
    print("\n✅ All voice router tests passed!")