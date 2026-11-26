"""
core/voice_router.py — Voice Command Router
Routes voice intents to skills with confidence scoring and entity extraction.
"""

from __future__ import annotations

import re
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

from core.action_loader import discover_actions

logger = logging.getLogger(__name__)

# Cache for action names
_action_names_cache: set[str] | None = None


def get_action_names() -> set[str]:
    """Get set of all registered action names."""
    global _action_names_cache
    if _action_names_cache is None:
        try:
            # Get actions directory
            actions_dir = Path(__file__).parent.parent / "actions"
            registry = discover_actions(actions_dir)
            _action_names_cache = registry.names()
        except Exception:
            _action_names_cache = set()
    return _action_names_cache


@dataclass
class VoiceIntent:
    """Parsed voice intent with entities."""
    intent: str
    entities: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    raw_text: str = ""
    action: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    offline_fallback: bool = False  # True if resolved via offline recognizer


@dataclass
class VoiceMacro:
    """Voice macro definition."""
    name: str
    trigger_phrases: list[str]
    steps: list[dict[str, Any]]
    description: str = ""


def _model_args(action: str, target: str = "both"):
    """Build the params extractor for the model.* intents.

    The optional trailing model name is captured when a pattern has a group;
    otherwise the action's own preferred local/cloud pick is used.
    """
    def extract(m):
        out: dict[str, Any] = {"action": action, "target": target}
        try:
            name = (m.group(1) or "").strip()
        except (IndexError, AttributeError):
            name = ""
        if name and name.lower() not in ("model", "mode", "local", "cloud", "online"):
            out["model"] = name
        return out

    return extract


class VoiceRouter:
    """Routes voice commands to appropriate skills/macros."""

    # Intent patterns mapped to (action, param_extractors)
    # Order matters: first match wins. More specific patterns should come first.
    INTENT_PATTERNS = {
        # ── Model / engine switching ─────────────────────────────────────────
        # Status comes FIRST: "are you using the local model?" contains the
        # words that would otherwise trigger a switch.
        "model.status": {
            "patterns": [
                r"\b(?:which|what)\s+model\b",
                r"\b(?:are|is)\s+(?:you|jarvis|hermes|it)\s+(?:using|running)\b",
                r"\bmodel\s+status\b",
                r"\bcheck\s+(?:the\s+)?model\b",
                r"\blocal\s+or\s+cloud\b",
                r"\bam\s+i\s+(?:using|on)\s+(?:the\s+)?local\b",
                # Questions about one assistant must not read as a switch
                # command: "is Hermes on local?" is a status ask, while
                # "switch Hermes to local" has no is/on/using.
                r"\bis\s+(?:jarvis|hermes)\s+(?:on\s+|using\s+)?(?:the\s+)?(?:local|cloud)\b",
                r"\b(?:jarvis|hermes)\s+(?:is\s+)?(?:on|using)\s+(?:the\s+)?(?:local|cloud)\b",
                r"\b(?:jarvis|hermes)\s*[:\-]?\s*(?:local|cloud)\s+or\s+(?:local|cloud)\b",
            ],
            "action": "model_control",
            "params": _model_args("status"),
        },
        "model.hermes_local": {
            "patterns": [
                r"\bhermes\b.*\b(?:local|offline|ollama)\b",
                r"\b(?:local|offline|ollama)\b.*\bhermes\b",
            ],
            "action": "model_control",
            "params": _model_args("use_local", "hermes"),
        },
        "model.hermes_cloud": {
            "patterns": [
                r"\bhermes\b.*\b(?:cloud|online|gemini)\b",
                r"\b(?:cloud|online|gemini)\b.*\bhermes\b",
            ],
            "action": "model_control",
            "params": _model_args("use_cloud", "hermes"),
        },
        "model.jarvis_local": {
            "patterns": [
                r"\bjarvis\b.*\b(?:local|offline|ollama)\b",
                r"\b(?:local|offline|ollama)\b.*\bjarvis\b",
            ],
            "action": "model_control",
            "params": _model_args("use_local", "jarvis"),
        },
        "model.jarvis_cloud": {
            "patterns": [
                r"\bjarvis\b.*\b(?:cloud|online|gemini)\b",
                r"\b(?:cloud|online|gemini)\b.*\bjarvis\b",
            ],
            "action": "model_control",
            "params": _model_args("use_cloud", "jarvis"),
        },
        "model.local": {
            "patterns": [
                r"\b(?:use|switch\s+to|go|run)\s+(?:the\s+)?(?:local|offline)\s+"
                r"(?:model|mode|llm)(?:\s+([a-zA-Z0-9][\w./:-]+))?\b",
                r"\b(?:go|switch\s+to)\s+local\b",
                r"\brun\s+locally\b",
                r"\buse\s+ollama\b",
                r"\blocal\s+mode\b",
                r"\boffline\s+mode\b",
            ],
            "action": "model_control",
            "params": _model_args("use_local", "both"),
        },
        "model.cloud": {
            "patterns": [
                r"\b(?:use|switch\s+to|go|back\s+to|return\s+to)\s+(?:the\s+)?"
                r"(?:cloud|online|gemini)\s+(?:model|mode|llm)?"
                r"(?:\s+([a-zA-Z0-9][\w./:-]+))?\b",
                r"\b(?:go|switch\s+to|back\s+to)\s+(?:the\s+)?cloud\b",
                r"\buse\s+(?:gemini|cloud)\b",
                r"\bcloud\s+mode\b",
                r"\bonline\s+mode\b",
            ],
            "action": "model_control",
            "params": _model_args("use_cloud", "both"),
        },
        "model.free": {
            "patterns": [
                r"\b(?:use|switch\s+to)\s+free\s+(?:models?|mode|tier)\b",
            ],
            "action": "model_control",
            "params": _model_args("use_free", "jarvis"),
        },
        "model.smart": {
            "patterns": [
                r"\b(?:use|switch\s+to)\s+(?:smart|auto)\s+(?:mode|routing)\b",
            ],
            "action": "model_control",
            "params": _model_args("use_smart", "jarvis"),
        },
        # Video control (most specific - requires "video" keyword)
        "video.play": {
            "patterns": [
                r"(?:play|watch)\s+video\s+(.+?)(?:\s+on\s+(\w+))?$",
                r"open\s+video\s+(.+)",
            ],
            "action": "video_player",
            "params": lambda m: {"query": m.group(1).strip()}
        },
        "video.pause": {
            "patterns": [r"pause\s+video", r"stop\s+video"],
            "action": "video_player",
            "params": lambda m: {"command": "pause"}
        },

        # System control (more specific patterns first)
        "system.volume": {
            "patterns": [
                r"system\s+volume\s+(?:up|down|(\d+))",
                r"(?:turn\s+)?(?:up|down)\s+(?:the\s+)?(?:system\s+)?volume",
                r"volume\s+(?:up|down)",  # Generic "volume up/down"
                r"(?:set\s+)?volume\s+to\s+(\d+)",  # "set volume to 50" or "volume to 50"
            ],
            "action": "computer_settings",
            "params": lambda m: {
                "setting": "volume",
                "value": int(m.group(1)) if (m.lastindex or 0) >= 1 and m.group(1) and m.group(1).isdigit() else None,
                "operation": "set" if (m.lastindex or 0) >= 1 and m.group(1) else ("increase" if "up" in m.group(0).lower() else "decrease")
            }
        },
        "system.brightness": {
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
        },
        "system.wifi": {
            "patterns": [
                r"(?:turn\s+)?(?:on|off)\s+wifi",
                r"(?:enable|disable)\s+wifi",
            ],
            "action": "computer_settings",
            "params": lambda m: {
                "setting": "wifi",
                "value": "on" in m.group(0).lower() or "enable" in m.group(0).lower()
            }
        },
        "system.sleep": {
            "patterns": [r"(?:go\s+to\s+)?sleep", r"suspend", r"hibernate"],
            "action": "computer_settings",
            "params": lambda m: {"setting": "sleep", "value": True}
        },
        "system.shutdown": {
            "patterns": [r"shutdown", r"power\s+off", r"turn\s+off\s+(?:computer|pc)"],
            "action": "computer_settings",
            "params": lambda m: {"setting": "shutdown", "value": True}
        },

        # Scheduler (before media to avoid "schedule play..." matching media.play)
        "scheduler.add": {
            "patterns": [
                r"schedule\s+(.+?)\s+(?:every|daily|weekly|monthly|at\s+\d+:\d+|at\s+\d+\s*(?:am|pm))",
                r"add\s+schedule\s+(.+?)\s+(?:every|daily|weekly|monthly|at\s+\d+:\d+|at\s+\d+\s*(?:am|pm))",
                r"schedule\s+job\s+(.+?)\s+(?:every|daily|weekly|monthly|at\s+\d+:\d+|at\s+\d+\s*(?:am|pm))",
            ],
            "action": "voice_command",
            "params": lambda m: {"command": m.group(1).strip(), "schedule": True}
        },
        "scheduler.remove": {
            "patterns": [
                r"remove\s+schedule\s+(.+)",
                r"delete\s+schedule\s+(.+)",
                r"cancel\s+schedule\s+(.+)",
            ],
            "action": "voice_command",
            "params": lambda m: {"job_name": m.group(1).strip(), "remove": True}
        },
        "scheduler.list": {
            "patterns": [
                r"list\s+schedules?",
                r"show\s+schedules?",
                r"what\s+(?:schedules|jobs)\s+(?:are\s+)?(?:scheduled|running)",
            ],
            "action": "voice_command",
            "params": lambda m: {"list": True}
        },
        "scheduler.enable": {
            "patterns": [
                r"enable\s+schedule\s+(.+)",
                r"activate\s+schedule\s+(.+)",
                r"turn\s+on\s+schedule\s+(.+)",
            ],
            "action": "voice_command",
            "params": lambda m: {"job_name": m.group(1).strip(), "enable": True}
        },
        "scheduler.disable": {
            "patterns": [
                r"disable\s+schedule\s+(.+)",
                r"deactivate\s+schedule\s+(.+)",
                r"turn\s+off\s+schedule\s+(.+)",
            ],
            "action": "voice_command",
            "params": lambda m: {"job_name": m.group(1).strip(), "disable": True}
        },

        # Media control (music/audio) - after scheduler to avoid conflict
        "media.play": {
            "patterns": [
                r"play\s+(?:music\s+)?(.+?)(?:\s+on\s+(\w+))?$",
                r"start\s+(?:music\s+)?(.+?)(?:\s+on\s+(\w+))?$",
                r"play\s+(.+)",  # General play - matches last
            ],
            "action": "music_player",
            "params": lambda m: {
                "query": m.group(1).strip(),
                "source": m.group(2) if m.lastindex >= 2 else "jiosaavn"
            }
        },
        "media.pause": {
            "patterns": [r"pause", r"stop\s+(?:music|song|playback)"],
            "action": "music_player",
            "params": lambda m: {"command": "pause"}
        },
        "media.next": {
            "patterns": [r"next\s+(?:song|track)", r"skip"],
            "action": "music_player",
            "params": lambda m: {"command": "next"}
        },
        "media.previous": {
            "patterns": [r"previous\s+(?:song|track)", r"go\s+back"],
            "action": "music_player",
            "params": lambda m: {"command": "previous"}
        },
        "media.volume": {
            "patterns": [
                r"music\s+volume\s+(?:up|down|(\d+))",
                r"media\s+volume\s+(?:up|down|(\d+))",
                r"volume\s+(?:up|down|(\d+))",
                r"(?:turn\s+)?(?:up|down)\s+(?:the\s+)?(?:music|media)\s+volume",
            ],
            "action": "music_player",
            "params": lambda m: {
                "command": "volume",
                "level": int(m.group(1)) if (m.lastindex or 0) >= 1 and m.group(1) and m.group(1).isdigit() else None,
                "operation": "set" if (m.lastindex or 0) >= 1 and m.group(1) else ("increase" if "up" in m.group(0).lower() else "decrease")
            }
        },

        # Information queries
        "info.weather": {
            "patterns": [
                r"weather\s*(?:in\s+(.+))?$",
                r"how'?s\s+the\s+weather",
            ],
            "action": "web_search",
            "params": lambda m: {
                "query": f"weather {m.group(1)}" if m.group(1) else "weather",
                "source": "weather"
            }
        },
        "info.time": {
            "patterns": [r"what'?s\s+the\s+time", r"current\s+time", r"time\s+now", r"what\s+is\s+the\s+time"],
            "action": "web_search",
            "params": lambda m: {"query": "current time", "source": "time"}
        },
        "info.news": {
            "patterns": [
                r"(?:read|get|show)\s+(?:me\s+)?(?:the\s+)?news",
                r"news\s+(?:about\s+(.+))?$",
            ],
            "action": "news_briefing",
            "params": lambda m: {"topic": m.group(1) if m.group(1) else "general"}
        },

        # Calendar & reminders
        "calendar.today": {
            "patterns": [
                r"what'?s\s+(?:on\s+)?(?:my\s+)?calendar\s+(?:today|today'?s?)",
                r"today'?s\s+(?:schedule|appointments|events)",
            ],
            "action": "calendar_control",
            "params": lambda m: {"command": "today"}
        },
        "calendar.add": {
            "patterns": [
                r"(?:add|schedule|create)\s+(?:event|meeting|appointment)\s+(.+?)(?:\s+(?:at|on)\s+(.+))?$",
            ],
            "action": "calendar_control",
            "params": lambda m: {
                "command": "add",
                "title": m.group(1).strip(),
                "time": m.group(2).strip() if m.group(2) else None
            }
        },
        "reminder.set": {
            "patterns": [
                r"remind\s+me\s+(?:to\s+)?(.+?)(?:\s+(?:at|in|on)\s+(.+))?$",
            ],
            "action": "calendar_control",
            "params": lambda m: {
                "command": "remind",
                "text": m.group(1).strip(),
                "when": m.group(2).strip() if m.group(2) else None
            }
        },

        # Timer & alarms
        "timer.set": {
            "patterns": [
                r"set\s+(?:a\s+)?timer\s+(?:for\s+)?(.+)",
                r"timer\s+(.+)",
            ],
            "action": "timer",
            "params": lambda m: {"duration": m.group(1).strip()}
        },
        "timer.cancel": {
            "patterns": [r"cancel\s+timer", r"stop\s+timer"],
            "action": "timer",
            "params": lambda m: {"command": "cancel"}
        },
        "alarm.set": {
            "patterns": [
                r"set\s+(?:an\s+)?alarm\s+(?:for\s+)?(.+)",
                r"alarm\s+(.+)",
            ],
            "action": "alarm",
            "params": lambda m: {"time": m.group(1).strip()}
        },

        # Web search
        "search.web": {
            "patterns": [
                r"(?:search|google|look\s+up)\s+(?:for\s+)?(.+)",
                r"what\s+is\s+(.+)",
                r"who\s+is\s+(.+)",
            ],
            "action": "web_search",
            "params": lambda m: {"query": m.group(1).strip()}
        },

        # Email
        "email.check": {
            "patterns": [r"check\s+(?:my\s+)?email", r"any\s+new\s+email"],
            "action": "gmail_control",
            "params": lambda m: {"command": "check"}
        },
        "email.send": {
            "patterns": [
                r"send\s+(?:an\s+)?email\s+to\s+(.+?)(?:\s+(?:saying|with)\s+(.+))?$",
            ],
            "action": "gmail_control",
            "params": lambda m: {
                "command": "send",
                "to": m.group(1).strip(),
                "body": m.group(2).strip() if m.group(2) else ""
            }
        },

        # Development
        "dev.run_tests": {
            "patterns": [
                r"run\s+(?:the\s+)?tests?",
                r"execute\s+tests?",
            ],
            "action": "run_tests",
            "params": lambda m: {}
        },
        "dev.git_status": {
            "patterns": [r"git\s+status", r"what'?s\s+(?:the\s+)?git\s+status"],
            "action": "git_status",
            "params": lambda m: {}
        },

        # Knowledge base
        "kb.search": {
            "patterns": [
                r"(?:search|find)\s+(?:in\s+)?(?:knowledge|notes|docs)\s+(.+)",
            ],
            "action": "knowledge_base",
            "params": lambda m: {"command": "search", "query": m.group(1).strip()}
        },

        # Home automation (placeholder)
        "home.lights": {
            "patterns": [
                r"(?:turn\s+)?(?:on|off)\s+(?:the\s+)?lights?",
                r"lights?\s+(?:on|off)",
            ],
            "action": "home_automation",
            "params": lambda m: {"device": "lights", "state": "on" in m.group(0).lower()}
        },
    }

    def __init__(self, macro_file: str | None = None):
        self.macros: dict[str, VoiceMacro] = {}
        self.macro_file = macro_file or "config/voice_macros.yaml"
        self._load_macros()

    def _load_macros(self):
        """Load macros from YAML file."""
        try:
            import yaml
            path = Path(self.macro_file)
            if path.exists():
                data = yaml.safe_load(path.read_text())
                for name, macro_data in (data or {}).get("macros", {}).items():
                    self.macros[name] = VoiceMacro(
                        name=name,
                        trigger_phrases=macro_data.get("trigger_phrases", []),
                        steps=macro_data.get("steps", []),
                        description=macro_data.get("description", "")
                    )
                logger.info(f"Loaded {len(self.macros)} voice macros")
        except Exception as e:
            logger.warning(f"Could not load macros: {e}")

    def save_macros(self):
        """Save macros to YAML file."""
        try:
            import yaml
            path = Path(self.macro_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "macros": {
                    name: {
                        "trigger_phrases": macro.trigger_phrases,
                        "steps": macro.steps,
                        "description": macro.description
                    }
                    for name, macro in self.macros.items()
                }
            }
            path.write_text(yaml.dump(data, default_flow_style=False))
        except Exception as e:
            logger.error(f"Could not save macros: {e}")

    def parse(self, text: str, use_offline_fallback: bool = True) -> VoiceIntent:
        """Parse voice text into intent with entities."""
        text = text.strip().lower()
        if not text:
            return VoiceIntent(intent="unknown", raw_text=text, confidence=0.0)

        best_match = None
        best_confidence = 0.0

        # Check macros first - find best (longest) matching phrase
        best_macro = None
        best_phrase_len = 0
        for macro_name, macro in self.macros.items():
            for phrase in macro.trigger_phrases:
                # Use word boundary regex for exact phrase matching
                phrase_pattern = r'\b' + re.escape(phrase.lower()) + r'\b'
                if re.search(phrase_pattern, text):
                    phrase_len = len(phrase)
                    if phrase_len > best_phrase_len:
                        best_phrase_len = phrase_len
                        best_macro = macro_name

        if best_macro:
            return VoiceIntent(
                intent=f"macro.{best_macro}",
                entities={"macro": best_macro},
                confidence=0.95,
                raw_text=text,
                action="macro",
                params={"macro": best_macro}
            )

        # Check intent patterns (first match wins - patterns are ordered by priority)
        for intent_name, config in self.INTENT_PATTERNS.items():
            for pattern in config["patterns"]:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    confidence = 0.8 + (0.15 * len(match.group(0)) / len(text))
                    confidence = min(confidence, 0.95)
                    params = config["params"](match) if callable(config["params"]) else config["params"]
                    return VoiceIntent(
                        intent=intent_name,
                        entities=self._extract_entities(text, intent_name),
                        confidence=confidence,
                        raw_text=text,
                        action=config["action"],
                        params=params
                    )

        # Fallback: try to match any action name
        action_names = get_action_names()
        for action_name in action_names:
            if action_name.replace("_", " ") in text:
                return VoiceIntent(
                    intent=f"action.{action_name}",
                    entities={},
                    confidence=0.6,
                    raw_text=text,
                    action=action_name,
                    params={}
                )

        # Offline fallback
        if use_offline_fallback:
            try:
                from core.voice_offline import get_offline_recognizer
                offline = get_offline_recognizer()
                offline_result = offline.recognize(text)
                if offline_result.intent != "unknown":
                    return VoiceIntent(
                        intent=offline_result.intent,
                        entities=offline_result.params,
                        confidence=offline_result.confidence * 0.8,  # Lower confidence for offline
                        raw_text=text,
                        action=offline_result.action,
                        params=offline_result.params,
                        offline_fallback=True
                    )
            except Exception as e:
                logger.debug(f"Offline recognizer unavailable: {e}")

        return VoiceIntent(
            intent="unknown",
            entities={},
            confidence=0.0,
            raw_text=text
        )

    def _extract_entities(self, text: str, intent: str) -> dict[str, Any]:
        """Extract additional entities from text based on intent."""
        entities = {}

        # Time entities
        time_patterns = [
            r"(\d{1,2}):(\d{2})\s*(?:am|pm)?",
            r"(\d{1,2})\s*(?:am|pm)",
            r"in\s+(\d+)\s*(?:minutes?|hours?|seconds?)",
        ]
        for pattern in time_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                entities["time_mentioned"] = match.group(0)
                break

        # Duration entities
        duration_match = re.search(r"(\d+)\s*(?:minutes?|hours?|seconds?)", text, re.IGNORECASE)
        if duration_match:
            entities["duration"] = duration_match.group(0)

        # Numbers
        numbers = re.findall(r"\b\d+\b", text)
        if numbers:
            entities["numbers"] = [int(n) for n in numbers]

        return entities

    def get_macro(self, name: str) -> VoiceMacro | None:
        """Get macro by name."""
        return self.macros.get(name)

    def list_macros(self) -> list[VoiceMacro]:
        """List all macros."""
        return list(self.macros.values())


# Singleton instance
_router: VoiceRouter | None = None


def get_voice_router() -> VoiceRouter:
    """Get or create the voice router singleton."""
    global _router
    if _router is None:
        _router = VoiceRouter()
    return _router