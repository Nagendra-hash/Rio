"""
core/repo_agent/skills.py — the typed skill runner.

This is the ONLY path by which Jarvis (voice, dashboard, Hermes) executes
code inside a managed repository. There is deliberately no generic
"run this shell command" tool:

    * the operation must exist in the skill manifest's allowlist
    * arguments are validated against the manifest's input schema
      (unknown params rejected, control characters rejected, length capped)
    * the command template is a fixed argv — placeholders are substituted
      with validated values, never with a shell string
    * stdout/stderr are captured through a redaction filter into separate
      per-job log files
    * runtime is bounded by the manifest's timeout; cancellation kills the
      whole process group
    * declared outputs are verified to exist with nonzero size before the
      job may be reported COMPLETED
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from core.repo_agent import manifests as M
from core.repo_agent.security import (RepoSecurityError, coerce_param,
                                      redact, safe_dir_name, scrub_env)

__all__ = ["run_skill", "skill_health", "load_skill", "resolve_skill"]

ProgressFn = Callable[[str, float, str], None]
CancelFn = Callable[[], bool]


def load_skill(skill_id: str) -> dict[str, Any]:
    """Resolve a skill id → registry entry + on-disk manifest."""
    skill_id = safe_dir_name(skill_id)
    reg = M.load_registry()
    entry = reg["skills"].get(skill_id)
    if not entry:
        raise RepoSecurityError(f"unknown skill '{skill_id}'")
    manifest = M.load_skill_manifest(entry["repo"])
    if not manifest:
        raise RepoSecurityError(
            f"skill '{skill_id}' has no adapter manifest — reinstall the repo")
    manifest["_registry"] = entry
    return manifest


def resolve_skill(skill_id: str) -> dict[str, Any]:
    """Registry view of a skill (enabled / status / repo) without raising."""
    try:
        skill_id = safe_dir_name(skill_id)
        reg = M.load_registry()
        entry = reg["skills"].get(skill_id) or {}
        manifest = M.load_skill_manifest(entry.get("repo", "")) or {}
        return {"ok": bool(entry), "skill_id": skill_id, "entry": entry,
                "manifest": manifest}
    except Exception:
        return {"ok": False, "skill_id": skill_id, "entry": {}, "manifest": {}}


def _config_value(dotted: str) -> str:
    """Resolve a dotted path inside Mark-III's gitignored api_keys.json."""
    try:
        from memory.config_manager import load_api_keys
        value: Any = load_api_keys() or {}
        for part in str(dotted).split("."):
            if isinstance(value, list):
                value = value[int(part)]
            elif isinstance(value, dict):
                value = value.get(part)
            else:
                return ""
        return value if isinstance(value, str) else ""
    except Exception:
        return ""


def _build_env(manifest: dict[str, Any], repo_dir: Path) -> dict[str, str]:
    """Scrubbed env + fake HOME + secret env vars resolved at run time."""
    env = {
        "HOME": str(repo_dir / ".jarvis" / "home"),
        "UV_CACHE_DIR": str(M.tools_root() / ".cache" / "uv"),
        "PIP_CACHE_DIR": str(M.tools_root() / ".cache" / "pip"),
        "npm_config_cache": str(M.tools_root() / ".cache" / "npm"),
    }
    for var, spec in (manifest.get("env") or {}).items():
        if not isinstance(spec, dict):
            continue
        if spec.get("literal"):
            env[str(var)] = str(spec["literal"])
        elif spec.get("config"):
            value = _config_value(str(spec["config"]))
            if value:
                env[str(var)] = value
    return env


def _validate_args(manifest: dict[str, Any], op: str,
                   params: dict[str, Any]) -> dict[str, Any]:
    ops = manifest.get("entry_points") or {}
    if op not in ops:
        allowed = ", ".join(sorted(ops)) or "none"
        raise RepoSecurityError(
            f"operation '{op}' is not allowlisted for this skill "
            f"(allowed: {allowed})")
    entry = ops[op] or {}
    if entry.get("dangerous"):
        raise RepoSecurityError(f"operation '{op}' is disabled by policy")
    schema = (manifest.get("input_schema") or {}).get(op) or {}
    clean: dict[str, Any] = {}
    for name, value in (params or {}).items():
        clean[str(name)] = coerce_param(schema, str(name), value)
    for name, spec in schema.items():
        if spec.get("required") and clean.get(name) in (None, ""):
            raise RepoSecurityError(f"parameter '{name}' is required")
    return clean


def _render(template: list[str], values: dict[str, str]) -> list[str]:
    """Substitute {placeholders} in a fixed argv template."""
    out: list[str] = []
    for token in template:
        text = str(token)
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        if "{" in text and "}" in text:
            raise RepoSecurityError(
                f"unresolved placeholder in command template: {text[:80]}")
        out.append(text)
    return out


def _verify_outputs(patterns: list[str], values: dict[str, str]) -> list[str]:
    """Declared output globs must match ≥1 real, nonempty file."""
    found: list[str] = []
    for pattern in patterns or []:
        rendered = str(pattern)
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", value)
        for path in sorted(glob.glob(rendered)):
            p = Path(path)
            if p.is_file() and p.stat().st_size > 0:
                found.append(str(p))
    return found


def run_skill(skill_id: str, op: str, params: dict[str, Any], *,
              progress: ProgressFn | None = None,
              cancel: CancelFn | None = None,
              run_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute one allowlisted skill operation. Runs ONLY inside the detached
    worker. Returns a result dict; raises RepoCancelled on cancellation."""
    progress = progress or (lambda phase, pct, msg: None)
    cancel = cancel or (lambda: False)

    def _check() -> None:
        if cancel():
            raise RepoSecurityError("CANCELLED")

    manifest = load_skill(skill_id)
    registry = manifest.get("_registry") or {}
    if not registry.get("enabled", True):
        raise RepoSecurityError(f"skill '{skill_id}' is disabled")
    if str(registry.get("status", "READY")) != "READY":
        raise RepoSecurityError(
            f"skill '{skill_id}' is {registry.get('status')} — complete its "
            "configuration first")

    op = str(op or "").strip().lower()
    clean = _validate_args(manifest, op, params)
    entry = manifest["entry_points"][op]

    repo_dir = M.repo_dir(str(registry["repo"]))
    repo_manifest = M.load_repo_manifest(str(registry["repo"])) or {}
    venv_python = repo_manifest.get("venv_python", "")
    run_dir = Path(str((run_context or {}).get("out_dir") or "")).expanduser()
    if not run_dir:
        run_dir = repo_dir / ".jarvis" / "runs" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ".gitignore").write_text("*\n", encoding="utf-8")

    values = {**{k: ("" if v is None else str(v)) for k, v in clean.items()},
              "venv_python": venv_python, "repo": str(repo_dir),
              "out_dir": str(run_dir), "skill_id": str(skill_id)}

    # op-specific context supplied by the adapter (e.g. a generated config)
    prepare = manifest.get("prepare")
    if prepare and isinstance(prepare, dict) and prepare.get("module"):
        ctx = _invoke_prepare(prepare, skill_id, op, clean, run_dir)
        values.update({k: str(v) for k, v in (ctx or {}).items()})

    argv = _render(entry.get("cmd") or [], values)
    if not argv:
        raise RepoSecurityError(f"operation '{op}' has no command template")

    timeout = int(entry.get("timeout_s") or
                  (manifest.get("limits") or {}).get("run_timeout_s") or 3600)
    log_base = Path(str((run_context or {}).get("log_base") or
                    (M.logs_dir() / time.strftime("%Y%m%d-%H%M%S"))))
    out_log = log_base.with_suffix(".out.log")
    err_log = log_base.with_suffix(".err.log")

    env = _build_env(manifest, repo_dir)
    env.setdefault("OUT_DIR", str(run_dir))
    progress("running", 5, f"{skill_id}:{op} started")

    # Phase 5: live render progress — the adapter's wrapper prints its own
    # progress markers; map them to store phases via the manifest tables.
    import re as _re
    pattern = str((manifest.get("progress_pattern") or ""))
    stage_map = manifest.get("progress_stages") or {}
    line_rx = _re.compile(pattern) if pattern else None

    def _on_line(line: str) -> None:
        if not line_rx:
            return
        m = line_rx.search(line)
        if not m:
            return
        stage = m.group(1).lower()
        hit = stage_map.get(stage)
        if hit:
            progress(str(hit[0]), float(hit[1]),
                     f"{skill_id}:{op}: {stage}")

    started = time.time()
    code, ram_peak = _pump_run(argv, cwd=repo_dir, env=env, timeout=timeout,
                               out_log=out_log, err_log=err_log,
                               cancel=cancel, on_line=_on_line)
    runtime = time.time() - started

    patterns = (manifest.get("outputs") or {}).get(op) or []
    outputs = _verify_outputs(patterns, values) if code == 0 else []
    if cancel():
        return {"status": "CANCELLED", "op": op,
                "runtime_s": round(runtime, 1), "out_log": str(out_log),
                "err_log": str(err_log), "outputs": [],
                "out_dir": str(run_dir),
                "message": "cancelled by user"}
    ok = code == 0 and (not patterns or bool(outputs))
    status = "COMPLETED" if ok else "FAILED"
    result: dict[str, Any] = {
        "status": status, "op": op, "runtime_s": round(runtime, 1),
        "ram_peak_mb": ram_peak, "out_log": str(out_log),
        "err_log": str(err_log), "outputs": outputs,
        "out_dir": str(run_dir),
        "message": (f"{skill_id}:{op} finished in {runtime:.0f}s with "
                    f"{len(outputs)} artifact(s)")
        if ok else _failure_message(code, err_log, out_log),
    }
    if code == 0 and patterns and not outputs:
        result["error"] = ("process exited cleanly but no declared output "
                           "artifact was produced — not reporting success")
    elif code != 0:
        result["error"] = _failure_message(code, err_log, out_log)
    return result


def _invoke_prepare(prepare: dict[str, Any], skill_id: str, op: str,
                    clean: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Run the adapter's prepare hook (e.g. generate provider config)."""
    import importlib
    module = importlib.import_module(str(prepare["module"]))
    fn = getattr(module, str(prepare.get("function") or "prepare"))
    return fn(skill_id=skill_id, op=op, params=clean, run_dir=run_dir) or {}


def _pump_run(argv: list[str], *, cwd: Path, env: dict[str, str],
              timeout: int, out_log: Path, err_log: Path,
              cancel: CancelFn,
              on_line: "Callable[[str], None] | None" = None
              ) -> tuple[int, float]:
    """Run argv with REDACTED, separated stdout/stderr logs, a deadline and
    a cancel poll. `on_line` (Phase 5) receives each stdout line as it
    arrives so skill adapters can turn their own progress markers into live
    job progress. Returns (returncode, child peak RAM MB)."""
    deadline = time.time() + timeout
    out_log.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace", start_new_session=True)
    peak_mb = {"v": 0.0}

    def _pump(stream, path: Path) -> None:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                for line in iter(stream.readline, ""):
                    fh.write(redact(line))
                    fh.flush()
                    if on_line is not None and stream is proc.stdout:
                        try:
                            on_line(line.rstrip("\n"))
                        except Exception:
                            pass
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    threads = [threading.Thread(target=_pump, args=(proc.stdout, out_log),
                                daemon=True),
               threading.Thread(target=_pump, args=(proc.stderr, err_log),
                                daemon=True)]
    for t in threads:
        t.start()
    while True:
        try:
            code = proc.poll()
        except Exception:
            code = None
        if code is not None:
            break
        if time.time() > deadline:
            _kill_group(proc)
            return 124, peak_mb["v"]
        if cancel():
            _kill_group(proc)
            return 130, peak_mb["v"]
        peak_mb["v"] = max(peak_mb["v"], _child_rss_mb(proc.pid))
        time.sleep(1.0)
    for t in threads:
        t.join(timeout=5)
    return int(code), peak_mb["v"]


def _kill_group(proc: subprocess.Popen) -> None:
    import signal
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            return
    deadline = time.time() + 5
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.2)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _child_rss_mb(pid: int) -> float:
    """Peak RSS of the direct child via /proc — Linux only, best effort."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return float(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


def _failure_message(code: int, err_log: Path, out_log: Path) -> str:
    tail = ""
    for path in (err_log, out_log):
        try:
            lines = Path(path).read_text(errors="replace")\
                .strip().splitlines()
            if lines:
                tail = " | ".join(lines[-2:])[:280]
                break
        except Exception:
            continue
    reason = {124: "timed out", 130: "cancelled"}.get(code,
                                                      f"exit code {code}")
    return f"{reason}" + (f" — {tail}" if tail else "")


def skill_health(skill_id: str) -> dict[str, Any]:
    """Run the manifest's health check (allowlisted, short timeout)."""
    manifest = load_skill(skill_id)
    registry = manifest.get("_registry") or {}
    repo_dir = M.repo_dir(str(registry["repo"]))
    repo_manifest = M.load_repo_manifest(str(registry["repo"])) or {}
    entry = manifest.get("health_check") or {}
    if not entry.get("cmd"):
        return {"ok": bool(repo_manifest), "detail": "no health check defined"}
    values = {"venv_python": repo_manifest.get("venv_python", ""),
              "repo": str(repo_dir)}
    argv = _render(entry["cmd"], values)
    code, _ = _pump_run(argv, cwd=repo_dir, env=_build_env(manifest, repo_dir),
                        timeout=int(entry.get("timeout_s") or 60),
                        out_log=M.logs_dir() / f"{skill_id}-health.out.log",
                        err_log=M.logs_dir() / f"{skill_id}-health.err.log",
                        cancel=lambda: False)
    return {"ok": code == 0, "exit_code": code,
            "detail": entry.get("expect") or ""}
