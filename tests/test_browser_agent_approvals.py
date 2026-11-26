"""Offline tests for browser approvals (HUD + chat channels)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.browser_agent import approvals  # noqa: E402


def test_yes_no_parsing():
    for text in ("yes", "Yes, go ahead", "ok", "approve", "do it", "sure", "haan"):
        assert approvals.parse_yes_no(text) is True, text
    for text in ("no", "cancel", "stop", "don't", "nahi", "never mind"):
        assert approvals.parse_yes_no(text) is False, text
    for text in ("maybe", "what do you think", "", "open youtube"):
        assert approvals.parse_yes_no(text) is None, text


def test_request_and_resolve_for_the_same_chat():
    store = approvals.ApprovalStore()
    pending = store.request("telegram", "123", "send an email to bob", "needs confirmation")
    assert store.pending_for("telegram", "123") is pending

    resolved, note = store.resolve_for_chat("telegram", "123", True, by="telegram:123")
    assert resolved is pending
    assert "approved" in note
    assert store.pending_for("telegram", "123") is None


def test_another_chat_cannot_approve():
    store = approvals.ApprovalStore()
    store.request("telegram", "123", "send an email to bob", "reason")
    resolved, note = store.resolve_for_chat("telegram", "999", True)
    assert resolved is None
    assert "nothing is waiting" in note
    # Still parked for the real chat.
    assert store.pending_for("telegram", "123") is not None


def test_decline_is_recorded_and_clears_the_slot():
    store = approvals.ApprovalStore()
    pending = store.request("whatsapp", "abc", "buy headphones", "reason")
    resolved, note = store.resolve(pending.token, False, by="whatsapp:abc")
    assert resolved is pending
    assert "declined" in note
    assert store.pending_for("whatsapp", "abc") is None


def test_expired_request_cannot_be_approved():
    store = approvals.ApprovalStore()
    pending = store.request("telegram", "123", "trade btc", "reason", ttl_s=0.05)
    time.sleep(0.1)
    assert store.pending_for("telegram", "123") is None
    resolved, note = store.resolve(pending.token, True)
    assert resolved is None
    assert "no such pending" in note or "expired" in note


def test_newest_request_replaces_the_previous_one():
    store = approvals.ApprovalStore()
    first = store.request("telegram", "1", "task one", "r")
    second = store.request("telegram", "1", "task two", "r")
    assert store.pending_for("telegram", "1") is second
    resolved, _ = store.resolve(first.token, True)
    assert resolved is None
    assert store.pending_for("telegram", "1") is second


def test_cancel_by_token():
    store = approvals.ApprovalStore()
    pending = store.request("telegram", "1", "task", "r")
    assert store.cancel(pending.token) is True
    assert store.cancel(pending.token) is False
    assert len(store) == 0


def test_target_helpers_are_symmetric():
    """request_for_target and resolve_for_target must agree with the
    (channel, chat) API the runtime uses, whatever the target looks like."""
    store = approvals.ApprovalStore()
    for target in ("telegram:42", "whatsapp:abc-1", "telegram"):
        pending = store.request_for_target(target, "task", "reason")
        channel, chat = approvals.split_target(target)
        assert store.pending_for(channel, chat) is pending
        resolved, _ = store.resolve_for_target(target, True)
        assert resolved is pending, target

    pending = store.request_for_target("telegram:42", "task", "reason")
    resolved, note = store.resolve_for_target("telegram:99", True)
    assert resolved is None
    assert store.pending_for("telegram", "42") is pending


def test_public_view_hides_nothing_sensitive():
    store = approvals.ApprovalStore()
    store.request("telegram", "1", "send email to bob", "needs confirmation")
    view = store.list_pending()[0]
    assert view["channel"] == "telegram"
    assert "send email" in view["task"]
    assert "expires_in_s" in view
