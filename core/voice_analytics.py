"""
core/voice_analytics.py — Voice Command Analytics
Tracking, analysis, and insights for voice command usage.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class VoiceEvent:
    """A single voice command event."""
    timestamp: float
    session_id: str
    transcript: str
    intent: str
    action: str | None
    entities: dict[str, Any]
    params: dict[str, Any]
    success: bool
    confidence: float
    latency_ms: float
    user_id: str | None = None
    error: str | None = None


@dataclass
class SessionSummary:
    """Summary of a voice session."""
    session_id: str
    start_time: float
    end_time: float
    total_commands: int
    successful_commands: int
    failed_commands: int
    unique_intents: set[str]
    avg_confidence: float
    avg_latency_ms: float
    user_id: str | None = None


class VoiceAnalytics:
    """Analytics engine for voice commands."""

    def __init__(self, storage_dir: str = "memory/voice_analytics"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        # In-memory buffers (flushed periodically)
        self.events: list[VoiceEvent] = []
        self.max_events_memory = 10000
        self._flush_interval = 60  # seconds
        self._last_flush = time.time()

        # Aggregated stats (computed on demand)
        self._stats_cache: dict[str, Any] = {}
        self._cache_ttl = 300  # 5 minutes
        self._cache_time = 0.0

    def record_event(self, event: VoiceEvent) -> None:
        """Record a voice command event."""
        self.events.append(event)

        # Trim if too large
        if len(self.events) > self.max_events_memory:
            self.events = self.events[-self.max_events_memory // 2:]

        # Invalidate cache
        self._cache_time = 0.0

        # Auto-flush
        if time.time() - self._last_flush > self._flush_interval:
            self.flush()

    def flush(self) -> None:
        """Flush events to disk."""
        if not self.events:
            return

        # Group by date
        by_date: dict[str, list[VoiceEvent]] = defaultdict(list)
        for event in self.events:
            date_str = datetime.fromtimestamp(event.timestamp).strftime("%Y-%m-%d")
            by_date[date_str].append(event)

        for date_str, day_events in by_date.items():
            file = self.storage_dir / f"events_{date_str}.jsonl"
            try:
                with file.open("a") as f:
                    for event in day_events:
                        f.write(json.dumps({
                            "timestamp": event.timestamp,
                            "session_id": event.session_id,
                            "transcript": event.transcript,
                            "intent": event.intent,
                            "action": event.action,
                            "entities": event.entities,
                            "params": event.params,
                            "success": event.success,
                            "confidence": event.confidence,
                            "latency_ms": event.latency_ms,
                            "user_id": event.user_id,
                            "error": event.error
                        }) + "\n")
            except Exception as e:
                logger.error(f"Failed to flush events to {file}: {e}")

        self.events.clear()
        self._last_flush = time.time()
        self._cache_time = 0.0

    def _load_events(self, days: int = 30) -> list[VoiceEvent]:
        """Load events from disk for the last N days."""
        events = []
        cutoff = time.time() - (days * 86400)

        # Load from memory buffer first
        events.extend(self.events)

        # Load from disk
        for file in self.storage_dir.glob("events_*.jsonl"):
            try:
                date_str = file.stem.replace("events_", "")
                file_date = datetime.strptime(date_str, "%Y-%m-%d").timestamp()
                if file_date < cutoff:
                    continue

                with file.open() as f:
                    for line in f:
                        try:
                            data = json.loads(line.strip())
                            if data["timestamp"] >= cutoff:
                                events.append(VoiceEvent(**data))
                        except Exception:
                            continue
            except Exception as e:
                logger.warning(f"Failed to load events from {file}: {e}")

        return events

    def get_stats(self, days: int = 30, force_refresh: bool = False) -> dict[str, Any]:
        """Get aggregated statistics."""
        now = time.time()

        # Check cache
        if not force_refresh and self._cache_time and (now - self._cache_time) < self._cache_ttl:
            return self._stats_cache

        events = self._load_events(days)

        if not events:
            return {
                "total_commands": 0,
                "success_rate": 0.0,
                "avg_confidence": 0.0,
                "avg_latency_ms": 0.0,
                "top_intents": [],
                "top_actions": [],
                "commands_per_day": {},
                "success_by_intent": {},
                "errors": []
            }

        # Compute stats
        total = len(events)
        successful = sum(1 for e in events if e.success)
        success_rate = successful / total if total > 0 else 0.0

        avg_confidence = sum(e.confidence for e in events) / total
        avg_latency = sum(e.latency_ms for e in events) / total

        intent_counts = Counter(e.intent for e in events)
        action_counts = Counter(e.action for e in events if e.action)

        # Commands per day
        per_day = defaultdict(int)
        for e in events:
            day = datetime.fromtimestamp(e.timestamp).strftime("%Y-%m-%d")
            per_day[day] += 1

        # Success rate by intent
        intent_success = defaultdict(lambda: {"total": 0, "success": 0})
        for e in events:
            intent_success[e.intent]["total"] += 1
            if e.success:
                intent_success[e.intent]["success"] += 1

        success_by_intent = {
            intent: data["success"] / data["total"]
            for intent, data in intent_success.items()
            if data["total"] > 0
        }

        # Errors
        errors = [e.error for e in events if e.error]
        error_counts = Counter(errors).most_common(10)

        stats = {
            "total_commands": total,
            "successful_commands": successful,
            "success_rate": round(success_rate, 4),
            "avg_confidence": round(avg_confidence, 4),
            "avg_latency_ms": round(avg_latency, 2),
            "top_intents": intent_counts.most_common(10),
            "top_actions": action_counts.most_common(10),
            "commands_per_day": dict(sorted(per_day.items())),
            "success_by_intent": {k: round(v, 4) for k, v in success_by_intent.items()},
            "top_errors": [{"error": err, "count": cnt} for err, cnt in error_counts],
            "unique_sessions": len(set(e.session_id for e in events)),
            "unique_users": len(set(e.user_id for e in events if e.user_id))
        }

        self._stats_cache = stats
        self._cache_time = now
        return stats

    def get_session_summary(self, session_id: str) -> SessionSummary | None:
        """Get summary for a specific session."""
        events = [e for e in self.events if e.session_id == session_id]
        if not events:
            # Check disk
            all_events = self._load_events(30)
            events = [e for e in all_events if e.session_id == session_id]

        if not events:
            return None

        events.sort(key=lambda e: e.timestamp)
        start = events[0].timestamp
        end = events[-1].timestamp

        successful = sum(1 for e in events if e.success)
        intents = set(e.intent for e in events)
        avg_conf = sum(e.confidence for e in events) / len(events)
        avg_lat = sum(e.latency_ms for e in events) / len(events)

        return SessionSummary(
            session_id=session_id,
            start_time=start,
            end_time=end,
            total_commands=len(events),
            successful_commands=successful,
            failed_commands=len(events) - successful,
            unique_intents=intents,
            avg_confidence=avg_conf,
            avg_latency_ms=avg_lat,
            user_id=events[0].user_id
        )

    def get_user_stats(self, user_id: str, days: int = 30) -> dict[str, Any]:
        """Get stats for a specific user."""
        events = self._load_events(days)
        user_events = [e for e in events if e.user_id == user_id]

        if not user_events:
            return {"total_commands": 0}

        return self._compute_stats(user_events)

    def get_intent_trends(self, days: int = 30, top_n: int = 5) -> dict[str, list[dict]]:
        """Get intent usage trends over time."""
        events = self._load_events(days)

        # Group by day and intent
        daily_intents = defaultdict(lambda: defaultdict(int))
        for e in events:
            day = datetime.fromtimestamp(e.timestamp).strftime("%Y-%m-%d")
            daily_intents[day][e.intent] += 1

        # Get top intents overall
        intent_totals = Counter(e.intent for e in events)
        top_intents = [i for i, _ in intent_totals.most_common(top_n)]

        # Build trend data
        trends = {}
        for intent in top_intents:
            trends[intent] = [
                {"date": day, "count": daily_intents[day].get(intent, 0)}
                for day in sorted(daily_intents.keys())
            ]

        return trends

    def get_hourly_distribution(self, days: int = 30) -> dict[int, int]:
        """Get command distribution by hour of day."""
        events = self._load_events(days)
        hourly = Counter()
        for e in events:
            hour = datetime.fromtimestamp(e.timestamp).hour
            hourly[hour] += 1
        return dict(sorted(hourly.items()))

    def _compute_stats(self, events: list[VoiceEvent]) -> dict[str, Any]:
        """Compute stats from a list of events."""
        if not events:
            return {"total_commands": 0}

        total = len(events)
        successful = sum(1 for e in events if e.success)
        return {
            "total_commands": total,
            "successful_commands": successful,
            "success_rate": round(successful / total, 4),
            "avg_confidence": round(sum(e.confidence for e in events) / total, 4),
            "avg_latency_ms": round(sum(e.latency_ms for e in events) / total, 2),
            "top_intents": Counter(e.intent for e in events).most_common(10),
            "top_actions": Counter(e.action for e in events if e.action).most_common(10),
            "unique_sessions": len(set(e.session_id for e in events))
        }

    def export_report(self, days: int = 30, format: str = "json") -> str:
        """Export analytics report."""
        stats = self.get_stats(days)
        trends = self.get_intent_trends(days)
        hourly = self.get_hourly_distribution(days)

        report = {
            "generated_at": datetime.now().isoformat(),
            "period_days": days,
            "summary": stats,
            "intent_trends": trends,
            "hourly_distribution": hourly
        }

        if format == "json":
            return json.dumps(report, indent=2)
        elif format == "markdown":
            return self._format_markdown(report)
        else:
            return json.dumps(report, indent=2)

    def _format_markdown(self, report: dict) -> str:
        """Format report as markdown."""
        s = report["summary"]
        lines = [
            f"# Voice Analytics Report",
            f"Generated: {report['generated_at']}",
            f"Period: {report['period_days']} days",
            "",
            "## Summary",
            f"- Total Commands: {s['total_commands']}",
            f"- Success Rate: {s['success_rate']:.1%}",
            f"- Avg Confidence: {s['avg_confidence']:.2f}",
            f"- Avg Latency: {s['avg_latency_ms']:.0f}ms",
            f"- Unique Sessions: {s['unique_sessions']}",
            f"- Unique Users: {s['unique_users']}",
            "",
            "## Top Intents",
        ]
        for intent, count in s.get("top_intents", []):
            lines.append(f"- {intent}: {count}")

        lines.append("")
        lines.append("## Top Actions")
        for action, count in s.get("top_actions", []):
            lines.append(f"- {action}: {count}")

        lines.append("")
        lines.append("## Hourly Distribution")
        for hour, count in sorted(report.get("hourly_distribution", {}).items()):
            bar = "█" * (count // max(1, max(report.get("hourly_distribution", {}).values()) // 20))
            lines.append(f"- {hour:02d}:00 {bar} {count}")

        return "\n".join(lines)


# Singleton instance
_analytics: VoiceAnalytics | None = None


def get_voice_analytics() -> VoiceAnalytics:
    """Get or create the voice analytics singleton."""
    global _analytics
    if _analytics is None:
        _analytics = VoiceAnalytics()
    return _analytics