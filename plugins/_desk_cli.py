"""
plugins/_desk_cli.py — command-line door into the Market Intelligence Desk.

Built for the Hermes bridge (see plugins/_desk_engine.py): the Hermes skill
'market-desk' tells the agent to run this CLI, so Telegram/WhatsApp users can
query the desk through Hermes without Hermes knowing the handoff schema.

Usage:
  python3 plugins/_desk_cli.py ask "<question>" [--companies TCS,BTC] [--depts N]
  python3 plugins/_desk_cli.py status
  python3 plugins/_desk_cli.py run-now [--budget N]
  python3 plugins/_desk_cli.py routines
  python3 plugins/_desk_cli.py activate <routine_id> | pause <routine_id>
  python3 plugins/_desk_cli.py universe

The CLI is deliberately thin: it forwards to plugins._desk_engine / market_desk
and prints human-readable text. It never bypasses the desk's safety model —
no trades, no universe expansion, paused-until-activated routines, budget caps.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins._desk_engine import (  # noqa: E402
    ROUTINES,
    ask_desk,
    run_schedule,
    set_routine_active,
    _load_state,
)
from plugins._desk_roster import load_universe  # noqa: E402


def _cmd_ask(argv: list[str]) -> str:
    question = argv[0] if argv else ""
    if not question:
        return 'usage: ask "<question>" [--companies SYM1,SYM2] [--depts N]'
    companies: list[str] = []
    depts = 2
    i = 1
    while i < len(argv):
        if argv[i] == "--companies" and i + 1 < len(argv):
            companies = [c.strip() for c in argv[i + 1].split(",") if c.strip()]
            i += 2
        elif argv[i] == "--depts" and i + 1 < len(argv):
            try:
                depts = max(1, min(4, int(argv[i + 1])))
            except ValueError:
                pass
            i += 2
        else:
            i += 1
    return ask_desk(question, companies=companies or None, max_depts=depts)


def _cmd_status(_: list[str]) -> str:
    state = _load_state()
    cursors = state.get("cursors", {})
    lines = [f"Routines ({len(ROUTINES)}):"]
    for r in ROUTINES:
        active = state.get("routines", {}).get(r["id"], {}).get("active", False)
        lines.append(
            f"  {r['id']}: {r['time']} IST · "
            f"{'ACTIVE' if active else 'paused'} · "
            f"last {cursors.get(r['id'], 'never')}"
        )
    return "\n".join(lines)


def _cmd_run_now(argv: list[str]) -> str:
    budget = None
    if len(argv) >= 2 and argv[0] == "--budget":
        try:
            budget = int(argv[1])
        except ValueError:
            pass
    return run_schedule(max_calls=budget)


def _cmd_routines(_: list[str]) -> str:
    return "\n".join(r["id"] for r in ROUTINES)


def _cmd_universe(_: list[str]) -> str:
    uni = load_universe()
    return "\n".join(
        f"{c['symbol']} — {c['name']} ({c['market']})" for c in uni["companies"]
    )


def _cmd_set(rid: str, active: bool) -> str:
    return set_routine_active(rid, active)


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "ask":
        print(_cmd_ask(rest))
    elif cmd == "status":
        print(_cmd_status(rest))
    elif cmd == "run-now":
        print(_cmd_run_now(rest))
    elif cmd == "routines":
        print(_cmd_routines(rest))
    elif cmd == "universe":
        print(_cmd_universe(rest))
    elif cmd in ("activate", "pause"):
        if not rest:
            print("usage: activate|pause <routine_id>  (see: routines)")
            return 2
        print(_cmd_set(rest[0], cmd == "activate"))
    else:
        print(
            f"unknown command '{cmd}' — try ask|status|run-now|routines|"
            f"universe|activate|pause"
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
