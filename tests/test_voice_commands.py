"""
tests/test_voice_commands.py — Comprehensive Voice Command Testing Framework
Unit tests, integration tests, and benchmarking for voice command system.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pytest

from core.voice_router import get_voice_router, VoiceIntent
from core.voice_session import get_session_manager, VoiceSession, VoiceTurn
from core.voice_auth import get_voice_authenticator, AuthResult
from core.voice_scheduler import get_voice_scheduler
from core.voice_analytics import get_voice_analytics, VoiceEvent
from core.voice_offline import get_offline_recognizer, OfflineIntent
from core.voice_wake_word import get_wake_word_trainer, get_wake_word_detector

logger = logging.getLogger(__name__)


# =============================================================================
# Test Data Structures
# =============================================================================

@dataclass
class VoiceTestCase:
    """A single voice command test case."""
    name: str
    transcript: str
    expected_intent: str
    expected_action: str | None = None
    expected_params: dict[str, Any] | None = None
    min_confidence: float = 0.7
    session_context: dict[str, Any] | None = None
    followup: str | None = None
    followup_expected_params: dict[str, Any] | None = None


@dataclass
class VoiceTestSuite:
    """Collection of voice test cases."""
    name: str
    cases: list[VoiceTestCase] = field(default_factory=list)
    setup_fn: Callable | None = None
    teardown_fn: Callable | None = None

    def add_case(self, case: VoiceTestCase) -> "VoiceTestSuite":
        self.cases.append(case)
        return self


@dataclass
class TestResult:
    """Result of a single test case."""
    case_name: str
    passed: bool
    intent_match: bool
    action_match: bool
    params_match: bool
    confidence_ok: bool
    actual_intent: str
    actual_action: str | None
    actual_params: dict
    actual_confidence: float
    error: str | None = None
    latency_ms: float = 0.0


@dataclass
class SuiteResult:
    """Result of a test suite run."""
    suite_name: str
    total: int
    passed: int
    failed: int
    results: list[TestResult]
    total_latency_ms: float
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# =============================================================================
# Predefined Test Suites
# =============================================================================

def create_basic_suite() -> VoiceTestSuite:
    """Create basic intent recognition test suite."""
    suite = VoiceTestSuite("Basic Intent Recognition")
    cases = [
        VoiceTestCase("media_play_simple", "play jazz", "media.play", "music_player"),
        VoiceTestCase("media_play_with_source", "play rock on spotify", "media.play", "music_player"),
        VoiceTestCase("media_pause", "pause music", "media.pause", "music_player"),
        VoiceTestCase("media_next", "next song", "media.next", "music_player"),
        VoiceTestCase("media_previous", "previous track", "media.previous", "music_player"),
        VoiceTestCase("media_volume_up", "volume up", "system.volume", "computer_settings"),
        VoiceTestCase("media_volume_down", "turn down the volume", "system.volume", "computer_settings"),
        VoiceTestCase("system_volume_specific", "set volume to 50", "system.volume", "computer_settings"),
        VoiceTestCase("system_brightness", "brightness up", "system.brightness", "computer_settings"),
        VoiceTestCase("system_wifi_on", "turn on wifi", "system.wifi", "computer_settings"),
        VoiceTestCase("system_sleep", "go to sleep", "system.sleep", "computer_settings"),
        VoiceTestCase("system_shutdown", "shutdown", "system.shutdown", "computer_settings"),
        VoiceTestCase("timer_set", "set timer for 5 minutes", "timer.set", "timer"),
        VoiceTestCase("alarm_set", "set alarm for 7 am", "alarm.set", "alarm"),
        VoiceTestCase("weather_query", "what's the weather", "info.weather", "web_search"),
        VoiceTestCase("time_query", "what time is it", "info.time", "web_search"),
        VoiceTestCase("search_web", "search for python tutorials", "search.web", "web_search"),
        VoiceTestCase("calendar_today", "what's on my calendar today", "calendar.today", "calendar_control"),
        VoiceTestCase("reminder_set", "remind me to call mom at 5 pm", "reminder.set", "calendar_control"),
    ]
    for case in cases:
        suite.add_case(case)
    return suite


def create_macro_suite() -> VoiceTestSuite:
    """Create macro recognition test suite."""
    suite = VoiceTestSuite("Macro Recognition")
    cases = [
        VoiceTestCase("good_morning", "good morning", "macro.good_morning", "macro"),
        VoiceTestCase("good_night", "good night", "macro.good_night", "macro"),
        VoiceTestCase("start_work", "start work", "macro.start_work", "macro"),
        VoiceTestCase("end_work", "end work", "macro.end_work", "macro"),
        VoiceTestCase("workout_start", "start workout", "macro.workout_start", "macro"),
        VoiceTestCase("movie_night", "movie night", "macro.movie_night", "macro"),
    ]
    for case in cases:
        suite.add_case(case)
    return suite


def create_followup_suite() -> VoiceTestSuite:
    """Create follow-up resolution test suite."""
    suite = VoiceTestSuite("Follow-up Resolution")

    def setup(session: VoiceSession):
        # Add a turn with search results
        turn = VoiceTurn(
            timestamp=time.time(),
            transcript="search for jazz songs",
            intent="search.web",
            entities={"query": "jazz songs"},
            action="web_search",
            params={"query": "jazz songs"},
            result={
                "success": True,
                "results": [
                    {"title": "Song 1", "artist": "Artist A"},
                    {"title": "Song 2", "artist": "Artist B"},
                    {"title": "Song 3", "artist": "Artist C"}
                ]
            },
            success=True
        )
        session.add_turn(turn)

    suite.setup_fn = setup

    cases = [
        VoiceTestCase(
            "followup_again",
            "play it again",
            "media.play",
            "music_player",
            session_context={"last_intent": "search.web", "last_result": {"results": [{"title": "Song 1"}]}},
            followup="play it again",
            followup_expected_params={"query": "jazz songs"}
        ),
        VoiceTestCase(
            "followup_second",
            "play the second one",
            "media.play",
            "music_player",
            session_context={},
            followup="play the second one",
            followup_expected_params={"selected_index": 1}
        ),
        VoiceTestCase(
            "followup_other",
            "play another one",
            "media.play",
            "music_player",
            session_context={},
            followup="play another one",
            followup_expected_params={"selected_index": 2}
        ),
    ]
    for case in cases:
        suite.add_case(case)
    return suite


def create_offline_suite() -> VoiceTestSuite:
    """Create offline recognizer test suite."""
    suite = VoiceTestSuite("Offline Recognizer")
    cases = [
        VoiceTestCase("offline_play", "play jazz", "media.play", "music_player"),
        VoiceTestCase("offline_pause", "pause", "media.pause", "music_player"),
        VoiceTestCase("offline_system_volume", "turn up the volume", "system.volume", "computer_settings"),
        VoiceTestCase("offline_timer", "set timer for 5 minutes", "timer.set", "timer"),
        VoiceTestCase("offline_time", "what time is it", "info.time", "web_search"),
        VoiceTestCase("offline_macro_morning", "good morning", "macro.good_morning", "macro"),
        VoiceTestCase("offline_scheduler_add", "schedule play jazz every day at 9am", "scheduler.add", "voice_command"),
        VoiceTestCase("offline_scheduler_list", "list schedules", "scheduler.list", "voice_command"),
    ]
    for case in cases:
        suite.add_case(case)
    return suite


def create_scheduler_suite() -> VoiceTestSuite:
    """Create scheduler test suite."""
    suite = VoiceTestSuite("Voice Scheduler")
    cases = [
        VoiceTestCase(
            "scheduler_add",
            "schedule play jazz every day at 9am",
            "scheduler.add",
            "voice_command",
            expected_params={"command": "play jazz", "schedule": True}
        ),
        VoiceTestCase(
            "scheduler_remove",
            "remove schedule morning_music",
            "scheduler.remove",
            "voice_command",
            expected_params={"job_name": "morning_music", "remove": True}
        ),
        VoiceTestCase(
            "scheduler_list",
            "list schedules",
            "scheduler.list",
            "voice_command",
            expected_params={"list": True}
        ),
    ]
    for case in cases:
        suite.add_case(case)
    return suite


ALL_SUITES = [
    create_basic_suite(),
    create_macro_suite(),
    create_followup_suite(),
    create_offline_suite(),
    create_scheduler_suite(),
]


# =============================================================================
# Test Runner
# =============================================================================

class VoiceTestRunner:
    """Runs voice command test suites."""

    def __init__(self):
        self.router = get_voice_router()
        self.session_mgr = get_session_manager()
        self.offline = get_offline_recognizer()

    def run_case(self, case: VoiceTestCase, use_offline: bool = False) -> TestResult:
        """Run a single test case."""
        start = time.time()

        # Setup session if needed
        session = None
        if case.session_context or case.followup:
            session = self.session_mgr.get_session(f"test_{case.name}")
            if case.session_context:
                # Manually set up session context
                if "last_intent" in case.session_context:
                    session.last_intent = case.session_context["last_intent"]
                if "last_result" in case.session_context:
                    session.last_result = case.session_context["last_result"]
                if "last_params" in case.session_context:
                    session.last_params = case.session_context["last_params"]

        # Parse intent
        router = self.offline if use_offline else self.router
        intent = router.recognize(case.transcript) if use_offline else router.parse(case.transcript)

        # Check followup if specified
        if case.followup and session:
            followup_intent = router.recognize(case.followup) if use_offline else router.parse(case.followup)
            followup_intent.params = session.resolve_followup(case.followup, followup_intent.params)
            intent = followup_intent

        latency_ms = (time.time() - start) * 1000

        # Evaluate results
        intent_match = intent.intent == case.expected_intent
        action_match = (intent.action == case.expected_action) if case.expected_action else True

        params_match = True
        if case.expected_params:
            for key, expected_val in case.expected_params.items():
                actual_val = intent.params.get(key)
                if actual_val != expected_val:
                    params_match = False
                    break

        confidence_ok = intent.confidence >= case.min_confidence

        passed = intent_match and action_match and params_match and confidence_ok

        return TestResult(
            case_name=case.name,
            passed=passed,
            intent_match=intent_match,
            action_match=action_match,
            params_match=params_match,
            confidence_ok=confidence_ok,
            actual_intent=intent.intent,
            actual_action=intent.action,
            actual_params=intent.params,
            actual_confidence=intent.confidence,
            error=None if passed else self._generate_error_msg(case, intent),
            latency_ms=latency_ms
        )

    def _generate_error_msg(self, case: VoiceTestCase, intent: VoiceIntent) -> str:
        errors = []
        if intent.intent != case.expected_intent:
            errors.append(f"Intent: expected '{case.expected_intent}', got '{intent.intent}'")
        if case.expected_action and intent.action != case.expected_action:
            errors.append(f"Action: expected '{case.expected_action}', got '{intent.action}'")
        if case.expected_params:
            for key, expected_val in case.expected_params.items():
                actual_val = intent.params.get(key)
                if actual_val != expected_val:
                    errors.append(f"Param '{key}': expected '{expected_val}', got '{actual_val}'")
        if intent.confidence < case.min_confidence:
            errors.append(f"Confidence: expected >= {case.min_confidence}, got {intent.confidence:.2f}")
        return "; ".join(errors)

    def run_suite(self, suite: VoiceTestSuite, use_offline: bool = False) -> SuiteResult:
        """Run a complete test suite."""
        results = []
        total_latency = 0.0

        # Run setup if provided
        if suite.setup_fn:
            session = self.session_mgr.get_session(f"test_{suite.name}_setup")
            suite.setup_fn(session)

        for case in suite.cases:
            result = self.run_case(case, use_offline)
            results.append(result)
            total_latency += result.latency_ms

        # Run teardown if provided
        if suite.teardown_fn:
            suite.teardown_fn()

        passed = sum(1 for r in results if r.passed)
        return SuiteResult(
            suite_name=suite.name,
            total=len(results),
            passed=passed,
            failed=len(results) - passed,
            results=results,
            total_latency_ms=total_latency
        )

    def run_all(self, use_offline: bool = False) -> list[SuiteResult]:
        """Run all test suites."""
        return [self.run_suite(suite, use_offline) for suite in ALL_SUITES]


# =============================================================================
# Pytest Integration
# =============================================================================

@pytest.fixture
def voice_runner():
    """Provide a voice test runner."""
    return VoiceTestRunner()


@pytest.mark.parametrize("suite", ALL_SUITES)
def test_voice_suite_online(voice_runner: VoiceTestRunner, suite: VoiceTestSuite):
    """Test each suite with online router."""
    result = voice_runner.run_suite(suite, use_offline=False)
    assert result.failed == 0, f"Suite '{suite.name}' failed: {result.failed}/{result.total} tests failed"


@pytest.mark.parametrize("suite", ALL_SUITES)
def test_voice_suite_offline(voice_runner: VoiceTestRunner, suite: VoiceTestSuite):
    """Test each suite with offline recognizer."""
    result = voice_runner.run_suite(suite, use_offline=True)
    assert result.failed == 0, f"Offline suite '{suite.name}' failed: {result.failed}/{result.total} tests failed"


def test_voice_router_confidence(voice_runner: VoiceTestRunner):
    """Test that confidence scores are reasonable."""
    test_cases = [
        ("play jazz", 0.8),
        ("pause", 0.7),
        ("what time is it", 0.7),
        ("good morning", 0.9),
    ]
    for transcript, min_conf in test_cases:
        intent = voice_runner.router.parse(transcript)
        assert intent.confidence >= min_conf, f"Low confidence for '{transcript}': {intent.confidence}"


def test_voice_router_fallback(voice_runner: VoiceTestRunner):
    """Test that unknown commands get 'unknown' intent."""
    intent = voice_runner.router.parse("xyzabc completely unknown command")
    assert intent.intent == "unknown"
    assert intent.confidence == 0.0


def test_offline_recognizer_priority(voice_runner: VoiceTestRunner):
    """Test that offline recognizer respects priority order."""
    # "start work" should match macro, not media.play
    intent = voice_runner.offline.recognize("start work")
    assert intent.intent == "macro.start_work"

    # "schedule play jazz every day" should match scheduler, not media.play
    intent = voice_runner.offline.recognize("schedule play jazz every day at 9am")
    assert intent.intent == "scheduler.add"


def test_voice_session_followup(voice_runner: VoiceTestRunner):
    """Test follow-up resolution in voice session."""
    session = voice_runner.session_mgr.get_session("test_followup")

    # Add a search result
    from core.voice_session import VoiceTurn
    turn = VoiceTurn(
        timestamp=time.time(),
        transcript="search for songs",
        intent="search.web",
        entities={"query": "songs"},
        action="web_search",
        params={"query": "songs"},
        result={"success": True, "results": [{"title": "A"}, {"title": "B"}, {"title": "C"}]},
        success=True
    )
    session.add_turn(turn)

    # "play the second one"
    intent = voice_runner.router.parse("play the second one")
    resolved = session.resolve_followup("play the second one", intent.params)
    assert resolved.get("selected_index") == 1
    assert resolved.get("selected_item") == {"title": "B"}

    # "play another one" -> should cycle to third
    intent = voice_runner.router.parse("play another one")
    resolved = session.resolve_followup("play another one", intent.params)
    assert resolved.get("selected_index") == 2


# =============================================================================
# Benchmarking
# =============================================================================

def benchmark_voice_router(runner: VoiceTestRunner, iterations: int = 1000) -> dict[str, float]:
    """Benchmark voice router performance."""
    test_commands = [
        "play jazz music",
        "pause",
        "turn up the volume",
        "set timer for 5 minutes",
        "what's the weather",
        "good morning",
        "schedule play jazz every day at 9am",
    ]

    latencies = []
    for _ in range(iterations):
        for cmd in test_commands:
            start = time.time()
            runner.router.parse(cmd)
            latencies.append((time.time() - start) * 1000)

    return {
        "mean_ms": sum(latencies) / len(latencies),
        "median_ms": sorted(latencies)[len(latencies) // 2],
        "p95_ms": sorted(latencies)[int(len(latencies) * 0.95)],
        "p99_ms": sorted(latencies)[int(len(latencies) * 0.99)],
        "max_ms": max(latencies),
        "total_commands": len(latencies)
    }


def benchmark_offline_recognizer(runner: VoiceTestRunner, iterations: int = 1000) -> dict[str, float]:
    """Benchmark offline recognizer performance."""
    test_commands = [
        "play jazz music",
        "pause",
        "turn up the volume",
        "set timer for 5 minutes",
        "what's the time",
        "good morning",
        "schedule play jazz every day at 9am",
    ]

    latencies = []
    for _ in range(iterations):
        for cmd in test_commands:
            start = time.time()
            runner.offline.recognize(cmd)
            latencies.append((time.time() - start) * 1000)

    return {
        "mean_ms": sum(latencies) / len(latencies),
        "median_ms": sorted(latencies)[len(latencies) // 2],
        "p95_ms": sorted(latencies)[int(len(latencies) * 0.95)],
        "p99_ms": sorted(latencies)[int(len(latencies) * 0.99)],
        "max_ms": max(latencies),
        "total_commands": len(latencies)
    }


# =============================================================================
# Main Entry Point
# =============================================================================

def run_all_tests(use_offline: bool = False, verbose: bool = True) -> list[SuiteResult]:
    """Run all voice command tests and print results."""
    runner = VoiceTestRunner()
    suite_results = runner.run_all(use_offline)

    if verbose:
        print("\n" + "=" * 60)
        print(f"VOICE COMMAND TEST RESULTS ({'OFFLINE' if use_offline else 'ONLINE'})")
        print("=" * 60)

        for suite_result in suite_results:
            status = "✓ PASS" if suite_result.failed == 0 else "✗ FAIL"
            print(f"\n{suite_result.suite_name}: {status}")
            print(f"  Total: {suite_result.total}, Passed: {suite_result.passed}, Failed: {suite_result.failed}")
            print(f"  Total Latency: {suite_result.total_latency_ms:.2f}ms")

            for result in suite_result.results:
                if not result.passed:
                    print(f"  ✗ {result.case_name}: {result.error}")
                elif verbose > 1:
                    print(f"  ✓ {result.case_name} ({result.latency_ms:.1f}ms)")

        # Overall summary
        total_tests = sum(r.total for r in suite_results)
        total_passed = sum(r.passed for r in suite_results)
        total_failed = sum(r.failed for r in suite_results)
        total_latency = sum(r.total_latency_ms for r in suite_results)

        print("\n" + "=" * 60)
        print(f"OVERALL: {total_passed}/{total_tests} passed ({total_failed} failed)")
        print(f"Total Latency: {total_latency:.2f}ms")
        print("=" * 60)

    return suite_results


if __name__ == "__main__":
    # Run online tests
    print("Running ONLINE tests...")
    online_results = run_all_tests(use_offline=False)

    # Run offline tests
    print("\nRunning OFFLINE tests...")
    offline_results = run_all_tests(use_offline=True)

    # Benchmark
    print("\nBenchmarking...")
    runner = VoiceTestRunner()
    online_bench = benchmark_voice_router(runner)
    offline_bench = benchmark_offline_recognizer(runner)

    print(f"\nOnline Router: {online_bench['mean_ms']:.2f}ms avg, {online_bench['p99_ms']:.2f}ms p99")
    print(f"Offline Recognizer: {offline_bench['mean_ms']:.2f}ms avg, {offline_bench['p99_ms']:.2f}ms p99")

    # Exit with error if any tests failed
    total_failed = sum(r.failed for r in online_results + offline_results)
    exit(1 if total_failed > 0 else 0)