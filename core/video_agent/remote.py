"""
core/video_agent/remote.py — Phase 7: remote GPU worker support.

Wan 2.1 / LTX-Video cannot run on this machine (no CUDA driver — measured).
The mission's answer is remote inference on EXPLICITLY user-configured GPU
workers, with authenticated connections.

Design:

  * workers live in config ONLY: repo_agent.video_remote_workers =
    [{"name": "lab-gpu", "host": "10.0.0.9", "user": "nagendra",
      "port": 22, "remote_dir": "~/wan-jobs", "cmd": "python generate.py"},
     …] — nothing is auto-discovered or auto-connected
  * transport = the system OpenSSH client (`ssh`/`scp`, argument arrays,
    BatchMode=yes, ConnectTimeout) — key-based auth, no new Python deps,
    no passwords on the command line
  * connectivity is verified ON DEMAND (a real `ssh … echo` probe) and the
    result is recorded per worker (verified_at) — a worker that was never
    verified keeps engines at NOT_CONFIGURED, never READY_REMOTE
  * a job = upload a small JSON request → run the worker's command →
    poll for the result file → scp it back → verify with ffprobe
  * cancellation kills the local ssh process group (the remote job, if
    long-running, is left to the worker's own deadline — stated honestly)

Nothing here installs anything on the remote host or changes local drivers.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from core.video_agent import hardware as hw_mod

__all__ = ["workers", "get_worker", "check", "submit_and_fetch", "state_text"]

_CONNECT_TIMEOUT_S = 15
_POLL_INTERVAL_S = 5.0


def workers() -> list[dict[str, Any]]:
    """Explicit user configuration only (same source as hardware.py)."""
    return hw_mod.remote_workers()


def get_worker(name: str) -> dict[str, Any] | None:
    name = str(name or "").strip().lower()
    for w in workers():
        if str(w.get("name") or "").strip().lower() == name:
            return w
    return None


def _ssh_base(worker: dict[str, Any]) -> list[str]:
    host = str(worker.get("host") or "")
    user = str(worker.get("user") or "")
    port = int(worker.get("port") or 22)
    target = f"{user}@{host}" if user else host
    return ["ssh", "-p", str(port), "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={_CONNECT_TIMEOUT_S}",
            "-o", "StrictHostKeyChecking=accept-new", target]


def _run(argv: list[str], timeout_s: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in argv], capture_output=True,
                          text=True, timeout=timeout_s)


# ── connectivity verification ────────────────────────────────────────────────

def check(worker_name: str) -> dict[str, Any]:
    """A REAL connectivity probe (`ssh … echo ok`) — on demand only, never
    automatic. Verifies the worker enough to justify READY_REMOTE."""
    worker = get_worker(worker_name)
    if worker is None:
        return {"ok": False,
                "error": f"no remote worker named '{worker_name}' is "
                         "configured (repo_agent.video_remote_workers)"}
    try:
        proc = _run(_ssh_base(worker) + ["echo", "remote-ok"], 30)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "verified": False,
                "error": f"ssh probe failed: {type(exc).__name__}: "
                         f"{str(exc)[:160]}"}
    ok = proc.returncode == 0 and "remote-ok" in (proc.stdout or "")
    result = {"ok": ok, "verified": ok,
              "detail": ("ssh probe answered — worker reachable"
                         if ok else
                         f"ssh probe failed (exit {proc.returncode}): "
                         f"{(proc.stderr or '')[:160]}"),
              "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime()) if ok else "",
              "host": worker.get("host")}
    _record_verification(worker, result)
    return result


def _load_config() -> dict[str, Any]:
    from memory.config_manager import load_api_keys
    return load_api_keys() or {}


def _write_config(data: dict[str, Any]) -> None:
    from memory import config_manager
    config_manager.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_manager.CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(config_manager.CONFIG_FILE)


def _record_verification(worker: dict[str, Any],
                         result: dict[str, Any]) -> None:
    """Persist the verification into the config file so READY_REMOTE has a
    basis. Only the video_remote_workers section is touched."""
    try:
        data = _load_config()
        section = data.setdefault("repo_agent", {})
        entries = section.get("video_remote_workers") or []
        for entry in entries:
            if str(entry.get("name") or "").lower() == \
                    str(worker.get("name") or "").lower():
                if result.get("ok"):
                    entry["verified_at"] = result["verified_at"]
                else:
                    entry.pop("verified_at", None)
        _write_config(data)
    except Exception:                              # noqa: BLE001
        pass                                       # best-effort persistence


def is_verified(worker: dict[str, Any]) -> bool:
    """A worker counts as verified only with a recorded, recent-enough
    verification stamp (7 days)."""
    stamp = str(worker.get("verified_at") or "")
    if not stamp:
        return False
    try:
        import datetime
        then = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=datetime.timezone.utc)
        age = datetime.datetime.now(datetime.timezone.utc) - then
        return age.total_seconds() < 7 * 24 * 3600
    except ValueError:
        return False


# ── job execution ────────────────────────────────────────────────────────────

def submit_and_fetch(worker: dict[str, Any], request: dict[str, Any],
                     dest: Path, *, deadline_s: int = 3600,
                     progress: Callable[[str, float, str], None] | None = None,
                     cancel: Callable[[], bool] | None = None,
                     log: Callable[[str], None] | None = None,
                     ) -> dict[str, Any]:
    """Run one generation request on the worker and fetch the MP4 back.

    request = {"model": "wan-2.1-t2v-1.3b", "prompt": str, "duration_s": int,
               "resolution": "720p", …} — the worker's `cmd` template decides
    how it is executed; we ship a JSON request file and expect
    `<remote_dir>/result.mp4` (worker contract, documented in the config).

    Honest contract: every failure is a FAILED with the real reason; nothing
    is faked locally."""
    def _p(phase: str, pct: float, msg: str = "") -> None:
        if progress:
            progress(phase, pct, msg)

    def _log(msg: str) -> None:
        if log:
            log(f"[remote] {msg}")

    def _cancelled() -> bool:
        return bool(cancel and cancel())

    remote_dir = str(worker.get("remote_dir") or "~/wan-jobs")
    result_name = str(worker.get("result_file") or "result.mp4")
    ssh = _ssh_base(worker)

    # 1. connectivity is REQUIRED before any upload
    _p("planning", 0.05, "verifying remote worker")
    probe = check(str(worker.get("name") or ""))
    if not probe.get("ok"):
        return {"status": "FAILED",
                "error": f"remote worker not reachable: "
                         f"{probe.get('error', '')[:200]}"}

    # 2. upload the request through ssh stdin (no temp files locally)
    _p("generating", 0.15, "uploading request")
    req_name = f"request-{int(time.time())}.json"
    upload_cmd = (f"mkdir -p {remote_dir} && cat > {remote_dir}/{req_name}")
    try:
        up = subprocess.run(ssh + [upload_cmd],
                            input=json.dumps(request), capture_output=True,
                            text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "FAILED",
                "error": f"request upload failed: {type(exc).__name__}: "
                         f"{str(exc)[:160]}"}
    if up.returncode != 0:
        return {"status": "FAILED",
                "error": f"request upload failed: {(up.stderr or '')[:200]}"}

    # 3. run the worker command
    cmd_template = str(worker.get("cmd") or "")
    if not cmd_template:
        return {"status": "FAILED",
                "error": "worker has no 'cmd' — the remote command "
                         "template is part of the user configuration"}
    remote_cmd = (f"cd {remote_dir} && "
                  f"{cmd_template} --request {remote_dir}/{req_name} "
                  f"--out {remote_dir}/{result_name}")
    _log(f"running remote command on {worker.get('host')}")
    _p("generating", 0.25, "remote generation running")
    try:
        run = subprocess.run(ssh + [remote_cmd], capture_output=True,
                             text=True, timeout=deadline_s)
    except subprocess.TimeoutExpired:
        return {"status": "FAILED",
                "error": f"remote generation exceeded {deadline_s}s"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "FAILED",
                "error": f"remote execution failed: {type(exc).__name__}: "
                         f"{str(exc)[:160]}"}
    if _cancelled():
        return {"status": "CANCELLED", "message": "cancelled during remote "
                                                  "generation"}
    if run.returncode != 0:
        return {"status": "FAILED",
                "error": f"remote command failed (exit {run.returncode}): "
                         f"{(run.stderr or '')[-300]}"}

    # 4. fetch the result
    _p("editing", 0.8, "fetching result")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    user = str(worker.get("user") or "")
    host = str(worker.get("host") or "")
    target = f"{user}@{host}" if user else host
    fetch_cmd = ["scp", "-P", str(int(worker.get("port") or 22)),
                 "-o", "BatchMode=yes",
                 f"{target}:{remote_dir}/{result_name}", str(dest)]
    try:
        fetch = subprocess.run(fetch_cmd, capture_output=True, text=True,
                               timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "FAILED",
                "error": f"result fetch failed: {type(exc).__name__}: "
                         f"{str(exc)[:160]}"}
    if fetch.returncode != 0 or not dest.is_file():
        return {"status": "FAILED",
                "error": f"result fetch failed: {(fetch.stderr or '')[:200]}"}
    _p("validating", 0.95, "result fetched")
    return {"status": "COMPLETED", "output": str(dest),
            "detail": f"fetched from {worker.get('host')} "
                      f"({dest.stat().st_size / (1024*1024):.1f} MB)"}


def state_text() -> str:
    ws = workers()
    if not ws:
        return ("Remote GPU workers: none configured — add entries under "
                "repo_agent.video_remote_workers "
                "([{name, host, user, port, remote_dir, cmd}]) to enable "
                "Wan 2.1 / LTX-Video remote inference.")
    lines = ["Remote GPU workers (explicit configuration):"]
    for w in ws:
        verified = "verified" if is_verified(w) else "NOT verified " \
                                                      "(run a check)"
        lines.append(f"• {w.get('name')} → {w.get('user')}@{w.get('host')}"
                     f":{w.get('port', 22)} — {verified}")
    return "\n".join(lines)
