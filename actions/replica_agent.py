"""replica_agent.py — clean-room app rebuilds from a Google Play listing.

One action over the `orchestrator.agents.replica` pack: point it at a Play
Store link and it reverse-engineers the app from its PUBLIC listing, plans an
Android build, reads the app's real reviews to find what users hate, rebuilds
it as your own improved app, and verifies parity + rebrand before it ships.

    replica_agent(url="https://play.google.com/store/apps/details?id=…")
    replica_agent(url="…", action="recon")          # just the recon map
    replica_agent(url="…", action="clone", build=true)   # …and compile the APK
    replica_agent(url="…", reviews="/path/reviews.csv")  # rank real reviews
    replica_agent(action="status")                  # what a run produced

Clean-room rules are enforced: it reads the listing and the app's observable
behaviour only. It never decompiles the APK, never copies the original's code,
strings, artwork or branding, and it REFUSES a request that asks it to.

Runs in a worker thread with a chat-visible timeout, like run_agent: coding a
full app can take minutes, so a timeout reports honestly instead of lying.
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

_TIMEOUT_S = 1500  # a full pack run with an Android build is long
_QUICK_TIMEOUT_S = 120  # recon/plan/status without a build

_ACTIONS = {
    "clone",
    "recon",
    "plan",
    "design",
    "critic",
    "enhance",
    "build",
    "verify",
    "status",
    "list",
}


def _agent():
    from orchestrator.agents.replica.replica_agent import ReplicaAgent

    return ReplicaAgent()


def replica_agent(parameters: dict, player=None) -> str:
    p = parameters or {}
    url = str(p.get("url") or p.get("listing") or "").strip()
    action = str(p.get("action") or "").strip().lower()
    app_name = str(p.get("app_name") or "").strip()
    workspace = str(p.get("workspace") or "").strip()
    build = bool(p.get("build"))
    reviews = str(p.get("reviews") or "").strip()

    if action and action not in _ACTIONS:
        return (
            f"Unknown action '{action}'. Use one of: " f"{', '.join(sorted(_ACTIONS))}."
        )
    if not url and not action:
        return _help()

    # Seed the run's reviews.csv before the critic step reads it.
    if reviews:
        try:
            src = Path(reviews).expanduser()
            if src.is_file() and workspace:
                dest = Path(workspace).expanduser() / "replica" / "reviews.csv"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
        except Exception:
            pass

    meta = {}
    if action:
        meta["action"] = action
    if app_name:
        meta["app_name"] = app_name
    if workspace:
        meta["workspace"] = workspace
    if build:
        meta["build"] = True

    text = url or (f"{action} the replica project" if action else "")

    from orchestrator.contracts import AgentRequest, TaskType, new_task_id

    request = AgentRequest(
        task_id=new_task_id("replica"),
        task_type=TaskType.REPLICA,
        text=text,
        metadata=meta,
    )

    outcome: dict = {"done": False, "text": "", "status": "failed"}

    def _work():
        try:
            result = _agent().handle(request)
            outcome["status"] = str(getattr(result.status, "value", "failed"))
            outcome["text"] = str(result.text or "")
        except Exception as exc:  # never silent
            outcome["status"] = "failed"
            outcome["text"] = f"{type(exc).__name__}: {exc}"
        finally:
            outcome["done"] = True

    budget = (
        _TIMEOUT_S
        if (build or action in ("clone", "build", "verify"))
        else _QUICK_TIMEOUT_S
    )
    threading.Thread(target=_work, daemon=True, name="replica-agent").start()
    deadline = time.monotonic() + budget
    while not outcome["done"] and time.monotonic() < deadline:
        time.sleep(0.25)

    if not outcome["done"]:
        return (
            f"The replica run is still working (exceeded the {budget}s chat "
            f"budget). Its artifacts are under the workspace's replica/ "
            f"folder; ask for status shortly."
        )
    body = outcome["text"] or "(no output)"
    if outcome["status"] == "denied":
        return f"[replica refused] {body}"
    if outcome["status"] == "failed":
        return f"[replica incomplete] {body}"
    return body


def _help() -> str:
    return (
        "Give me a Google Play link and I'll rebuild the app clean-room as "
        "your own, improved version.\n"
        '  replica_agent(url="https://play.google.com/store/apps/'
        'details?id=…")\n'
        "Actions: clone (default), recon, plan, design, critic, enhance, "
        "build, verify, status.\n"
        "Add build=true to compile the APK, reviews=/path/reviews.csv to rank "
        "real user reviews.\n"
        "I only read the public listing and the app's observable behaviour. I "
        "never decompile the APK or copy the original's code, assets or "
        "branding, and I'll refuse a request that asks me to."
    )


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "replica_agent",
    "description": (
        "Clean-room rebuild of an Android app from its Google Play listing. "
        "Give it a Play Store URL and it recon's the app from the public "
        "listing, plans an Android build, reads the app's real reviews to "
        "find what users hate, rebuilds it as the user's own improved app, "
        "and verifies parity + rebrand before it ships. Use when the user "
        "says 'clone this app', 'make my own version of <app>', 'rebuild "
        "this Play Store app better', or pastes a play.google.com link and "
        "wants their own version. NEVER decompiles the APK or copies the "
        "original's code, assets or branding."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "url": {
                "type": "STRING",
                "description": "The Google Play Store app URL "
                "(play.google.com/store/apps/details?id=…).",
            },
            "action": {
                "type": "STRING",
                "description": "clone (default full pack), recon, plan, "
                "design, critic, enhance, build, verify, "
                "status, or list.",
            },
            "build": {
                "type": "BOOLEAN",
                "description": "Compile the Android APK (slow). Default false "
                "for recon/plan; set true to actually build.",
            },
            "app_name": {
                "type": "STRING",
                "description": "Optional name for the rebuilt app's folder.",
            },
            "workspace": {
                "type": "STRING",
                "description": "Optional folder to run the rebuild in "
                "(defaults to ~/Desktop/<App>Replica).",
            },
            "reviews": {
                "type": "STRING",
                "description": "Optional path to a reviews CSV "
                "(columns: source,url,date,rating,text) to "
                "rank real user feedback from.",
            },
        },
        "required": [],
    },
    "handler": replica_agent,
}
