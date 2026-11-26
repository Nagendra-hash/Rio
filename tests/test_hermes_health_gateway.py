"""Offline tests for the Hermes gateway probe.

Regression guard for a real mis-report (2026-11-16): a gateway started with
`hermes gateway ensure` has no systemd unit, and the probe called that FAIL
while Telegram was connected.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pytest  # noqa: E402

from actions import hermes_health as hh  # noqa: E402


@pytest.fixture
def state_files(tmp_path, monkeypatch):
    state = tmp_path / "gateway_state.json"
    lock = tmp_path / "gateway.lock"
    monkeypatch.setattr(hh, "_GATEWAY_STATE", state)
    monkeypatch.setattr(hh, "_GATEWAY_LOCK", lock)
    return state, lock


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_state_file_wins_over_lock(state_files):
    state, lock = state_files
    _write(lock, {"pid": 111})
    _write(state, {"pid": 222, "gateway_state": "running"})
    assert hh._read_gateway_state()["pid"] == 222
    lock.unlink()
    state.unlink()
    _write(lock, {"pid": 333})
    assert hh._read_gateway_state()["pid"] == 333
    lock.unlink()
    assert hh._read_gateway_state() == {}


def test_systemd_active_is_reported_as_running(monkeypatch):
    monkeypatch.setattr(hh, "_systemd_gateway_state",
                        lambda: ("loaded", "active", "Mon 2026-11-16 10:00:00"))
    ok, detail = hh._probe_gateway()
    assert ok is True
    assert "systemd" in detail and "10:00" in detail


def test_runtime_without_a_unit_is_healthy(state_files, monkeypatch):
    state, _ = state_files
    _write(state, {"pid": 4242, "gateway_state": "running",
                   "platforms": {"telegram": {"state": "connected"},
                                 "whatsapp": {"state": "fatal"}}})
    monkeypatch.setattr(hh, "_systemd_gateway_state", lambda: ("not-found", "inactive", ""))
    monkeypatch.setattr(hh, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(hh, "_pid_is_gateway", lambda pid: True)
    ok, detail = hh._probe_gateway()
    assert ok is True, detail
    assert "manual runtime" in detail and "telegram connected" in detail
    assert "whatsapp fatal" in detail


def test_stale_state_is_reported_as_down(state_files, monkeypatch):
    state, _ = state_files
    _write(state, {"pid": 20984, "gateway_state": "running"})
    monkeypatch.setattr(hh, "_systemd_gateway_state", lambda: ("not-found", "inactive", ""))
    monkeypatch.setattr(hh, "_pid_alive", lambda pid: False)
    ok, detail = hh._probe_gateway()
    assert ok is False
    assert "stale" in detail and "20984" in detail


def test_live_pid_that_is_not_a_gateway_is_rejected(state_files, monkeypatch):
    state, _ = state_files
    _write(state, {"pid": 7})
    monkeypatch.setattr(hh, "_systemd_gateway_state", lambda: ("not-found", "inactive", ""))
    monkeypatch.setattr(hh, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(hh, "_pid_is_gateway", lambda pid: False)
    ok, detail = hh._probe_gateway()
    assert ok is False and "not a Hermes gateway" in detail


def test_failed_unit_with_no_runtime_says_service_failed(state_files, monkeypatch):
    state, _ = state_files
    monkeypatch.setattr(hh, "_systemd_gateway_state", lambda: ("loaded", "failed", ""))
    monkeypatch.setattr(hh, "_probe_gateway_runtime",
                        lambda: (False, "gateway not running"))
    ok, detail = hh._probe_gateway()
    assert ok is False and "service failed" in detail


def test_falls_back_to_the_legacy_pidfile(monkeypatch, tmp_path):
    monkeypatch.setattr(hh, "_read_gateway_state", lambda: {})
    monkeypatch.setattr(hh, "_probe_gateway_pidfile", lambda: (True, "running (pid 9, pidfile)"))
    ok, detail = hh._probe_gateway_runtime()
    assert ok is True and "pidfile" in detail


def test_pid_alive_uses_a_real_pid():
    assert hh._pid_alive(os.getpid()) is True
    assert hh._pid_alive(999_999_999) is False


def test_pid_is_gateway_tolerates_non_linux():
    # On Linux the current process is not a gateway; on other platforms the
    # helper stays permissive rather than failing the probe.
    result = hh._pid_is_gateway(os.getpid())
    assert isinstance(result, bool)
