"""
core/voice_offline.py — Offline Voice Fallback
Local intent recognition for when cloud services are unavailable.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class OfflineIntent:
    """Result of offline intent recognition."""
    intent: str
    action: str | None
    params: dict[str, Any]
    confidence: float
    matched_pattern: str | None = None


@dataclass
class IntentResult:
    """Result of intent recognition (test-compatible interface)."""
    intent: str
    confidence: float
    entities: dict[str, Any]
    raw_text: str


def _offline_intent_to_result(offline_intent: OfflineIntent, raw_text: str) -> IntentResult:
    """Convert OfflineIntent to IntentResult for test compatibility."""
    return IntentResult(
        intent=offline_intent.intent,
        confidence=offline_intent.confidence,
        entities=offline_intent.params,
        raw_text=raw_text
    )


# Extract patterns dict for external access (test compatibility)
INTENT_PATTERNS = {}
# Will be populated after OfflineRecognizer class definition


class OfflineRecognizer:
    """
    Local intent recognizer using pattern matching.
    Works without internet/cloud connectivity.
    """

    def __init__(self, model_dir: str = "models/voice_offline"):
        self.model_dir = Path(model_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)

        # Ordered patterns for offline use (priority order - first match wins)
        self.pattern_list = [
            # Macro execution (highest priority - exact phrases with word boundaries)
            ("macro.good_morning", {
                "patterns": [r"\bgood\s+morning\b", r"^morning$", r"\bstart\s+the\s+day\b"],
                "action": "macro",
                "params": lambda m: {"macro": "good_morning"}
            }),
            ("macro.good_night", {
                "patterns": [r"\bgood\s+night\b", r"^night$", r"\bsleep\s+mode\b"],
                "action": "macro",
                "params": lambda m: {"macro": "good_night"}
            }),
            ("macro.start_work", {
                "patterns": [r"\bstart\s+work\b", r"\bbegin\s+work\b", r"\bwork\s+mode\b", r"\bfocus\s+mode\b"],
                "action": "macro",
                "params": lambda m: {"macro": "start_work"}
            }),
            ("macro.end_work", {
                "patterns": [r"\bend\s+work\b", r"\bfinish\s+work\b", r"\bdone\s+working\b"],
                "action": "macro",
                "params": lambda m: {"macro": "end_work"}
            }),
            ("macro.workout_start", {
                "patterns": [r"\bstart\s+workout\b", r"\bbegin\s+exercise\b", r"\bworkout\s+time\b"],
                "action": "macro",
                "params": lambda m: {"macro": "workout_start"}
            }),
            ("macro.movie_night", {
                "patterns": [r"\bmovie\s+night\b", r"\bfilm\s+night\b", r"\bwatch\s+movie\b"],
                "action": "macro",
                "params": lambda m: {"macro": "movie_night"}
            }),

            # Scheduler (before media/app to avoid conflicts)
            ("scheduler.add", {
                "patterns": [
                    r"schedule\s+(.+?)\s+(?:every|daily|weekly|monthly|at\s+\d+:\d+|at\s+\d+\s*(?:am|pm))",
                    r"add\s+schedule\s+(.+?)\s+(?:every|daily|weekly|monthly|at\s+\d+:\d+|at\s+\d+\s*(?:am|pm))",
                ],
                "action": "voice_command",
                "params": lambda m: {"command": m.group(1).strip(), "schedule": True}
            }),
            ("scheduler.remove", {
                "patterns": [
                    r"remove\s+schedule\s+(.+)",
                    r"delete\s+schedule\s+(.+)",
                    r"cancel\s+schedule\s+(.+)",
                ],
                "action": "voice_command",
                "params": lambda m: {"job_name": m.group(1).strip(), "remove": True}
            }),
            ("scheduler.list", {
                "patterns": [
                    r"list\s+schedules?",
                    r"show\s+schedules?",
                    r"what\s+(?:schedules|jobs)\s+(?:are\s+)?(?:scheduled|running)",
                ],
                "action": "voice_command",
                "params": lambda m: {"list": True}
            }),
            ("scheduler.enable", {
                "patterns": [
                    r"enable\s+schedule\s+(.+)",
                    r"activate\s+schedule\s+(.+)",
                    r"turn\s+on\s+schedule\s+(.+)",
                ],
                "action": "voice_command",
                "params": lambda m: {"job_name": m.group(1).strip(), "enable": True}
            }),
            ("scheduler.disable", {
                "patterns": [
                    r"disable\s+schedule\s+(.+)",
                    r"deactivate\s+schedule\s+(.+)",
                    r"turn\s+off\s+schedule\s+(.+)",
                ],
                "action": "voice_command",
                "params": lambda m: {"job_name": m.group(1).strip(), "disable": True}
            }),

            # System control
            ("system.volume", {
                "patterns": [
                    r"system\s+volume\s+(?:up|down|(\d+))",
                    r"(?:turn\s+)?(?:up|down)\s+(?:the\s+)?(?:system\s+)?volume",
                    r"volume\s+(?:up|down)",
                    r"(?:set\s+)?volume\s+to\s+(\d+)",
                ],
                "action": "computer_settings",
                "params": lambda m: {
                    "setting": "volume",
                    "value": int(m.group(1)) if (m.lastindex or 0) >= 1 and m.group(1) and m.group(1).isdigit() else None,
                    "operation": "set" if (m.lastindex or 0) >= 1 and m.group(1) else ("increase" if "up" in m.group(0).lower() else "decrease")
                }
            }),
            ("system.brightness", {
                "patterns": [
                    r"brightness\s+(?:up|down|(\d+))",
                    r"(?:turn\s+)?(?:up|down)\s+brightness",
                ],
                "action": "computer_settings",
                "params": lambda m: {
                    "setting": "brightness",
                    "value": int(m.group(1)) if m.group(1) and m.group(1).isdigit() else None,
                    "operation": "set" if m.group(1) else ("increase" if "up" in m.group(0) else "decrease")
                }
            }),
            ("system.wifi", {
                "patterns": [
                    r"(?:turn\s+)?(?:on|off)\s+wifi",
                    r"(?:enable|disable)\s+wifi",
                ],
                "action": "computer_settings",
                "params": lambda m: {
                    "setting": "wifi",
                    "value": "on" in m.group(0).lower() or "enable" in m.group(0).lower()
                }
            }),
            ("system.sleep", {
                "patterns": [r"(?:go\s+to\s+)?sleep", r"suspend", r"hibernate"],
                "action": "computer_settings",
                "params": lambda m: {"setting": "sleep", "value": True}
            }),
            ("system.shutdown", {
                "patterns": [r"shutdown", r"power\s+off", r"turn\s+off\s+(?:computer|pc)"],
                "action": "computer_settings",
                "params": lambda m: {"setting": "shutdown", "value": True}
            }),

            # Timers and alarms
            ("timer.set", {
                "patterns": [
                    r"set\s+(?:a\s+)?timer\s+(?:for\s+)?(.+)",
                    r"timer\s+(.+)",
                ],
                "action": "timer",
                "params": lambda m: {"duration": m.group(1).strip()}
            }),
            ("timer.cancel", {
                "patterns": [r"cancel\s+timer", r"stop\s+timer"],
                "action": "timer",
                "params": lambda m: {"command": "cancel"}
            }),
            ("alarm.set", {
                "patterns": [
                    r"set\s+(?:an\s+)?alarm\s+(?:for\s+)?(.+)",
                    r"alarm\s+(.+)",
                ],
                "action": "alarm",
                "params": lambda m: {"time": m.group(1).strip()}
            }),

            # Basic queries
            ("info.time", {
                "patterns": [r"what'?s\s+the\s+time", r"current\s+time", r"time\s+now", r"what\s+time\s+is\s+it", r"what\s+is\s+the\s+time"],
                "action": "web_search",
                "params": lambda m: {"query": "current time", "source": "time"}
            }),
            ("info.date", {
                "patterns": [r"what'?s\s+the\s+date", r"today'?s\s+date", r"date\s+today"],
                "action": "web_search",
                "params": lambda m: {"query": "today's date", "source": "date"}
            }),
            ("info.weather", {
                "patterns": [r"what'?s\s+the\s+weather", r"weather\s*(?:in\s+(.+))?$", r"how'?s\s+the\s+weather"],
                "action": "web_search",
                "params": lambda m: {"query": f"weather {m.group(1)}" if (m.lastindex or 0) >= 1 and m.group(1) else "weather", "source": "weather"}
            }),

            # Calendar & reminders
            ("calendar.today", {
                "patterns": [
                    r"what'?s\s+(?:on\s+)?(?:my\s+)?calendar\s+(?:today|today'?s?)",
                    r"today'?s\s+(?:schedule|appointments|events)",
                ],
                "action": "calendar_control",
                "params": lambda m: {"command": "today"}
            }),
            ("calendar.add", {
                "patterns": [
                    r"(?:add|schedule|create)\s+(?:event|meeting|appointment)\s+(.+?)(?:\s+(?:at|on)\s+(.+))?$",
                ],
                "action": "calendar_control",
                "params": lambda m: {"command": "add", "title": m.group(1).strip(), "time": m.group(2).strip() if m.group(2) else None}
            }),
            ("reminder.set", {
                "patterns": [
                    r"remind\s+me\s+(?:to\s+)?(.+?)(?:\s+(?:at|in|on)\s+(.+))?$",
                ],
                "action": "calendar_control",
                "params": lambda m: {"command": "remind", "text": m.group(1).strip(), "when": m.group(2).strip() if m.group(2) else None}
            }),

            # Web search
            ("search.web", {
                "patterns": [
                    r"(?:search|google|look\s+up)\s+(?:for\s+)?(.+)",
                    r"what\s+is\s+(.+)",
                    r"who\s+is\s+(.+)",
                ],
                "action": "web_search",
                "params": lambda m: {"query": m.group(1).strip()}
            }),

            # Media control
            ("media.play", {
                "patterns": [
                    r"play\s+(.+?)(?:\s+on\s+(\w+))?$",
                    r"start\s+(.+?)(?:\s+on\s+(\w+))?$",
                ],
                "action": "music_player",
                "params": lambda m: {
                    "query": m.group(1).strip(),
                    "source": m.group(2) if m.lastindex >= 2 else "jiosaavn"
                }
            }),
            ("media.pause", {
                "patterns": [
                    r"pause\s+(?:music|song|playback)",
                    r"stop\s+(?:music|song|playback)",
                    r"pause",
                    r"stop\s+the\s+music",
                ],
                "action": "music_player",
                "params": lambda m: {"command": "pause"}
            }),
            ("media.next", {
                "patterns": [r"next\s+(?:song|track)", r"skip"],
                "action": "music_player",
                "params": lambda m: {"command": "next"}
            }),
            ("media.previous", {
                "patterns": [r"previous\s+(?:song|track)", r"go\s+back"],
                "action": "music_player",
                "params": lambda m: {"command": "previous"}
            }),
            ("media.volume", {
                "patterns": [
                    r"music\s+volume\s+(?:up|down|(\d+))",
                    r"volume\s+(?:up|down|(\d+))",
                    r"turn\s+(?:it\s+)?up",
                    r"turn\s+(?:it\s+)?down",
                ],
                "action": "music_player",
                "params": lambda m: {
                    "command": "volume",
                    "level": int(m.group(1)) if (m.lastindex or 0) >= 1 and m.group(1) and m.group(1).isdigit() else None,
                    "operation": "set" if (m.lastindex or 0) >= 1 and m.group(1) else ("increase" if "up" in m.group(0).lower() else "decrease")
                }
            }),

            # App control
            ("app.open", {
                "patterns": [
                    r"open\s+(.+)",
                    r"launch\s+(.+)",
                    r"start\s+app\s+(.+)",
                ],
                "action": "open_app",
                "params": lambda m: {"app": m.group(1).strip()}
            }),
            ("app.close", {
                "patterns": [
                    r"close\s+(.+)",
                    r"quit\s+(.+)",
                    r"exit\s+(.+)",
                ],
                "action": "open_app",
                "params": lambda m: {"app": m.group(1).strip(), "command": "close"}
            }),
        ]

        # Compile patterns for performance (maintain order)
        self._compiled: list[tuple[str, list[tuple[re.Pattern, dict]]]] = []
        for intent, config in self.pattern_list:
            compiled_patterns = []
            for pattern in config["patterns"]:
                try:
                    compiled_patterns.append((re.compile(pattern, re.IGNORECASE), config))
                except re.error as e:
                    logger.error(f"Invalid regex pattern '{pattern}': {e}")
            if compiled_patterns:
                self._compiled.append((intent, compiled_patterns))

    def recognize(self, transcript: str) -> OfflineIntent:
        """
        Recognize intent from transcript using local patterns.

        Args:
            transcript: Voice transcript text

        Returns:
            OfflineIntent with best match
        """
        transcript = transcript.strip().lower()
        if not transcript:
            return OfflineIntent(
                intent="unknown",
                action=None,
                params={},
                confidence=0.0
            )

        # Iterate in priority order (first match wins)
        for intent, compiled_patterns in self._compiled:
            for pattern, config in compiled_patterns:
                match = pattern.search(transcript)
                if match:
                    # Calculate confidence based on match quality
                    # Full match = higher confidence
                    match_len = len(match.group(0))
                    transcript_len = len(transcript)
                    coverage = match_len / transcript_len if transcript_len > 0 else 0

                    # Base confidence on coverage and pattern specificity
                    confidence = min(0.9, 0.5 + coverage * 0.4)

                    params = config["params"](match) if callable(config["params"]) else config["params"]
                    return OfflineIntent(
                        intent=intent,
                        action=config["action"],
                        params=params,
                        confidence=confidence,
                        matched_pattern=pattern.pattern
                    )

        return OfflineIntent(
            intent="unknown",
            action=None,
            params={},
            confidence=0.0
        )

    def add_custom_pattern(
        self,
        intent: str,
        patterns: list[str],
        action: str,
        params: dict | Callable
    ) -> None:
        """Add custom pattern at runtime."""
        if intent not in self.patterns:
            self.patterns[intent] = {"patterns": [], "action": action, "params": params}
            self._compiled[intent] = []

        for pattern in patterns:
            try:
                self.patterns[intent]["patterns"].append(pattern)
                self._compiled[intent].append((re.compile(pattern, re.IGNORECASE), self.patterns[intent]))
            except re.error as e:
                logger.error(f"Invalid regex pattern '{pattern}': {e}")

    def save_patterns(self, file_path: str | None = None) -> None:
        """Save patterns to file."""
        path = Path(file_path) if file_path else self.model_dir / "patterns.json"
        try:
            data = {
                intent: {
                    "patterns": config["patterns"],
                    "action": config["action"],
                    "params": config["params"] if not callable(config["params"]) else "callable"
                }
                for intent, config in self.patterns.items()
            }
            path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error(f"Failed to save patterns: {e}")

    def load_patterns(self, file_path: str | None = None) -> bool:
        """Load patterns from file."""
        path = Path(file_path) if file_path else self.model_dir / "patterns.json"
        if not path.exists():
            return False

        try:
            data = json.loads(path.read_text())
            for intent, config in data.items():
                if intent not in self.patterns:
                    self.patterns[intent] = {"patterns": [], "action": config["action"], "params": config["params"]}
                    self._compiled[intent] = []

                for pattern in config["patterns"]:
                    try:
                        self.patterns[intent]["patterns"].append(pattern)
                        self._compiled[intent].append((re.compile(pattern, re.IGNORECASE), self.patterns[intent]))
                    except re.error as e:
                        logger.error(f"Invalid regex pattern '{pattern}': {e}")

            logger.info(f"Loaded offline patterns from {path}")
            return True
        except Exception as e:
            logger.error(f"Failed to load patterns: {e}")
            return False


# Singleton instance
_offline_recognizer: OfflineRecognizer | None = None


def get_offline_recognizer() -> OfflineRecognizer:
    """Get or create the offline recognizer singleton."""
    global _offline_recognizer
    if _offline_recognizer is None:
        _offline_recognizer = OfflineRecognizer()
    return _offline_recognizer


# Populate INTENT_PATTERNS after class definition
# This needs to be done after OfflineRecognizer is defined
def _populate_intent_patterns():
    """Populate INTENT_PATTERNS dict from the pattern_list."""
    global INTENT_PATTERNS
    # Create a temporary instance to access pattern_list
    temp_recognizer = OfflineRecognizer()
    for intent, config in temp_recognizer.pattern_list:
        INTENT_PATTERNS[intent] = config["patterns"]


# Initialize patterns on module load
_populate_intent_patterns()


def recognize_intent(transcript: str) -> IntentResult:
    """
    Recognize intent from transcript using offline patterns.

    Args:
        transcript: Voice transcript text

    Returns:
        IntentResult with best match (test-compatible)
    """
    recognizer = get_offline_recognizer()
    offline_intent = recognizer.recognize(transcript)
    return _offline_intent_to_result(offline_intent, transcript)


# ── STT/TTS engine factories (for test compatibility) ─────────────────────────


def get_stt_engine():
    """
    Get the best available STT engine.
    Tries Whisper first, falls back to Vosk, returns None if neither available.
    """
    # Try Whisper
    try:
        from faster_whisper import WhisperModel
        from core.stt import WhisperSTT
        return WhisperSTT(model_name="base")
    except Exception:
        pass
    # Try Vosk
    try:
        from vosk import Model as VoskModel
        from core.stt import VoskSTT
        return VoskSTT()
    except Exception:
        pass
    return None


def get_tts_engine():
    """
    Get the best available TTS engine.
    Tries Kokoro first, falls back to SystemTTS.
    """
    # Try Kokoro
    try:
        from kokoro import KPipeline
        from core.tts import KokoroTTSEngine
        return KokoroTTSEngine()
    except Exception:
        pass
    # Fallback to SystemTTS
    try:
        from core.system_tts import SystemTTS
        return SystemTTS()
    except Exception:
        pass
    return None


def process_voice_command(audio_data) -> dict:
    """
    Process a voice command through the full offline pipeline:
    STT → Intent Recognition → Local LLM → TTS

    Args:
        audio_data: Audio data as numpy array (int16 or float32)

    Returns:
        Dict with intent, transcript, response, etc.
    """
    stt = get_stt_engine()
    tts = get_tts_engine()
    recognizer = get_offline_recognizer()

    # Convert to float32 if int16
    if audio_data.dtype == np.int16:
        audio_float = audio_data.astype(np.float32) / 32768.0
    else:
        audio_float = audio_data

    # Transcribe
    transcript = ""
    if stt:
        try:
            transcript = stt.transcribe(audio_float)
        except Exception:
            transcript = ""

    # Recognize intent
    intent_result = recognize_intent(transcript) if transcript else IntentResult(
        intent="unknown", confidence=0.0, entities={}, raw_text=transcript
    )

    # Generate response (placeholder - would use ModelRouter in real implementation)
    response = f"Recognized: {intent_result.intent}"
    if intent_result.intent == "unknown":
        response = "I didn't understand that command."

    # Speak response
    if tts:
        try:
            tts.speak(response)
        except Exception:
            pass

    return {
        "intent": intent_result.intent,
        "confidence": intent_result.confidence,
        "entities": intent_result.entities,
        "transcript": transcript,
        "response": response
    }