"""
actions/voice_command.py — Voice Command Skill
Handles voice command processing, routing, and execution with auth, scheduling, and analytics.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from pathlib import Path

from core.voice_router import get_voice_router, VoiceIntent
from core.voice_session import get_session_manager, VoiceSession, VoiceTurn
from core.voice_auth import get_voice_authenticator, AuthResult
from core.voice_scheduler import get_voice_scheduler
from core.voice_analytics import get_voice_analytics, VoiceEvent
from core.action_loader import discover_actions

logger = logging.getLogger(__name__)

# Sensitive actions that require confirmation
SENSITIVE_ACTIONS = {
    "system.shutdown", "system.sleep", "system.restart",
    "email.send", "calendar.delete", "kb.delete",
    "deploy_app", "backup_now", "home.security"
}

# Actions that support follow-up references
FOLLOWUP_ACTIONS = {
    "media.play", "video.play", "search.web", "kb.search",
    "email.send", "calendar.add", "reminder.set"
}


async def voice_command_handler(params: dict[str, Any]) -> dict[str, Any]:
    """
    Main voice command handler.

    Returns:
        dict with: success, intent, action, result, response_text, needs_confirmation
    """
    start_time = time.time()

    transcript = params.get("transcript", "").strip()
    session_id = params.get("session_id", "default")
    auto_confirm = params.get("auto_confirm", False)
    return_audio = params.get("return_audio", True)
    user_id = params.get("user_id")  # For voice auth
    audio_data = params.get("audio_data")  # For voice auth verification
    schedule_job = params.get("schedule_job")  # Schedule this command as a job
    job_name = params.get("job_name")
    job_cron = params.get("job_cron")

    if not transcript:
        return {"success": False, "error": "Empty transcript"}

    # Voice authentication if user_id and audio provided
    if user_id and audio_data:
        auth_result = await _verify_voice(user_id, audio_data)
        if not auth_result.authenticated:
            return {
                "success": False,
                "error": f"Voice authentication failed: {auth_result.error}",
                "auth_required": True
            }

    # Get router and session
    router = get_voice_router()
    session_mgr = get_session_manager()
    session = session_mgr.get_session(session_id)
    analytics = get_voice_analytics()

    # Parse intent (with offline fallback)
    intent = router.parse(transcript)
    logger.info(f"Voice intent: {intent.intent} (confidence: {intent.confidence:.2f})")

    # Handle macro execution
    if intent.intent.startswith("macro."):
        result = await _execute_macro(router, session, intent, return_audio)
        await _record_analytics(analytics, session_id, user_id, transcript, intent, result, start_time)
        return result

    # Handle scheduler commands
    if intent.intent in ("scheduler.add", "scheduler.remove", "scheduler.list", "scheduler.enable", "scheduler.disable"):
        result = await _handle_scheduler(intent, job_name, job_cron, session_id)
        await _record_analytics(analytics, session_id, user_id, transcript, intent, result, start_time)
        return result

    # Handle confirmation responses
    if session.pending_confirmation:
        result = await _handle_confirmation(session, transcript, return_audio)
        await _record_analytics(analytics, session_id, user_id, transcript, intent, result, start_time)
        return result

    # Check if this is a follow-up to previous command
    if session.last_intent and _is_followup(transcript, intent):
        intent.params = session.resolve_followup(transcript, intent.params)
        logger.info(f"Resolved followup params: {intent.params}")

    # Check for sensitive action requiring confirmation
    if not auto_confirm and intent.intent in SENSITIVE_ACTIONS:
        session.request_confirmation(
            f"Confirm {intent.intent.replace('_', ' ')}?",
            intent
        )
        return {
            "success": True,
            "needs_confirmation": True,
            "confirmation_prompt": session.pending_confirmation.prompt,
            "intent": intent.intent,
            "response_text": f"Please confirm: {session.pending_confirmation.prompt}"
        }

    # Execute the action
    result = await _execute_action(session, intent)

    # Generate response text for TTS
    response_text = _generate_response(intent, result, transcript)

    # Save session
    session_mgr.save_session(session)

    # Schedule job if requested
    if schedule_job and job_name and job_cron:
        scheduler = get_voice_scheduler(_scheduler_executor)
        scheduler.add_job(
            name=job_name,
            cron_expression=job_cron,
            command=transcript,
            params={"session_id": session_id, "auto_confirm": auto_confirm}
        )
        result["scheduled"] = True
        result["job_name"] = job_name

    # Record analytics
    await _record_analytics(analytics, session_id, user_id, transcript, intent, result, start_time)

    return {
        "success": result.get("success", False),
        "intent": intent.intent,
        "action": intent.action,
        "result": result,
        "response_text": response_text if return_audio else None,
        "confidence": intent.confidence,
        "offline_fallback": getattr(intent, "offline_fallback", False)
    }


async def _verify_voice(user_id: str, audio_data: list) -> AuthResult:
    """Verify voice against enrolled voiceprint."""
    import numpy as np
    from core.voice_auth import get_voice_authenticator  # noqa: F811

    audio_array = np.array(audio_data, dtype=np.float32)
    authenticator = get_voice_authenticator()
    return authenticator.verify(user_id, audio_array)


async def _handle_scheduler(
    intent: VoiceIntent,
    job_name: str | None,
    job_cron: str | None,
    session_id: str
) -> dict[str, Any]:
    """Handle scheduler-related intents."""
    scheduler = get_voice_scheduler(_scheduler_executor)

    if intent.intent == "scheduler.add":
        if not job_name or not job_cron:
            return {"success": False, "error": "job_name and job_cron required"}
        if not intent.params.get("command") and not intent.params.get("macro"):
            return {"success": False, "error": "command or macro required"}

        job = scheduler.add_job(
            name=job_name,
            cron_expression=job_cron,
            macro_name=intent.params.get("macro"),
            command=intent.params.get("command"),
            params={"session_id": session_id, **intent.params}
        )
        return {"success": True, "job_id": job.job_id, "next_run": job.next_run}

    elif intent.intent == "scheduler.remove":
        if not job_name:
            return {"success": False, "error": "job_name required"}
        success = scheduler.remove_job(job_name)
        return {"success": success, "message": f"Job {job_name} removed" if success else "Job not found"}

    elif intent.intent == "scheduler.list":
        jobs = scheduler.list_jobs()
        return {
            "success": True,
            "jobs": [
                {
                    "job_id": j.job_id,
                    "name": j.name,
                    "cron": j.cron_expression,
                    "enabled": j.enabled,
                    "next_run": j.next_run,
                    "run_count": j.run_count
                }
                for j in jobs
            ]
        }

    elif intent.intent in ("scheduler.enable", "scheduler.disable"):
        if not job_name:
            return {"success": False, "error": "job_name required"}
        enabled = intent.intent == "scheduler.enable"
        success = scheduler.enable_job(job_name, enabled)
        return {"success": success, "enabled": enabled}

    return {"success": False, "error": "Unknown scheduler command"}


async def _record_analytics(
    analytics: Any,
    session_id: str,
    user_id: str | None,
    transcript: str,
    intent: VoiceIntent,
    result: dict,
    start_time: float
) -> None:
    """Record analytics event."""
    try:
        latency_ms = (time.time() - start_time) * 1000
        event = VoiceEvent(
            timestamp=time.time(),
            session_id=session_id,
            transcript=transcript,
            intent=intent.intent,
            action=intent.action,
            entities=intent.entities,
            params=intent.params,
            success=result.get("success", False),
            confidence=intent.confidence,
            latency_ms=latency_ms,
            user_id=user_id,
            error=result.get("error")
        )
        analytics.record_event(event)
    except Exception as e:
        logger.warning(f"Failed to record analytics: {e}")


async def _scheduler_executor(mode: str, params: dict) -> dict:
    """Executor for scheduled jobs - calls voice_command_handler."""
    from core.voice_session import get_session_manager
    session_mgr = get_session_manager()
    session = session_mgr.get_session(params.get("session_id", "default"))

    # Create a mock intent for the scheduled command
    router = get_voice_router()
    transcript = params.get("transcript", "")
    intent = router.parse(transcript)
    intent.params.update(params)

    return await _execute_action(session, intent)


async def _execute_macro(router, session: VoiceSession, intent: VoiceIntent, return_audio: bool) -> dict[str, Any]:
    """Execute a voice macro."""
    macro_name = intent.entities.get("macro")
    macro = router.get_macro(macro_name)

    if not macro:
        return {"success": False, "error": f"Macro '{macro_name}' not found"}

    session.start_macro(macro_name)
    results = []

    for i, step in enumerate(macro.steps):
        session.macro_step = i + 1
        action_name = step.get("action")
        step_params = step.get("params", {})

        # Template substitution from previous results
        step_params = _substitute_templates(step_params, results, session)

        try:
            step_result = execute_action(action_name, step_params)
            results.append({"step": i, "action": action_name, "result": step_result})
            logger.info(f"Macro {macro_name} step {i+1}/{len(macro.steps)}: {action_name} - success")
        except Exception as e:
            logger.error(f"Macro {macro_name} step {i+1} failed: {e}")
            results.append({"step": i, "action": action_name, "error": str(e)})
            break

    session.end_macro()

    # Generate summary response
    success_count = sum(1 for r in results if "error" not in r)
    response_text = f"Macro '{macro_name}' completed: {success_count}/{len(results)} steps successful."

    return {
        "success": success_count == len(results),
        "intent": intent.intent,
        "action": "macro",
        "result": {"steps": results, "macro": macro_name},
        "response_text": response_text if return_audio else None
    }


async def _handle_confirmation(session: VoiceSession, transcript: str, return_audio: bool) -> dict[str, Any]:
    """Handle yes/no confirmation response."""
    text_lower = transcript.lower().strip()

    # Check for confirmation
    confirmed = any(w in text_lower for w in ["yes", "yeah", "yep", "sure", "ok", "okay", "confirm", "do it"])
    denied = any(w in text_lower for w in ["no", "nope", "cancel", "don't", "dont", "stop", "abort"])

    if not confirmed and not denied:
        return {
            "success": True,
            "needs_confirmation": True,
            "confirmation_prompt": session.pending_confirmation.prompt,
            "response_text": "Please say yes to confirm or no to cancel."
        }

    intent = session.confirm(confirmed)

    if not confirmed:
        return {
            "success": True,
            "cancelled": True,
            "response_text": "Cancelled." if return_audio else None
        }

    # Execute the confirmed action
    result = await _execute_action(session, intent)
    response_text = _generate_response(intent, result, transcript)

    session_mgr = get_session_manager()
    session_mgr.save_session(session)

    return {
        "success": result.get("success", False),
        "intent": intent.intent,
        "action": intent.action,
        "result": result,
        "response_text": response_text if return_audio else None
    }


async def _execute_action(session: VoiceSession, intent: VoiceIntent) -> dict[str, Any]:
    """Execute the action for an intent."""
    if not intent.action:
        return {"success": False, "error": f"No action for intent: {intent.intent}"}

    try:
        result = execute_action(intent.action, intent.params)

        # Record successful turn
        session.add_turn(VoiceTurn(
            timestamp=time.time(),
            transcript=intent.raw_text,
            intent=intent.intent,
            entities=intent.entities,
            action=intent.action,
            params=intent.params,
            result=result,
            success=result.get("success", False)
        ))

        return result

    except Exception as e:
        logger.error(f"Action {intent.action} failed: {e}")

        session.add_turn(VoiceTurn(
            timestamp=time.time(),
            transcript=intent.raw_text,
            intent=intent.intent,
            entities=intent.entities,
            action=intent.action,
            params=intent.params,
            result={"error": str(e)},
            success=False,
            error=str(e)
        ))

        return {"success": False, "error": str(e)}


def _is_followup(transcript: str, intent: VoiceIntent) -> bool:
    """Check if this is a follow-up command."""
    followup_keywords = [
        "again", "repeat", "once more", "the first", "the second", "the third",
        "the other", "different", "another", "similar", "like this", "more like"
    ]
    text_lower = transcript.lower()
    return any(kw in text_lower for kw in followup_keywords)


def _substitute_templates(params: dict, results: list, session: VoiceSession) -> dict:
    """Substitute template variables in macro step params."""
    import re

    def substitute(value: str) -> str:
        if not isinstance(value, str):
            return value

        # {{macro.step.field}} - reference previous macro step result
        def repl(match):
            path = match.group(1).split(".")
            if path[0] == "macro" and len(path) >= 3:
                step_idx = int(path[1])
                field = ".".join(path[2:])
                if step_idx < len(results):
                    step_result = results[step_idx].get("result", {})
                    # Navigate nested field
                    for part in field.split("."):
                        if isinstance(step_result, dict):
                            step_result = step_result.get(part)
                        else:
                            return match.group(0)
                    return str(step_result) if step_result is not None else match.group(0)
            # {{session.field}} - reference session context
            elif path[0] == "session":
                field = ".".join(path[1:])
                val = session
                for part in field.split("."):
                    if isinstance(val, dict):
                        val = val.get(part)
                    else:
                        return match.group(0)
                return str(val) if val is not None else match.group(0)
            return match.group(0)

        return re.sub(r"\{\{([^}]+)\}\}", repl, value)

    result = {}
    for key, value in params.items():
        if isinstance(value, str):
            result[key] = substitute(value)
        elif isinstance(value, dict):
            result[key] = _substitute_templates(value, results, session)
        elif isinstance(value, list):
            result[key] = [_substitute_templates(v, results, session) if isinstance(v, dict) else (substitute(v) if isinstance(v, str) else v) for v in value]
        else:
            result[key] = value
    return result


def _generate_response(intent: VoiceIntent, result: dict, transcript: str) -> str:
    """Generate natural language response for TTS."""
    if not result.get("success"):
        error = result.get("error", "Unknown error")
        return f"Sorry, I couldn't {intent.intent.replace('_', ' ')}. {error}"

    action = intent.action

    # Action-specific responses
    responses = {
        "music_player": _music_response,
        "video_player": _video_response,
        "computer_settings": _system_response,
        "web_search": _search_response,
        "calendar_control": _calendar_response,
        "news_briefing": _news_response,
        "gmail_control": _email_response,
        "knowledge_base": _kb_response,
        "macro": _macro_response,
        "model_control": _model_response,
    }

    if action in responses:
        return responses[action](result, transcript)

    # Generic success response
    return f"Done. {intent.intent.replace('_', ' ').replace('.', ' ')} completed."


def _music_response(result: dict, transcript: str) -> str:
    if "playing" in str(result).lower():
        track = result.get("track") or result.get("title") or "music"
        return f"Playing {track}."
    return "Music command completed."


def _video_response(result: dict, transcript: str) -> str:
    if "playing" in str(result).lower():
        title = result.get("title") or "video"
        return f"Playing {title}."
    return "Video command completed."


def _system_response(result: dict, transcript: str) -> str:
    setting = result.get("setting", "system")
    return f"{setting.capitalize()} updated."


def _search_response(result: dict, transcript: str) -> str:
    count = result.get("count", 0)
    return f"Found {count} results." if count else "Search completed."


def _calendar_response(result: dict, transcript: str) -> str:
    events = result.get("events", [])
    return f"You have {len(events)} events." if events else "Calendar updated."


def _news_response(result: dict, transcript: str) -> str:
    count = result.get("count", 0)
    return f"Here are {count} news items." if count else "News briefing ready."


def _email_response(result: dict, transcript: str) -> str:
    count = result.get("count", 0)
    return f"You have {count} emails." if count else "Email command completed."


def _kb_response(result: dict, transcript: str) -> str:
    return "Knowledge base updated."


def _macro_response(result: dict, transcript: str) -> str:
    macro = result.get("macro", "macro")
    steps = result.get("steps", [])
    success = sum(1 for s in steps if "error" not in s)
    return f"Macro '{macro}' completed: {success}/{len(steps)} steps successful."


def _model_response(result: dict, transcript: str) -> str:
    """model_control already answers in full sentences — speak them, rather
    than the generic 'Done. model status completed.' that says nothing about
    which model is answering."""
    payload = result.get("result")
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return "Model command completed."


def execute_action(action_name: str, params: dict) -> dict:
    """Execute an action by name."""
    try:
        actions_dir = Path(__file__).parent.parent / "actions"
        registry = discover_actions(actions_dir)
        if registry.has(action_name):
            # Use the registry's run method
            result = registry.run(action_name, params, {})
            return {"success": True, "result": result}
        else:
            return {"success": False, "error": f"Action '{action_name}' not found"}
    except Exception as e:
        logger.error(f"Execute action {action_name} failed: {e}")
        return {"success": False, "error": str(e)}


def get_action_names() -> set[str]:
    """Get set of all registered action names."""
    try:
        actions_dir = Path(__file__).parent.parent / "actions"
        registry = discover_actions(actions_dir)
        return registry.names()
    except Exception:
        return set()


TOOL = {
    "name": "voice_command",
    "description": "Process and execute voice commands with context awareness, macros, confirmations, voice auth, scheduling, and analytics",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "transcript": {
                "type": "string",
                "description": "The voice transcript to process"
            },
            "session_id": {
                "type": "string",
                "description": "Session ID for context (device/user identifier)",
                "default": "default"
            },
            "auto_confirm": {
                "type": "boolean",
                "description": "Skip confirmation for sensitive actions",
                "default": False
            },
            "return_audio": {
                "type": "boolean",
                "description": "Return TTS-ready response text",
                "default": True
            },
            "user_id": {
                "type": "string",
                "description": "User ID for voice authentication"
            },
            "audio_data": {
                "type": "array",
                "items": {"type": "number"},
                "description": "Audio samples for voice authentication"
            },
            "schedule_job": {
                "type": "boolean",
                "description": "Schedule this command as a recurring job",
                "default": False
            },
            "job_name": {
                "type": "string",
                "description": "Name for the scheduled job"
            },
            "job_cron": {
                "type": "string",
                "description": "Cron expression for scheduling (e.g., '0 9 * * 1-5' for weekdays 9am)"
            }
        },
        "required": ["transcript"]
    },
    "handler": voice_command_handler
}