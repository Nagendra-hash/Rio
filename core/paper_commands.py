"""
core/paper_commands.py — deterministic /paper_* Telegram commands.

Hermes forwards the message into the dashboard; main.py calls handle_command()
before handing the turn to the model, so these commands work even when no
Gemini session is connected. Replies go back through the SAME Hermes pipeline
(bw.send_hermes). PAPER ONLY — no command can enable live trading.

Everything here is simulated. Settings are persisted in
config/paper_trading.json and audited in paper_settings_history.
"""
from __future__ import annotations

import re

from core.paper_trading import (
    SETTING_ALIASES, SETTING_SPECS, format_balance, format_history,
    format_performance, format_positions, format_report, format_setting_value,
    format_status, get_engine,
)

_PREFIX = re.compile(r"^\s*\[INBOUND ([^\]]+)\]\s*", re.IGNORECASE)
_CMD = re.compile(
    r"^\s*/?(paper_[a-z_]+)(?:\s+(.*))?$",
    re.IGNORECASE,
)

_CONFIG_KEYS = {
    "capital": "starting_capital",
    "quantity": "fixed_quantity",
    "leverage": "leverage",
    "risk": "risk_percent",
    "rr": "reward_risk",
    "max_trades": "max_daily_trades",
    "max_positions": "max_open_positions",
    "daily_loss": "max_daily_loss_percent",
    "max_exposure": "max_exposure_percent",
    "fee": "fee_percent",
    "slippage": "slippage_percent",
    "settings": "settings",
}

# Command set that changes persisted settings (used by the audit actor too).
_SETTING_COMMANDS = {
    "paper_capital": "starting_capital",
    "paper_quantity": None,          # AUTO or a fixed number
    "paper_leverage": "leverage",
    "paper_rr": "reward_risk",
    "paper_risk": "risk_percent",
    "paper_max_trades": "max_daily_trades",
    "paper_max_positions": "max_open_positions",
    "paper_daily_loss": "max_daily_loss_percent",
    "paper_max_exposure": "max_exposure_percent",
    "paper_fee": "fee_percent",
    "paper_slippage": "slippage_percent",
}

_USAGE = (
    "PAPER TRADING COMMANDS (PAPER ONLY — no real orders)\n\n"
    "Status/account:\n"
    "/paper_status · /paper_balance · /paper_positions\n"
    "/paper_history · /paper_performance · /paper_report\n\n"
    "Monitoring:\n"
    "/paper_start · /paper_stop\n\n"
    "Settings:\n"
    "/paper_settings — show all settings\n"
    "/paper_capital 10000 — starting capital\n"
    "/paper_quantity 0.01 | /paper_quantity auto\n"
    "/paper_leverage 3 · /paper_rr 1:3 · /paper_risk 1\n"
    "/paper_max_trades 5 · /paper_max_positions 2\n"
    "/paper_daily_loss 3 · /paper_max_exposure 50\n"
    "/paper_fee 0.1 · /paper_slippage 0.05\n"
    "/paper_reset_settings confirm — restore defaults\n"
    "/paper_reset confirm — archive account\n\n"
    "/paper_config — this help"
)


def reply_target(text: str) -> str:
    """Extract the Hermes target from the inbound tag, else the home channel."""
    match = _PREFIX.match(str(text or ""))
    return match.group(1).strip() if match else "telegram"


def _actor_from(text: str) -> str:
    target = reply_target(text)
    return target or "telegram"


def _chat_id_from(target: str) -> str:
    """'telegram:12345' / 'telegram:12345:7' -> '12345'."""
    parts = str(target or "").split(":")
    return parts[1].strip() if len(parts) > 1 else ""


def _approved_chat_ids() -> list:
    """The chat ids Hermes already trusts (plugins config: allowed_chat_ids).

    Reuses the existing authorization list; no second allowlist is created.
    Returns [] when none is configured (nothing to enforce).
    """
    try:
        from memory.config_manager import get_plugin_config
    except Exception:
        return []
    ids = []
    for plugin in ("telegram_remote", "telegram"):
        try:
            raw = str((get_plugin_config(plugin) or {}).get("allowed_chat_ids")
                      or "")
        except Exception:
            continue
        for token in re.split(r"[,\s]+", raw):
            token = token.strip()
            if token:
                ids.append(token)
    return ids


def is_authorized(text: str) -> tuple[bool, str]:
    """(authorized, reason) for a command that changes persisted settings."""
    target = reply_target(text)
    if not target or target == "telegram":
        # Local/dashboard-originated command with no chat context: the caller
        # already passed the dashboard bearer auth.
        return True, ""
    approved = _approved_chat_ids()
    if not approved:
        return True, ""          # no allowlist configured in this install
    chat = _chat_id_from(target)
    variants = {chat, chat.lstrip("+"), chat.replace(" ", "")}
    if any(v and v in approved for v in variants):
        return True, ""
    return False, (f"⛔ Unauthorized: chat {chat or 'unknown'} is not in the "
                   "approved Telegram list. No setting was changed.")


def _changed_reply(result: dict, actor: str) -> str:
    spec = SETTING_SPECS[result["key"]]
    old = format_setting_value(result["key"], result["old"])
    new = format_setting_value(result["key"], result["new"])
    return (
        f"Paper trading {spec['label'].lower()} updated successfully.\n"
        f"Previous: {old}\n"
        f"New: {new}\n"
        f"Applies to: {spec.get('applies', 'New trades only.')}\n"
        "Existing positions: Unchanged.\n"
        f"Mode: PAPER TRADING ONLY."
    )


def _invalid_reply(key: str, error: str) -> str:
    spec = SETTING_SPECS.get(key) or {}
    bounds = []
    if spec.get("min") is not None:
        bounds.append(f"min {format_setting_value(key, spec['min'])}")
    if spec.get("max") is not None:
        bounds.append(f"max {format_setting_value(key, spec['max'])}")
    limit = f" ({', '.join(bounds)})" if bounds else ""
    return (f"⚠ Rejected: {error}{limit}.\n"
            f"Current {spec.get('label', key)}: "
            f"{format_setting_value(key, _current(key)) if spec else '—'}\n"
            "No setting was changed. PAPER TRADING ONLY.")


def _current(key: str):
    try:
        return get_engine().get_setting(key)["value"]
    except Exception:
        return SETTING_SPECS.get(key, {}).get("default")


def format_settings(engine=None) -> str:
    """The full readable configuration report."""
    engine = engine or get_engine()
    s = engine.status()
    settings = s.get("settings") or {}
    account = s.get("account") or {}
    mon = s.get("monitoring") or {}
    cur = account.get("quote_currency", "USDT")
    positions = s.get("positions") or []
    mode = settings.get("quantity_mode", "AUTO")
    if mode == "AUTO":
        qty_line = "AUTO (risk-based)"
        fixed_line = f"{settings.get('fixed_quantity', 0):g} (unused in AUTO)"
    else:
        qty_line = "FIXED"
        fixed_line = f"{settings.get('fixed_quantity', 0):g}"
    entries = "YES" if mon.get("accepting_entries") else "NO"
    trading = "ON" if mon.get("accepting_entries") else "OFF"
    return "\n".join([
        "PAPER TRADING CONFIGURATION",
        "",
        "Account:",
        f"- Starting capital: "
        f"{settings.get('starting_capital', 0):,.2f} {cur}",
        f"- Current balance: {account.get('balance', 0):,.2f} {cur}",
        f"- Equity: {account.get('equity', 0):,.2f} {cur}",
        f"- Available: {account.get('available_balance', 0):,.2f} {cur}",
        f"- Used margin: {account.get('used_margin', 0):,.2f} {cur}",
        "",
        "Execution:",
        f"- Quantity mode: {mode}",
        f"- Quantity: {qty_line}",
        f"- Fixed quantity: {fixed_line}",
        f"- Leverage: {settings.get('leverage', 1):g}x",
        f"- Trading fee: {settings.get('fee_percent', 0):g}%",
        f"- Slippage: {settings.get('slippage_percent', 0):g}%",
        "",
        "Risk:",
        f"- Risk per trade: {settings.get('risk_percent', 0):g}%",
        f"- Risk/reward: 1:{settings.get('reward_risk', 0):g}",
        f"- Maximum trades/day: {settings.get('max_daily_trades', 0)}",
        f"- Maximum open positions: {settings.get('max_open_positions', 0)}",
        f"- Maximum daily loss: {settings.get('max_daily_loss_percent', 0):g}%",
        f"- Maximum exposure: {settings.get('max_exposure_percent', 0):g}%",
        "",
        "Status:",
        f"- Paper trading: {trading}",
        f"- New entries allowed: {entries}",
        f"- Existing positions: {len(positions)}",
        f"- Symbols: {', '.join(mon.get('allowed_symbols') or [])}",
        "",
        "PAPER TRADING ONLY — NO REAL ORDERS.",
    ])


def format_config_help() -> str:
    """Available settings, ranges and usage examples."""
    lines = ["PAPER TRADING SETTINGS — AVAILABLE COMMANDS", ""]
    for command, key in _CONFIG_KEYS.items():
        if key == "settings":
            continue
        spec = SETTING_SPECS[key]
        bounds = []
        if spec.get("choices"):
            bounds.append("/".join(spec["choices"]))
        if spec.get("min") is not None and not spec.get("choices"):
            bounds.append(f"min {format_setting_value(key, spec['min'])}")
        if spec.get("max") is not None and not spec.get("choices"):
            bounds.append(f"max {format_setting_value(key, spec['max'])}")
        rng = f" ({', '.join(bounds)})" if bounds else ""
        lines.append(f"/paper_{command} <value>{rng}")
        lines.append(f"    {spec['label']} · current "
                     f"{format_setting_value(key, _current(key))}")
    lines += [
        "/paper_quantity auto | <number>",
        "/paper_settings · /paper_reset_settings confirm",
        "",
        "Examples:",
        "/paper_capital 10000",
        "/paper_quantity 0.01",
        "/paper_quantity auto",
        "/paper_leverage 3",
        "/paper_rr 1:3",
        "/paper_risk 2",
        "",
        "Changing a setting affects NEW trades only. Existing positions keep "
        "their entry, quantity, stop-loss, target, leverage and fees.",
        "",
        "PAPER TRADING ONLY — NO REAL ORDERS.",
    ]
    return "\n".join(lines)


def handle_command(text: str) -> str | None:
    """Return a reply for a /paper_* command, or None for a normal turn."""
    raw = _PREFIX.sub("", str(text or "")).strip()
    match = _CMD.match(raw)
    if not match:
        if raw.lower().startswith(("/paper", "paper_")):
            return _USAGE
        return None
    command = match.group(1).lower()
    args = (match.group(2) or "").strip()
    lower = args.lower()
    actor = _actor_from(text)

    mutating = (command in _SETTING_COMMANDS
                or command in ("paper_reset_settings", "paper_reset",
                               "paper_start", "paper_stop"))
    if mutating:
        allowed, reason = is_authorized(text)
        if not allowed:
            return reason

    try:
        engine = get_engine()

        # ── read-only ─────────────────────────────────────────────────
        if command in ("paper_status", "paper_health"):
            return format_status(engine)
        if command == "paper_positions":
            return format_positions(engine)
        if command == "paper_history":
            return format_history(engine)
        if command == "paper_balance":
            return format_balance(engine)
        if command == "paper_performance":
            return format_performance(engine)
        if command == "paper_report":
            return format_report(engine)
        if command == "paper_settings":
            return format_settings(engine)
        if command == "paper_config":
            return format_config_help()

        # ── monitoring ────────────────────────────────────────────────
        if command == "paper_start":
            engine.start_trading()
            return ("✅ PAPER entries enabled. Strategy monitoring continues; "
                    "no real orders are possible.\n\n" + format_status(engine))
        if command == "paper_stop":
            engine.stop_trading()
            return ("🛑 New PAPER entries stopped. Existing open positions "
                    "continue to be managed (SL/TP/invalidation).\n\n"
                    + format_status(engine))

        # ── settings mutation ─────────────────────────────────────────
        if command == "paper_reset_settings":
            if lower not in ("confirm", "yes", "true"):
                return ("Reset ALL paper settings to defaults? This does not "
                        "touch the account balance or open positions.\n"
                        "Send `/paper_reset_settings confirm` to proceed.")
            result = engine.reset_settings(actor=actor, source="telegram")
            changed = result.get("changed") or {}
            if not changed:
                return ("Settings already at defaults. Nothing changed.\n"
                        "PAPER TRADING ONLY.")
            lines = ["✅ Paper settings restored to defaults.",
                     f"Changed: {len(changed)} setting(s).", ""]
            for key in changed:
                lines.append(
                    f"- {SETTING_SPECS[key]['label']}: "
                    f"{format_setting_value(key, changed[key]['new'])}")
            lines += ["", "Existing positions: Unchanged.",
                      "Mode: PAPER TRADING ONLY."]
            return "\n".join(lines)

        if command == "paper_reset":
            if lower not in ("confirm", "yes", "true"):
                return ("Paper reset requires explicit confirmation. Send "
                        "`/paper_reset confirm`. The current paper account will "
                        "be archived; existing Binance/liquidity history is "
                        "untouched.")
            result = engine.reset(confirm=True)
            return ("✅ Paper account reset. Previous account archived: "
                    f"{result['archived_account']}\nNew account: "
                    f"{result['account_id']}\n\n" + format_status(engine))

        if command == "paper_quantity":
            if not args:
                return (f"Current quantity mode: {_current('quantity_mode')} · "
                        f"fixed quantity {format_setting_value('fixed_quantity', _current('fixed_quantity'))}\n"
                        "Use `/paper_quantity auto` or `/paper_quantity 0.01`.")
            if lower in ("auto", "risk", "risk-based"):
                result = engine.update_setting("quantity_mode", "AUTO",
                                               actor=actor, source="telegram")
                return (
                    "Paper trading quantity mode updated successfully.\n"
                    f"Previous: {result['old']}\n"
                    "New: AUTO (risk-based sizing from equity, risk % and stop distance)\n"
                    "Applies to: New trades only.\n"
                    "Existing positions: Unchanged.\n"
                    "Mode: PAPER TRADING ONLY.")
            if lower in ("fixed",):
                result = engine.update_setting("quantity_mode", "FIXED",
                                               actor=actor, source="telegram")
                return (
                    "Paper trading quantity mode updated successfully.\n"
                    f"Previous: {result['old']}\n"
                    f"New: FIXED ({format_setting_value('fixed_quantity', _current('fixed_quantity'))})\n"
                    "Applies to: New trades only.\n"
                    "Existing positions: Unchanged.\n"
                    "Mode: PAPER TRADING ONLY.")
            try:
                fixed = engine.update_setting("fixed_quantity", args,
                                              actor=actor, source="telegram")
            except ValueError as exc:
                return _invalid_reply("fixed_quantity", str(exc))
            mode = engine.update_setting("quantity_mode", "FIXED",
                                         actor=actor, source="telegram")
            return (
                "Paper trading fixed quantity updated successfully.\n"
                f"Previous: {format_setting_value('fixed_quantity', fixed['old'])}\n"
                f"New: {format_setting_value('fixed_quantity', fixed['new'])}"
                f" (mode: {mode['new']})\n"
                "Applies to: New trades only.\n"
                "Existing positions: Unchanged.\n"
                "Mode: PAPER TRADING ONLY.")

        if command in _SETTING_COMMANDS:
            key = _SETTING_COMMANDS[command]
            if key is None:
                return _USAGE
            if not args:
                return (f"Current {SETTING_SPECS[key]['label']}: "
                        f"{format_setting_value(key, _current(key))}\n"
                        f"Send `/paper_{command[6:]} <value>` to change it.")
            try:
                result = engine.update_setting(key, args, actor=actor,
                                               source="telegram")
            except ValueError as exc:
                return _invalid_reply(key, str(exc))
            if not result.get("changed"):
                return (f"{SETTING_SPECS[key]['label']} is already "
                        f"{format_setting_value(key, result['new'])}. "
                        "Nothing changed.\nPAPER TRADING ONLY.")
            return _changed_reply(result, actor)

        return _USAGE
    except Exception as exc:
        return (f"Paper command failed: {type(exc).__name__}: "
                f"{str(exc)[:240]}. No setting was changed and no order was "
                "placed.")
