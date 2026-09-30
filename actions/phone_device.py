# actions/phone_device.py — control the user's PHONE from a JARVIS voice command.
#
# The PC cannot open WhatsApp on a handset. What CAN is the phone's own
# dashboard page: it speaks Android's intent:// and tel:/sms: URL dialects.
# This action forwards a structured command over the dashboard WebSocket and
# reports honestly how many phones received it (0 = say so, never pretend).
#
# Requires: the phone's browser open on the dashboard (role: phone announced).
# No USB debugging, no WiFi pairing, no ADB — just the paired web page.

TOOL = {
    "name": "phone_device",
    "description": (
        "Control the user's PHONE (not this PC): open an app, start a phone "
        "call, open the SMS app, toggle media, or set an alarm. Requires the "
        "phone's dashboard page to be open — the result reports whether it "
        "was delivered. Use for requests like 'open WhatsApp on my phone', "
        "'call someone', 'open Telegram', 'set an alarm for 6:30'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "One of: open_app, call, sms, media, alarm",
            },
            "app": {
                "type": "STRING",
                "description": "App name for open_app (e.g. whatsapp, telegram, youtube, instagram, maps, gmail, paytm, phonepe, zomato)",
            },
            "number": {
                "type": "STRING",
                "description": "Phone number (digits, with country code if known) for call or sms",
            },
            "text": {
                "type": "STRING",
                "description": "Optional message body for sms",
            },
            "time": {
                "type": "STRING",
                "description": "Alarm time HH:MM (24h) for action=alarm",
            },
        },
        "required": ["action"],
    },
    "handler": lambda parameters, **ctx: _run(parameters, ctx),
}


def _run(parameters: dict, ctx: dict) -> str:
    action = (parameters.get("action") or "").strip().lower()
    if not action:
        return "phone_device failed: no action given."

    dashboard = ctx.get("dashboard")
    if dashboard is None or getattr(dashboard, "_loop", None) is None:
        return ("phone_device failed: dashboard not running — I can't reach "
                "the phone right now.")

    cmd = {
        "type":   "phone_cmd",
        "action": action,
        "app":    (parameters.get("app") or "").strip(),
        "number": (parameters.get("number") or "").strip(),
        "text":   (parameters.get("text") or "").strip(),
        "time":   (parameters.get("time") or "").strip(),
    }

    import asyncio
    fut = asyncio.run_coroutine_threadsafe(
        dashboard.broadcast_phone_cmd(cmd), dashboard._loop
    )
    try:
        delivered = fut.result(timeout=5)
    except Exception:
        return "phone_device failed: no response from the phone dashboard."

    if delivered == 0:
        return ("No phone is connected to the dashboard. Ask the user to open "
                "the JARVIS dashboard on their phone first, then try again.")
    return f"Command '{action}' delivered to {delivered} phone."
