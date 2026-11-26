"""
jarvis-pdf-forward — Hermes gateway hook (event: agent:end).

WHEN IT FIRES
    After every Hermes agent turn that came from a messaging platform.
    context carries: platform, user_id, chat_id, thread_id, chat_type,
    session_id, message (the user's text, truncated to 500 chars),
    response, model, provider. Errors from this hook never break the
    gateway — the registry swallows them (gateway/hooks.py).

WHAT IT DOES
    Hermes ALREADY receives Telegram/WhatsApp documents: the gateway saves
    the media, the agent turn sees the local file path, and the user's
    message (caption) says what to do with it. This hook inspects the turn:

      * the message contains a PDF path that still exists on disk, AND
      * the caption shows remember-intent
        ("remember", "save this", "add to my library", "rag", …)

    → both are forwarded to Jarvis's RAG inbox:
        <jarvis>/scripts/jarvis_inbox.py --channel <platform> \
            --chat <chat_id> --text "<caption>" --file "<pdf path>"

    Jarvis then runs its own intent layer (remember → RAG ingest with OCR +
    vision + embeddings; summarize → read-once) and the reply arm in
    <jarvis>/main.py delivers the answer back through `hermes send` on the
    same channel. This hook is deliberately DUMB: no intent decisions here —
    Jarvis is the head (architecture spec §2).

WHY agent:end (AND NOT gateway_platform_event)
    gateway_platform_event fires only for reactions/edits — not documents.
    agent:end carries everything needed (message text with the saved media
    path, chat identity) and is an official, documented gateway event.

CONSENT
    Shell hooks need first-use consent, but DIRECTORY hooks (this kind) are
    trusted code in the user's own hooks dir — no allowlist dance.

INSTALL
    mkdir -p ~/.hermes/hooks/jarvis-pdf-forward
    rsync -a --delete hermes-hooks/jarvis-pdf-forward/ \
                 ~/.hermes/hooks/jarvis-pdf-forward/
    hermes gateway restart   (hooks load at gateway startup)

    The trailing slash on the source and the explicit mkdir matter. `cp -r
    src ~/.hermes/hooks/` looks equivalent and is not: it overwrites files that
    still exist in the repo but never removes ones that no longer do, so a
    deleted module (and __pycache__/) lingers in the hooks dir and can be
    picked up instead of the new code. rsync --delete makes a reinstall
    converge on exactly the repo contents, and is safe to re-run.

FINDING THE JARVIS CHECKOUT
    This hook is COPIED into ~/.hermes/hooks/, not imported from the repo, so
    __file__ points at ~/.hermes — the checkout is located by _find_inbox()
    below instead: JARVIS_HOME first, then the repo (when run from the source
    tree, e.g. under pytest), then a Mark-* directory in $HOME. Every candidate
    is validated by the presence of scripts/jarvis_inbox.py, so a rename of the
    checkout directory is picked up automatically.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_INBOX_REL = ("scripts", "jarvis_inbox.py")


def _find_inbox() -> Path | None:
    """Locate scripts/jarvis_inbox.py in the Jarvis checkout.

    Order: JARVIS_HOME (portable mode) → the repo this file lives in (source
    tree / tests) → a Mark-* checkout in $HOME. Candidates are accepted only
    if the script is actually there, so this survives the checkout being
    renamed or moved.
    """
    candidates: list[Path] = []
    env_home = os.environ.get("JARVIS_HOME", "").strip()
    if env_home:
        candidates.append(Path(env_home).expanduser())
    candidates.append(Path(__file__).resolve().parent.parent.parent)
    home = Path.home()
    candidates.extend(sorted(home.glob("Mark-*")))
    candidates.extend(sorted(home.glob("*/Mark-*")))
    for root in candidates:
        inbox = root.joinpath(*_INBOX_REL)
        if inbox.is_file():
            return inbox
    return None


# Where Jarvis's inbox script lives (this repo). None if no checkout found —
# handle() then declines to forward rather than firing a broken command.
_INBOX = _find_inbox()

# Explicit remember-intent words. Kept conservative: a bare "look at this"
# must NOT trigger a permanent ingestion — that is the read-once path.
_INTENT_RE = re.compile(
    r"\b(remember|memorize|memorise|save (this|it)|add to (my |the )?(library|"
    r"knowledge|rag)|ingest|index this)\b",
    re.IGNORECASE,
)

# PDF paths Hermes mentions in its turn (the gateway saves media under the
# Hermes home and hands the agent the path). Two passes: GREEDY first (a path
# with spaces must start at the FIRST slash, not the last — the lazy variant
# once matched only '/Pivot calcu.pdf' out of '/home/.../Pivot calcu.pdf'),
# then lazy as fallback. Every candidate is existence-checked anyway.
_PDF_RE_GREEDY = re.compile(r"/[\w.\- /]+\.pdf", re.IGNORECASE)
_PDF_RE_LAZY = re.compile(r"/[\w.\- ]+?\.pdf\b", re.IGNORECASE)

_JARVIS_ENV = {
    "PATH": f"{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": str(Path.home()),
}


def handle(event_type: str, context: dict) -> None:
    try:
        if event_type != "agent:end":
            return
        message = str(context.get("message") or "")
        response = str(context.get("response") or "")
        if not message:
            return
        if not _INTENT_RE.search(message):
            return  # no remember-intent → not the PDF flow (cheap exit)

        platform = str(context.get("platform") or "").strip().lower()
        chat_id = str(context.get("user_id") or context.get("chat_id") or "").strip()
        if not platform:
            return  # CLI/local turn — nothing to route back to
        if _INBOX is None:
            print(
                "[jarvis-pdf-forward] no Jarvis checkout found "
                "(set JARVIS_HOME) — cannot forward",
                flush=True,
            )
            return

        # Candidate PDF paths: from the user's caption first, then the
        # agent's response (Hermes often echoes the saved media path there).
        candidates: list[str] = []
        for source in (message, response):
            for rx in (_PDF_RE_GREEDY, _PDF_RE_LAZY):
                for m in rx.finditer(source):
                    p = m.group(0).strip()
                    if p not in candidates:
                        candidates.append(p)

        existing = [c for c in candidates if Path(c).exists()]
        pdf_path = max(existing, key=len) if existing else None
        if pdf_path is None:
            return  # intent but no on-disk PDF → Hermes handles it normally

        cmd = [
            "python3",
            str(_INBOX),
            "--channel",
            platform,
            "--chat",
            chat_id,
            "--text",
            message[:500],
            "--file",
            pdf_path,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, env=_JARVIS_ENV
            )
            ok = proc.returncode == 0
        except Exception as e:
            proc = None
            ok = False
            print(f"[jarvis-pdf-forward] inbox error: {e}", flush=True)
        if ok:
            print(
                f"[jarvis-pdf-forward] forwarded {Path(pdf_path).name} "
                f"from {platform}:{chat_id} to Jarvis",
                flush=True,
            )
        elif proc is not None:
            print(
                f"[jarvis-pdf-forward] inbox failed: "
                f"{(proc.stderr or proc.stdout or '').strip()[:200]}",
                flush=True,
            )
    except Exception as e:  # never break the gateway
        print(f"[jarvis-pdf-forward] error: {e}", flush=True)
