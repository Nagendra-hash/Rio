"""
dashboard/voice_api.py — REST surface for Voice Command Analytics & History.

Endpoints (bearer auth via the dashboard's _auth):

    GET  /api/voice/stats              Aggregated voice command statistics
    GET  /api/voice/history            Voice command history (paginated)
    GET  /api/voice/history/{session_id}  Session-specific history
    GET  /api/voice/trends             Intent usage trends over time
    GET  /api/voice/hourly             Hourly distribution of commands
    GET  /api/voice/sessions           List of voice sessions
    POST /api/voice/command            Execute voice command (already in server.py)
    GET  /api/voice/wake-word/models   List wake word models
    POST /api/voice/wake-word/train    Train a wake word model
    DELETE /api/voice/wake-word/{name} Delete a wake word model
"""

from __future__ import annotations

import threading
from dataclasses import asdict
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

_analytics_lock = threading.Lock()
_analytics = None


def shared_analytics() -> object | None:
    """The process-wide voice analytics singleton."""
    global _analytics
    with _analytics_lock:
        if _analytics is not None:
            return _analytics
        try:
            from core.voice_analytics import get_voice_analytics
            _analytics = get_voice_analytics()
        except Exception:
            return None
        return _analytics


def _deny_auth() -> JSONResponse:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


def _parse_days(req: Request, default: int = 30) -> int:
    """Parse days query parameter."""
    try:
        return int(req.query_params.get("days", default))
    except ValueError:
        return default


def _parse_limit(req: Request, default: int = 50) -> int:
    """Parse limit query parameter."""
    try:
        return int(req.query_params.get("limit", default))
    except ValueError:
        return default


def _parse_offset(req: Request, default: int = 0) -> int:
    """Parse offset query parameter."""
    try:
        return int(req.query_params.get("offset", default))
    except ValueError:
        return default


def register(app, auth_check) -> None:
    """Attach /api/voice/* endpoints to the running FastAPI app."""

    @app.get("/api/voice/stats")
    async def voice_stats(req: Request):
        """Get aggregated voice command statistics."""
        if not auth_check(req):
            return _deny_auth()

        analytics = shared_analytics()
        if not analytics:
            return JSONResponse({"ok": False, "error": "analytics unavailable"},
                                status_code=503)

        days = _parse_days(req)
        stats = analytics.get_stats(days=days)
        return JSONResponse({"ok": True, **stats})

    @app.get("/api/voice/history")
    async def voice_history(req: Request):
        """Get voice command history (paginated)."""
        if not auth_check(req):
            return _deny_auth()

        analytics = shared_analytics()
        if not analytics:
            return JSONResponse({"ok": False, "error": "analytics unavailable"},
                                status_code=503)

        days = _parse_days(req)
        limit = _parse_limit(req)
        offset = _parse_offset(req)

        events = analytics._load_events(days)
        events.sort(key=lambda e: e.timestamp, reverse=True)

        total = len(events)
        paginated = events[offset:offset + limit]

        history = []
        for e in paginated:
            history.append({
                "timestamp": e.timestamp,
                "session_id": e.session_id,
                "transcript": e.transcript,
                "intent": e.intent,
                "action": e.action,
                "success": e.success,
                "confidence": e.confidence,
                "latency_ms": e.latency_ms,
                "user_id": e.user_id,
                "error": e.error
            })

        return JSONResponse({
            "ok": True,
            "history": history,
            "total": total,
            "limit": limit,
            "offset": offset
        })

    @app.get("/api/voice/history/{session_id}")
    async def voice_session_history(req: Request, session_id: str):
        """Get voice command history for a specific session."""
        if not auth_check(req):
            return _deny_auth()

        analytics = shared_analytics()
        if not analytics:
            return JSONResponse({"ok": False, "error": "analytics unavailable"},
                                status_code=503)

        days = _parse_days(req)
        events = analytics._load_events(days)
        session_events = [e for e in events if e.session_id == session_id]
        session_events.sort(key=lambda e: e.timestamp)

        summary = analytics.get_session_summary(session_id)

        history = []
        for e in session_events:
            history.append({
                "timestamp": e.timestamp,
                "transcript": e.transcript,
                "intent": e.intent,
                "action": e.action,
                "entities": e.entities,
                "params": e.params,
                "success": e.success,
                "confidence": e.confidence,
                "latency_ms": e.latency_ms,
                "error": e.error
            })

        return JSONResponse({
            "ok": True,
            "session_id": session_id,
            "summary": asdict(summary) if summary else None,
            "history": history
        })

    @app.get("/api/voice/trends")
    async def voice_trends(req: Request):
        """Get intent usage trends over time."""
        if not auth_check(req):
            return _deny_auth()

        analytics = shared_analytics()
        if not analytics:
            return JSONResponse({"ok": False, "error": "analytics unavailable"},
                                status_code=503)

        days = _parse_days(req)
        top_n = int(req.query_params.get("top_n", 5))

        trends = analytics.get_intent_trends(days=days, top_n=top_n)
        return JSONResponse({"ok": True, "trends": trends})

    @app.get("/api/voice/hourly")
    async def voice_hourly(req: Request):
        """Get hourly distribution of commands."""
        if not auth_check(req):
            return _deny_auth()

        analytics = shared_analytics()
        if not analytics:
            return JSONResponse({"ok": False, "error": "analytics unavailable"},
                                status_code=503)

        days = _parse_days(req)
        hourly = analytics.get_hourly_distribution(days=days)
        return JSONResponse({"ok": True, "hourly": hourly})

    @app.get("/api/voice/sessions")
    async def voice_sessions(req: Request):
        """List recent voice sessions."""
        if not auth_check(req):
            return _deny_auth()

        analytics = shared_analytics()
        if not analytics:
            return JSONResponse({"ok": False, "error": "analytics unavailable"},
                                status_code=503)

        days = _parse_days(req)
        limit = _parse_limit(req)

        events = analytics._load_events(days)

        # Group by session
        sessions = {}
        for e in events:
            if e.session_id not in sessions:
                sessions[e.session_id] = {
                    "session_id": e.session_id,
                    "first_seen": e.timestamp,
                    "last_seen": e.timestamp,
                    "command_count": 0,
                    "success_count": 0,
                    "intents": set(),
                    "user_id": e.user_id
                }
            s = sessions[e.session_id]
            s["first_seen"] = min(s["first_seen"], e.timestamp)
            s["last_seen"] = max(s["last_seen"], e.timestamp)
            s["command_count"] += 1
            if e.success:
                s["success_count"] += 1
            s["intents"].add(e.intent)

        # Convert to list and sort by last_seen desc
        session_list = list(sessions.values())
        for s in session_list:
            s["intents"] = list(s["intents"])
            s["success_rate"] = s["success_count"] / s["command_count"] if s["command_count"] > 0 else 0

        session_list.sort(key=lambda s: s["last_seen"], reverse=True)
        session_list = session_list[:limit]

        return JSONResponse({"ok": True, "sessions": session_list})

    # Wake word endpoints
    @app.get("/api/voice/wake-word/models")
    async def wake_word_models(req: Request):
        """List wake word models."""
        if not auth_check(req):
            return _deny_auth()

        try:
            from core.voice_wake_word import get_wake_word_trainer
            trainer = get_wake_word_trainer()
            models = trainer.list_models()
            return JSONResponse({"ok": True, "models": models})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.post("/api/voice/wake-word/train")
    async def wake_word_train(req: Request):
        """Train a new wake word model."""
        if not auth_check(req):
            return _deny_auth()

        try:
            body = await req.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        name = (body.get("name") or "").strip()
        phrases = body.get("phrases") or []
        threshold = float(body.get("threshold", 0.5))
        sensitivity = float(body.get("sensitivity", 0.5))

        if not name:
            return JSONResponse({"error": "Name required"}, status_code=400)
        if not phrases:
            return JSONResponse({"error": "At least one phrase required"}, status_code=400)

        try:
            from core.voice_wake_word import get_wake_word_trainer
            import numpy as np

            trainer = get_wake_word_trainer()

            # For training, we need audio samples
            # If not provided, we'll just create the model structure
            audio_samples = body.get("audio_samples")
            if audio_samples:
                audio_arrays = [np.array(s, dtype=np.float32) for s in audio_samples]
            else:
                audio_arrays = []

            if audio_arrays:
                model = trainer.train_model(name, phrases, audio_arrays, threshold, sensitivity)
            else:
                # Create model without audio samples (user will add them later)
                from core.voice_wake_word import WakeWordModel
                import time
                model = WakeWordModel(
                    name=name,
                    phrases=phrases,
                    threshold=threshold,
                    sensitivity=sensitivity
                )
                trainer.models[name] = model
                trainer._save_model(model)

            return JSONResponse({"ok": True, "model": {
                "name": model.name,
                "phrases": model.phrases,
                "threshold": model.threshold,
                "sensitivity": model.sensitivity,
                "sample_count": len(model.audio_samples)
            }})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.delete("/api/voice/wake-word/{name}")
    async def wake_word_delete(req: Request, name: str):
        """Delete a wake word model."""
        if not auth_check(req):
            return _deny_auth()

        try:
            from core.voice_wake_word import get_wake_word_trainer
            trainer = get_wake_word_trainer()
            success = trainer.delete_model(name)
            if success:
                return JSONResponse({"ok": True, "message": f"Model {name} deleted"})
            else:
                return JSONResponse({"error": "Model not found"}, status_code=404)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)