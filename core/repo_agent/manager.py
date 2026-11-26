"""
core/repo_agent/manager.py — the Universal Repository Manager.

Responsible for the full lifecycle of a managed GitHub repository:

    inspect(url)                 read-only suitability + risk report
    install(url, …)              clone → static scan → isolated env → deps
                                 → smoke test → manifest + registry
    update(name, …)              fast-forward the pinned commit + re-sync deps
    remove(name, …)              delete the managed directory (caller confirms)
    smoke_test_dir(...)          best-effort import/health verification

Hard rules enforced here:

    * only canonical github.com URLs; the name can never escape ~/JarvisTools
    * NEVER runs README shell snippets — only the repository's own declared
      package manager (uv sync / pip / npm install) inside an ISOLATED env:
      dedicated venv, per-repo fake $HOME, scrubbed environment, no sudo
    * installation runs in the detached worker process, never in the
      conversation loop (see scripts/repo_agent_run.py)
    * a failed install is rolled back (directory removed) and reported FAILED
    * every install records the exact commit + dependency manifest
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from core.repo_agent import manifests as M
from core.repo_agent.security import (RepoSecurityError, redact, risk_summary,
                                      safe_dir_name, scan_text_risk,
                                      scrub_env, validate_repo_url)

__all__ = ["RepoCancelled", "RepositoryManager", "free_disk_gb", "find_uv"]

ProgressFn = Callable[[str, float, str], None]
CancelFn = Callable[[], bool]

_INSTALL_TIMEOUT_S = 1800
_CMD_TIMEOUT_S = 600
_GIT_TIMEOUT_S = 300


class RepoCancelled(RuntimeError):
    """Raised inside a long operation when the user cancelled the job."""


def free_disk_gb(path: Path | None = None) -> float:
    try:
        usage = shutil.disk_usage(str(path or Path.home()))
        return usage.free / (1024 ** 3)
    except Exception:
        return -1.0


def find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (Path.home() / ".local/bin/uv", Path("/usr/local/bin/uv")):
        if candidate.exists():
            return str(candidate)
    return None


class RepositoryManager:
    """Lifecycle manager. Long methods take progress/cancel callbacks because
    they only ever run inside the detached worker."""

    def __init__(self, progress: ProgressFn | None = None,
                 cancel: CancelFn | None = None) -> None:
        self._progress = progress or (lambda phase, pct, msg: None)
        self._cancel = cancel or (lambda: False)

    # ── internals ────────────────────────────────────────────────────────
    def _check_cancel(self) -> None:
        if self._cancel():
            raise RepoCancelled("cancelled by user")

    def _run(self, argv: list[str], *, cwd: Path | None = None,
             env: dict[str, str] | None = None,
             timeout: int = _CMD_TIMEOUT_S) -> tuple[int, str]:
        """Run one bounded subprocess, redact its output, honor cancel."""
        self._check_cancel()
        merged = scrub_env(env)
        try:
            proc = subprocess.run(
                argv, cwd=str(cwd) if cwd else None, env=merged,
                capture_output=True, text=True, timeout=timeout,
                errors="replace")
            out = redact(f"{proc.stdout}\n{proc.stderr}".strip())
            return proc.returncode, out[-4000:]
        except subprocess.TimeoutExpired:
            return 124, f"command timed out after {timeout}s: {argv[0]}"
        except FileNotFoundError:
            return 127, f"executable not found: {argv[0]}"

    def _tick(self, phase: str, pct: float, msg: str = "") -> None:
        self._progress(phase, pct, msg)

    # ── inspect (read-only, safe) ────────────────────────────────────────
    def inspect(self, url: str) -> dict[str, Any]:
        """Static suitability + risk report WITHOUT executing anything."""
        owner, repo = validate_repo_url(url)
        name = safe_dir_name(repo)
        report: dict[str, Any] = {
            "url": f"https://github.com/{owner}/{repo}", "owner": owner,
            "repo": repo, "name": name,
            "dest": str(M.repo_dir(name)),
            "already_installed": M.load_repo_manifest(name) is not None,
        }
        code, out = self._run(
            ["git", "ls-remote", "--heads", report["url"]],
            timeout=_GIT_TIMEOUT_S)
        report["reachable"] = code == 0
        if code != 0:
            report["error"] = out.splitlines()[-1] if out else "unreachable"
            return report
        report["head_branch"] = (out.splitlines() or [""])[0].split("\t")[-1]

        # shallow clone into a throwaway dir purely to READ metadata
        tmp = M.tools_root() / ".inspect-tmp" / name
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            code, out = self._run(
                ["git", "clone", "--depth", "1", "--quiet",
                 report["url"], str(tmp)], timeout=_GIT_TIMEOUT_S)
            if code != 0:
                report["error"] = out[-400:]
                return report
            report.update(self._read_tree(tmp))
            report["risk_level"] = risk_summary(report["risk_findings"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return report

    def _read_tree(self, tree: Path) -> dict[str, Any]:
        """Read-only metadata extraction from a checked-out tree."""
        info: dict[str, Any] = {"languages": [], "dependency_manifests": [],
                                "entry_points": [], "python_requires": None,
                                "license": None, "risk_findings": [],
                                "has_uv_lock": False, "has_node": False,
                                "install_plan": [], "notes": []}
        license_file = next((f for f in ("LICENSE", "LICENSE.md", "LICENSE.txt")
                             if (tree / f).exists()), None)
        if license_file:
            try:
                blob = (tree / license_file).read_text(
                    errors="replace")[:400].lower()
                for lic in ("mit", "apache", "bsd", "gpl", "lgpl", "mpl"):
                    if lic in blob:
                        info["license"] = lic.upper()
                        break
            except Exception:
                pass
        else:
            info["notes"].append("no LICENSE file found")
        for fname, lang in (("pyproject.toml", "python"),
                            ("requirements.txt", "python"),
                            ("setup.py", "python"),
                            ("package.json", "node"),
                            ("Cargo.toml", "rust"), ("go.mod", "go")):
            if (tree / fname).exists():
                info["dependency_manifests"].append(fname)
                if lang not in info["languages"]:
                    info["languages"].append(lang)
        info["has_uv_lock"] = (tree / "uv.lock").exists()
        info["has_node"] = (tree / "package.json").exists()
        pyproject = tree / "pyproject.toml"
        if pyproject.exists():
            text = pyproject.read_text(errors="replace")
            for line in text.splitlines():
                if "requires-python" in line and "=" in line:
                    info["python_requires"] = \
                        line.split("=", 1)[1].strip("\"' ,")
                    break
            if "[project.scripts]" in text:
                section = text.split("[project.scripts]", 1)[1]
                section = section.split("[", 1)[0]
                info["entry_points"].extend(
                    line.split("=", 1)[0].strip()
                    for line in section.splitlines() if "=" in line)
        for scan_target in ("README.md", "readme.md", "README.rst",
                            "Makefile", "setup.sh", "install.sh"):
            path = tree / scan_target
            if path.exists():
                try:
                    info["risk_findings"].extend(
                        {**f, "file": scan_target}
                        for f in scan_text_risk(
                            path.read_text(errors="replace")[:200000]))
                except Exception:
                    pass
        seen, unique = set(), []
        for f in info["risk_findings"]:
            key = (f.get("finding"), f.get("file"))
            if key not in seen:
                seen.add(key)
                unique.append(f)
        info["risk_findings"] = unique
        # the install plan we WOULD follow (uv/pip/npm only — never scripts)
        if info["has_uv_lock"] and find_uv():
            info["install_plan"].append("uv sync (isolated uv environment)")
        elif "requirements.txt" in info["dependency_manifests"]:
            info["install_plan"].append(
                "isolated venv + pip install -r requirements.txt")
        elif info["has_node"]:
            info["install_plan"].append("npm install (project-local)")
        if not info["install_plan"]:
            info["install_plan"].append(
                "no supported package manifest — manual review required")
            info["notes"].append("UNSUPPORTED: would need manual installation")
        try:
            size = sum(f.stat().st_size for f in tree.rglob("*")
                       if f.is_file()) / (1024 ** 2)
            info["clone_size_mb"] = round(size, 1)
        except Exception:
            pass
        return info

    # ── install ──────────────────────────────────────────────────────────
    def install(self, url: str, *, min_free_disk_gb: float = 10,
                allow_local: bool = False) -> dict[str, Any]:
        """Clone + inspect + provision. Returns a result dict — it NEVER
        raises except RepoCancelled (worker converts that to CANCELLED).

        allow_local: TEST-ONLY escape hatch to clone from a local path so the
        flow can be exercised offline. Never exposed to the LLM or voice.
        """
        if allow_local:
            source = str(url)
            name = safe_dir_name(Path(source).name.replace(".git", ""))
            repo_url = source
        else:
            owner, repo = validate_repo_url(url)
            name = safe_dir_name(repo)
            repo_url = f"https://github.com/{owner}/{repo}"
        dest = M.repo_dir(name)
        started = time.time()

        if M.load_repo_manifest(name) and (dest / ".git").exists():
            manifest = M.load_repo_manifest(name) or {}
            return {"ok": True, "already_installed": True, "name": name,
                    "status": manifest.get("status", "READY"),
                    "commit": manifest.get("commit", ""),
                    "message": f"{name} is already installed at {dest}"}

        free = free_disk_gb(M.tools_root())
        if 0 <= free < min_free_disk_gb:
            return {"ok": False, "status": "FAILED",
                    "error": f"only {free:.1f} GiB free "
                             f"(need {min_free_disk_gb} GiB) — nothing installed"}

        self._tick("clone", 5, f"cloning {repo_url}")
        self._check_cancel()
        staging = M.tools_root() / ".staging" / f"{name}-{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.parent.mkdir(parents=True, exist_ok=True)
        code, out = self._run(["git", "clone", "--depth", "1", "--quiet",
                               repo_url, str(staging)],
                              timeout=_GIT_TIMEOUT_S)
        if code != 0:
            shutil.rmtree(staging, ignore_errors=True)
            return {"ok": False, "status": "FAILED",
                    "error": f"git clone failed: {out[-300:]}"}

        self._tick("inspect", 25, "static risk scan")
        info = self._read_tree(staging)
        risk = risk_summary(info["risk_findings"])
        if not info["install_plan"] or \
                "no supported package manifest" in info["install_plan"][0]:
            shutil.rmtree(staging, ignore_errors=True)
            return {"ok": False, "status": "FAILED",
                    "error": "no supported dependency manifest "
                             "(pyproject+uv.lock / requirements.txt / "
                             "package.json) — refusing to run scripts"}

        # finalize: replace any previous failed tree, move staging into place
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            staging.rename(dest)
        except OSError:
            shutil.copytree(staging, dest)
            shutil.rmtree(staging, ignore_errors=True)

        # per-repo fake HOME + logs (pre-created so tools that assume a
        # standard XDG layout never hit ENOENT)
        jarvis_dir = dest / ".jarvis"
        for subdir in ("home/.local/share/uv", "home/.cache", "home/.config",
                       "logs"):
            (jarvis_dir / subdir).mkdir(parents=True, exist_ok=True)

        code, commit_out = self._run(["git", "rev-parse", "HEAD"], cwd=dest)
        commit = commit_out.splitlines()[0] if code == 0 else "unknown"

        env_ok, env_detail = self._provision(dest, info)
        if not env_ok:
            # ROLLBACK: an environment that never came up is a failed install
            shutil.rmtree(dest, ignore_errors=True)
            return {"ok": False, "status": "FAILED",
                    "error": f"dependency installation failed: {env_detail}",
                    "rolled_back": True}

        self._tick("smoke", 85, "smoke test")
        smoke = self.smoke_test_dir(dest, info)

        manifest = {
            "name": name, "url": repo_url, "commit": commit,
            "branch": info.get("head_branch", ""),
            "languages": info["languages"],
            "dependency_manifests": info["dependency_manifests"],
            "python_requires": info.get("python_requires"),
            "license": info.get("license"),
            "risk_level": risk, "risk_findings": info["risk_findings"][:20],
            "entry_points": info["entry_points"],
            "env_kind": info.get("_env_kind", ""),
            "venv_python": info.get("_venv_python", ""),
            "smoke": smoke,
            "status": "READY" if smoke.get("ok") else "PARTIAL",
            "size_mb": info.get("clone_size_mb"),
            "installed_at": M.now_iso(),
        }
        M.write_json(M.repo_manifest_path(name), manifest)
        M.upsert_repo({
            "name": name, "url": repo_url, "commit": commit,
            "license": manifest["license"], "risk_level": risk,
            "status": manifest["status"], "size_mb": manifest["size_mb"],
        })

        # adapter registration (known projects only — never generic)
        adapter = _known_adapters().get(name)
        skill_status = None
        if adapter:
            try:
                skill_status = adapter.register(dest, manifest)
            except Exception as exc:
                skill_status = {"ok": False,
                                "error": f"{type(exc).__name__}: "
                                         f"{str(exc)[:200]}"}

        self._tick("done", 100, "installed")
        return {"ok": True, "name": name, "status": manifest["status"],
                "commit": commit, "dest": str(dest),
                "risk_level": risk, "license": manifest["license"],
                "smoke": smoke, "skill": skill_status,
                "runtime_s": round(time.time() - started, 1),
                "rollback": f"remove {dest} (or ask Jarvis to remove {name})"}

    def _provision(self, dest: Path, info: dict[str, Any]) -> tuple[bool, str]:
        """Create the isolated environment + install declared dependencies.

        Returns (ok, detail). Only declared package managers are used — never
        README shell commands, never sudo, never JARVIS's own interpreter.
        """
        home_env = {
            "HOME": str(dest / ".jarvis" / "home"),
            "UV_CACHE_DIR": str(M.tools_root() / ".cache" / "uv"),
            "PIP_CACHE_DIR": str(M.tools_root() / ".cache" / "pip"),
            "npm_config_cache": str(M.tools_root() / ".cache" / "npm"),
        }
        uv = find_uv()
        if (dest / "uv.lock").exists() and uv:
            info["_env_kind"] = "uv"
            self._tick("deps", 45, "uv sync (isolated environment)")
            env = dict(home_env)
            # Managed interpreters live in the SHARED uv dir (never inside
            # the per-repo fake HOME) and the version is pinned so the
            # resolution is deterministic and nothing is downloaded when a
            # suitable interpreter already exists.
            env["UV_PYTHON_INSTALL_DIR"] = str(
                Path.home() / ".local" / "share" / "uv" / "python")
            code, out = self._run([uv, "sync", "--frozen", "--python",
                                   "3.12"], cwd=dest, env=env,
                                  timeout=_INSTALL_TIMEOUT_S)
            if code != 0:
                return False, out[-400:]
            py = dest / ".venv" / "bin" / "python"
            info["_venv_python"] = str(py) if py.exists() else ""
            return py.exists(), "uv environment created"
        if (dest / "requirements.txt").exists():
            info["_env_kind"] = "venv-pip"
            if not uv:
                return False, "uv not found and plain venv fallback requires it"
            self._tick("deps", 45, "creating isolated venv")
            code, out = self._run([uv, "venv", ".venv"], cwd=dest,
                                  env=home_env, timeout=120)
            if code != 0:
                return False, f"venv creation failed: {out[-300:]}"
            py = dest / ".venv" / "bin" / "python"
            self._tick("deps", 60, "installing requirements")
            code, out = self._run([uv, "pip", "install", "--python",
                                   str(py), "-r", "requirements.txt"],
                                  cwd=dest, env=home_env,
                                  timeout=_INSTALL_TIMEOUT_S)
            if code != 0:
                return False, out[-400:]
            info["_venv_python"] = str(py)
            return True, "venv + requirements installed"
        if (dest / "package.json").exists():
            info["_env_kind"] = "npm"
            npm = shutil.which("npm")
            if not npm:
                return False, "package.json present but npm is not installed"
            self._tick("deps", 45, "npm install (project-local)")
            code, out = self._run([npm, "install", "--no-audit", "--no-fund"],
                                  cwd=dest, env=home_env,
                                  timeout=_INSTALL_TIMEOUT_S)
            if code != 0:
                return False, out[-400:]
            return True, "node_modules installed"
        return False, "no supported dependency manifest"

    # ── smoke test ───────────────────────────────────────────────────────
    def smoke_test_dir(self, dest: Path,
                       info: dict[str, Any]) -> dict[str, Any]:
        """Best-effort verification that the environment actually works.

        Honest by design: an import smoke test proves the dependency tree
        resolved — it does NOT prove the tool can do its job (that needs
        provider keys / models), and the result says so.
        """
        py = info.get("_venv_python")
        if py and Path(py).exists():
            code, out = self._run(
                [py, "-c", "import sys; print(sys.version.split()[0])"],
                cwd=dest, timeout=60)
            if code != 0:
                return {"ok": False,
                        "detail": f"venv python broken: {out[-200:]}"}
            pyver = out.splitlines()[0] if out else "?"
            project = _project_import_name(dest)
            imports_ok, import_detail = True, ""
            if project:
                code, out = self._run([py, "-c", f"import {project}"],
                                      cwd=dest, timeout=120)
                if code == 0:
                    imports_ok = True
                elif "ModuleNotFoundError" in out and \
                        f"No module named '{project}'" in out:
                    # application-style repo (no importable top-level
                    # package): the interpreter check above is the real test
                    imports_ok = True
                    import_detail = ("application-style repository — no "
                                     "importable package; environment "
                                     "verified via interpreter only")
                else:
                    imports_ok = False
                    import_detail = out[-300:]
            return {"ok": imports_ok, "python": pyver,
                    "import_checked": project or "",
                    "detail": import_detail or
                    "environment verified (no importable package name)"}
        if (dest / "node_modules").exists():
            return {"ok": True, "detail": "node_modules installed"}
        return {"ok": False, "detail": "no environment could be verified"}

    # ── update / remove / status ─────────────────────────────────────────
    def update(self, name: str) -> dict[str, Any]:
        name = safe_dir_name(name)
        dest = M.repo_dir(name)
        manifest = M.load_repo_manifest(name)
        if not manifest or not dest.exists():
            return {"ok": False, "error": f"{name} is not installed"}
        self._tick("fetch", 20, "git fetch")
        code, out = self._run(["git", "fetch", "--depth", "1", "origin"],
                              cwd=dest, timeout=_GIT_TIMEOUT_S)
        if code != 0:
            return {"ok": False, "error": f"git fetch failed: {out[-200:]}"}
        local, _ = self._run(["git", "rev-parse", "HEAD"], cwd=dest)
        remote, _ = self._run(["git", "rev-parse", "origin/HEAD"], cwd=dest)
        local_sha = local.splitlines()[0] if local else ""
        remote_sha = remote.splitlines()[0] if remote else ""
        if local_sha and local_sha == remote_sha:
            return {"ok": True, "updated": False, "commit": local_sha,
                    "message": f"{name} already at the latest commit"}
        self._tick("merge", 40, "fast-forward")
        code, out = self._run(["git", "merge", "--ff-only", "origin/HEAD"],
                              cwd=dest, timeout=120)
        if code != 0:
            return {"ok": False,
                    "error": f"fast-forward failed (pinned commit kept): "
                             f"{out[-200:]}"}
        code, head_out = self._run(["git", "rev-parse", "HEAD"], cwd=dest)
        commit = head_out.splitlines()[0] if code == 0 else "unknown"
        info = self._read_tree(dest)
        env_ok, env_detail = self._provision(dest, info)
        if not env_ok:
            return {"ok": False,
                    "error": f"dependency re-sync failed: {env_detail}"}
        manifest.update(commit=commit, updated_at=M.now_iso(),
                        status="READY" if env_ok else manifest.get("status"))
        M.write_json(M.repo_manifest_path(name), manifest)
        M.upsert_repo({"name": name, "url": manifest["url"],
                       "commit": commit, "status": "READY"})
        return {"ok": True, "updated": True, "commit": commit,
                "message": f"{name} updated to {commit[:10]}"}

    def remove(self, name: str) -> dict[str, Any]:
        name = safe_dir_name(name)
        dest = M.repo_dir(name)
        if not dest.exists():
            M.remove_repo_entry(name)
            return {"ok": True, "message": f"{name} was not installed"}
        self._check_cancel()
        shutil.rmtree(dest, ignore_errors=True)
        M.remove_repo_entry(name)
        return {"ok": True, "message": f"{name} removed from {dest}"}

    def status(self, name: str) -> dict[str, Any]:
        name = safe_dir_name(name)
        manifest = M.load_repo_manifest(name)
        if not manifest:
            return {"ok": False, "error": f"{name} is not installed"}
        skill = M.load_skill_manifest(name)
        reg = M.load_registry()
        skill_entry = (reg["skills"].get(skill.get("skill_id", ""))
                       if skill else None)
        return {"ok": True, "manifest": manifest, "skill": skill,
                "registry": skill_entry, "path": str(M.repo_dir(name))}


def _project_import_name(dest: Path) -> str:
    """Best-effort importable package name from pyproject [project].name."""
    pyproject = dest / "pyproject.toml"
    if not pyproject.exists():
        return ""
    try:
        for line in pyproject.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("name") and "=" in line:
                value = line.split("=", 1)[1].strip().strip("\"' ")
                return value.replace("-", "_")
    except Exception:
        pass
    return ""


def _known_adapters() -> dict[str, Any]:
    """Registry of first-party adapters keyed by repository directory name."""
    out: dict[str, Any] = {}
    try:
        from core.repo_agent import vimax
        out[vimax.REPO_NAME] = vimax
    except Exception:
        pass
    try:
        from core.repo_agent import videorag
        out[videorag.REPO_NAME] = videorag
    except Exception:
        pass
    try:
        from core.repo_agent import claude_code_templates
        out[claude_code_templates.REPO_NAME] = claude_code_templates
    except Exception:
        pass
    return out
