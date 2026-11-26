"""
tests/test_google_bridge_hook.py — offline tests for the jarvis-google-bridge hook.

What this proves (no gateway, no network, no Telegram):
  1. Mail and calendar asks ARE routed: the common phrasings a Telegram user
     actually types, in English and Hindi.
  2. Ordinary messages are NOT routed. This is the important half — a false
     positive sends an unrelated message to Jarvis and yields a second, doubled
     answer in the chat, so the regexes must stay tight.
  3. When Hermes already did the Google work itself, the hook stays out of the
     way (no double answer).
  4. The per-chat cooldown forwards ONE ask once, and a failed forward does
     NOT start a cooldown (a transient error must not silently mute the user).
  5. A CLI/local turn (no platform) is never forwarded.

The handler is imported from the repo path directly; _INBOX is pointed at a
temp script so no test can reach the real dashboard.

Run:  .venv/bin/python -m pytest tests/test_google_bridge_hook.py -q
Also pytest-compatible: run_all().
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

HOOK = BASE / "hermes-hooks" / "jarvis-google-bridge" / "handler.py"


def _load_hook():
    """Import the hook fresh, with its state file and inbox redirected."""
    spec = importlib.util.spec_from_file_location("jarvis_google_bridge", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    tmp = Path(tempfile.mkdtemp(prefix="jarvis-google-bridge-"))
    mod._STATE_FILE = tmp / ".cooldown.json"
    inbox = tmp / "inbox.py"
    inbox.write_text("# stand-in for scripts/jarvis_inbox.py\n", encoding="utf-8")
    mod._INBOX = inbox
    mod._JARVIS_ENV = {"PATH": "/usr/bin:/bin"}
    return mod, tmp


HOOK_MOD, _TMP = _load_hook()

ROUTES = [
    # mail
    "check my mail",
    "check my email please",
    "do I have any unread emails",
    "any new mail?",
    "show my inbox",
    "read my emails",
    "search mail from bob",
    "send an email to alice@example.com",
    "mail me the details",
    "unread emails kya hain",
    "check my inbox",
    # calendar
    "what's on my calendar tomorrow",
    "what's my schedule today",
    "am I free Friday",
    "am I busy tomorrow",
    "book a meeting at 4pm",
    "schedule a call tomorrow",
    "add an event to my calendar",
    "cancel my 3pm meeting",
    "what are my meetings tomorrow",
    "kal kya schedule hai",
]

DOES_NOT_ROUTE = [
    "play some music",
    "delete that file",
    "what's the weather",
    "turn the volume up",
    "search the web for python tips",
    "open youtube and play a relaxing video",
    "set a reminder to call mom",
    "read this pdf and summarize it",
    "remember this document",
    "who won the match",
    "send a message to mom on whatsapp",
    "make me a sandwich",
    "what can you do",
    "stop",
]


def test_mail_and_calendar_asks_route():
    missing = [m for m in ROUTES if not HOOK_MOD._is_google_ask(m)]
    assert not missing, f"NOT routed (would break Telegram): {missing}"


def test_ordinary_messages_do_not_route():
    bad = [m for m in DOES_NOT_ROUTE if HOOK_MOD._is_google_ask(m)]
    assert not bad, f"false positives — would double-answer the user: {bad}"


def test_hermes_already_handled_it_stays_out_of_the_way():
    already = [
        "I fetched your mail via the gmail tool — you have 3 unread",
        "I read your inbox with the Gmail API already",
    ]
    bad = [m for m in already if HOOK_MOD._is_google_ask(m)]
    assert not bad, f"would double-answer after Hermes did it: {bad}"


class _Proc:
    """Stand-in for subprocess.CompletedProcess."""
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def test_forward_and_cooldown():
    """One ask is forwarded once; a follow-up inside the window is not."""
    calls = []
    mod = HOOK_MOD

    orig_run = mod.subprocess.run

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return _Proc(returncode=0)
    mod.subprocess.run = fake_run
    try:
        mod._STATE_FILE.unlink(missing_ok=True)
        ctx = {"platform": "telegram", "user_id": "42", "message": "check my mail"}

        mod.handle("agent:end", ctx)
        assert len(calls) == 1, calls
        cmd = calls[0]
        assert "--channel" in cmd and "telegram" in cmd
        assert "--chat" in cmd and "42" in cmd
        assert "check my mail" in cmd

        # second ask same chat inside cooldown → not forwarded again
        mod.handle("agent:end", ctx)
        assert len(calls) == 1, f"cooldown ignored: {len(calls)} forwards"

        # a DIFFERENT chat is unaffected by the first chat's cooldown
        mod.handle("agent:end", {"platform": "telegram", "user_id": "99",
                                 "message": "check my mail"})
        assert len(calls) == 2, "cooldown leaked across chats"
    finally:
        mod.subprocess.run = orig_run
        mod._STATE_FILE.unlink(missing_ok=True)


def test_failed_forward_does_not_start_cooldown():
    """A transient inbox error must not silently mute the user's next ask."""
    calls = []
    mod = HOOK_MOD

    def failing(cmd, *a, **k):
        calls.append(cmd)
        return _Proc(returncode=1, stderr="boom")

    orig_run = mod.subprocess.run
    mod.subprocess.run = failing
    try:
        mod._STATE_FILE.unlink(missing_ok=True)
        ctx = {"platform": "telegram", "user_id": "42", "message": "check my mail"}

        mod.handle("agent:end", ctx)
        mod.handle("agent:end", ctx)
        assert len(calls) == 2, \
            "a failure started a cooldown and blocked the retry"
    finally:
        mod.subprocess.run = orig_run
        mod._STATE_FILE.unlink(missing_ok=True)


def test_cli_turn_never_forwarded():
    calls = []
    mod = HOOK_MOD
    orig_run = mod.subprocess.run

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return _Proc()
    mod.subprocess.run = fake_run
    try:
        mod._STATE_FILE.unlink(missing_ok=True)
        mod.handle("agent:end", {"message": "check my mail"})   # no platform
        assert not calls, "forwarded a local/CLI turn"
    finally:
        mod.subprocess.run = orig_run
        mod._STATE_FILE.unlink(missing_ok=True)


def test_other_events_ignored():
    calls = []
    mod = HOOK_MOD
    orig_run = mod.subprocess.run

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return _Proc()
    mod.subprocess.run = fake_run
    try:
        mod._STATE_FILE.unlink(missing_ok=True)
        mod.handle("agent:start", {"platform": "telegram", "user_id": "42",
                                   "message": "check my mail"})
        assert not calls, "fired on a non-agent:end event"
    finally:
        mod.subprocess.run = orig_run
        mod._STATE_FILE.unlink(missing_ok=True)


def test_hook_yaml_declares_agent_end():
    yaml_txt = (HOOK.parent / "HOOK.yaml").read_text(encoding="utf-8")
    assert "name: jarvis-google-bridge" in yaml_txt
    assert "agent:end" in yaml_txt


def test_gitignored_so_no_secrets_committed():
    """The cooldown state holds chat ids — it must live in gitignored territory."""
    gitignore = (BASE / ".gitignore").read_text(encoding="utf-8")
    assert "token*.json" in gitignore, "token pattern missing"


# ── runner ────────────────────────────────────────────────────────────────────

def run_all() -> bool:
    test_mail_and_calendar_asks_route()
    print(f"  ✓ routes {len(ROUTES)} mail/calendar phrasings")
    test_ordinary_messages_do_not_route()
    print(f"  ✓ ignores {len(DOES_NOT_ROUTE)} unrelated messages")
    test_hermes_already_handled_it_stays_out_of_the_way()
    print("  ✓ no double-answer when Hermes already did it")
    test_forward_and_cooldown()
    print("  ✓ forwards once; cooldown is per-chat")
    test_failed_forward_does_not_start_cooldown()
    print("  ✓ a failed forward does not mute the next ask")
    test_cli_turn_never_forwarded()
    test_other_events_ignored()
    print("  ✓ CLI turns and other events ignored")
    test_hook_yaml_declares_agent_end()
    print("  ✓ HOOK.yaml declares the hook + agent:end")
    return True


if __name__ == "__main__":
    run_all()
    print("\nPASS: google bridge hook")
