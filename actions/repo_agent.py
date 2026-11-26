"""
actions/repo_agent.py — Jarvis tool for the Universal Repository Manager.

Thin bridge only: it validates the request, starts a DETACHED job
(scripts/repo_agent_run.py via core/repo_agent/jobs) and returns text. No
repository code ever runs inside this call, and the model can never pass a
shell command — only a GitHub URL, a repository name, a skill id or a job id.

Security model (REPOSITORY_SECURITY_AND_INSTALLATION_POLICY.md):
    * only canonical github.com URLs are accepted
    * install/update/remove run in an isolated worker; remove requires
      confirm=true
    * skills expose only manifest-allowlisted operations
"""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(BASE))

from core.repo_agent import manifests as M          # noqa: E402
from core.repo_agent import skills                  # noqa: E402
from core.repo_agent.jobs import RepoJobStore, start_job  # noqa: E402
from core.repo_agent.manager import RepositoryManager     # noqa: E402
from core.repo_agent.security import (RepoSecurityError,  # noqa: E402
                                      validate_repo_url)


def _reg_lines() -> list[str]:
    reg = M.load_registry()
    lines = []
    for name, repo in sorted(reg["repos"].items()):
        skills_txt = ", ".join(s for s, v in reg["skills"].items()
                               if v.get("repo") == name) or "no skill adapter"
        lines.append(f"• {name} [{repo.get('status', '?')}] "
                     f"{str(repo.get('commit') or '')[:10]} · "
                     f"licence {repo.get('license') or '?'} · "
                     f"risk {repo.get('risk_level', '?')} · skills: {skills_txt}")
    return lines or ["(no repositories installed)"]


def _skill_lines() -> list[str]:
    reg = M.load_registry()
    lines = []
    for skill_id, entry in sorted(reg["skills"].items()):
        lines.append(f"• {skill_id} [{entry.get('status', '?')}]"
                     f"{' (disabled)' if not entry.get('enabled', True) else ''}"
                     f" — {entry.get('name') or ''}"
                     + (f" · {entry.get('detail')}" if entry.get("detail")
                        else ""))
    return lines or ["(no skills registered)"]


def repo_agent(parameters: dict, player=None, speak=None) -> str:
    """Repository install / inspect / lifecycle actions. Never blocks."""
    parameters = parameters or {}
    action = str(parameters.get("action") or "list").strip().lower()
    url = str(parameters.get("url") or "").strip()
    name = str(parameters.get("name") or "").strip().lower()
    skill_id = str(parameters.get("skill_id") or "").strip().lower()
    job_id = str(parameters.get("job_id") or "").strip()
    confirm = parameters.get("confirm") in (True, 1, "1", "true", "yes")

    if player:
        player.write_log(f"[repo-agent] {action} {url or name or skill_id or job_id}")

    try:
        if action == "install":
            if not url:
                return "Which GitHub repository should I install? Give me a " \
                       "github.com/owner/repo URL and I will inspect it " \
                       "first, then install it in an isolated environment."
            owner, repo = validate_repo_url(url)
            existing = M.load_repo_manifest(repo.lower())
            if existing:
                return (f"{repo} is already installed "
                        f"[{existing.get('status', '?')}] at "
                        f"{M.repo_dir(repo.lower())} "
                        f"(commit {str(existing.get('commit'))[:10]}). Use "
                        "action=update to refresh it or action=status for "
                        "details.")
            result = start_job("install", repo.lower(), source="jarvis",
                               params={"url": f"https://github.com/{owner}/{repo}"})
            if not result.get("ok"):
                return f"Could not start the install: {result.get('error')}"
            if speak:
                speak("Starting the install, sir. I will inspect, clone and "
                      "provision it in the background and report back.")
            return (f"Install queued for github.com/{owner}/{repo} "
                    f"(job {result['job_id']}). I will validate the URL, "
                    "clone it into ~/JarvisTools, run a static risk scan, "
                    "build an isolated environment and verify a smoke test "
                    "before I call it ready."
                    + (" (an identical job is already active — nothing "
                       "duplicated)" if result.get("deduplicated") else ""))

        if action == "inspect":
            if not url:
                return "Give me the github.com URL to inspect."
            manager = RepositoryManager()
            report = manager.inspect(url)
            if not report.get("reachable"):
                return (f"Could not reach {report.get('url')}: "
                        f"{report.get('error', 'unreachable')}")
            findings = report.get("risk_findings") or []
            lines = [f"Inspection of {report['url']} "
                     f"(read-only, nothing executed):",
                     f"• destination: {report['dest']}",
                     f"• licence: {report.get('license') or 'none found'}",
                     f"• manifests: {', '.join(report['dependency_manifests']) or 'none'}",
                     f"• python: {report.get('python_requires') or 'n/a'}",
                     f"• install plan: {'; '.join(report['install_plan'])}",
                     f"• risk: {report.get('risk_level', 'low')} "
                     f"({len(findings)} finding(s))"]
            for f in findings[:5]:
                lines.append(f"  - [{f['severity']}] {f['finding']} "
                             f"({f.get('file', '')})")
            if report.get("already_installed"):
                lines.append("• already installed — install would be skipped")
            return "\n".join(lines)

        if action == "list":
            reg = M.load_registry()
            return ("Installed repositories:\n" + "\n".join(_reg_lines()) +
                    "\nRegistered skills:\n" + "\n".join(_skill_lines()))

        if action == "status":
            if skill_id:
                view = skills.resolve_skill(skill_id)
                if not view.get("ok"):
                    return f"Unknown skill '{skill_id}'."
                entry = view["entry"]
                return (f"Skill {skill_id}: status {entry.get('status')}, "
                        f"{'enabled' if entry.get('enabled', True) else 'disabled'}, "
                        f"repo {entry.get('repo')}."
                        + (f" Detail: {entry.get('detail')}"
                           if entry.get("detail") else ""))
            if not name:
                return "Which repository or skill should I check? (name= or skill_id=)"
            manager = RepositoryManager()
            status = manager.status(name)
            if not status.get("ok"):
                return f"{status.get('error')}"
            manifest = status["manifest"]
            smoke = manifest.get("smoke") or {}
            lines = [f"{name} [{manifest.get('status')}] at {status['path']}",
                     f"• source {manifest.get('url')} @ "
                     f"{str(manifest.get('commit'))[:10]}",
                     f"• licence {manifest.get('license') or '?'}, "
                     f"risk {manifest.get('risk_level', 'low')}",
                     f"• env: {manifest.get('env_kind') or '?'} · "
                     f"smoke: {'ok' if smoke.get('ok') else smoke.get('detail', 'failed')}"]
            if status.get("registry"):
                lines.append(f"• skill: {status['registry'].get('status')} "
                             f"({'enabled' if status['registry'].get('enabled', True) else 'disabled'})")
            return "\n".join(lines)

        if action == "update":
            if not name:
                return "Which repository should I update? (name=)"
            result = start_job("update", name, source="jarvis", params={})
            if not result.get("ok"):
                return f"Could not start the update: {result.get('error')}"
            return (f"Update queued for {name} (job {result['job_id']}) — "
                    "fast-forward + dependency re-sync, pinned commit "
                    "recorded. I will report when it finishes.")

        if action == "remove":
            if not name:
                return "Which repository should I remove? (name=)"
            if not confirm:
                return (f"Removing {name} deletes {M.repo_dir(name)} "
                        "including its environment and any local runs. "
                        "Confirm with confirm=true and I will remove it.")
            result = start_job("remove", name, source="jarvis", params={})
            if not result.get("ok"):
                return f"Could not start the removal: {result.get('error')}"
            return (f"Removal of {name} queued (job {result['job_id']}). "
                    "Registry entry and skill adapter will be cleaned up too.")

        if action in ("enable", "disable"):
            if not skill_id:
                return "Which skill? (skill_id=)"
            entry = M.set_skill_state(skill_id,
                                      enabled=(action == "enable"))
            if entry is None:
                return f"Unknown skill '{skill_id}'."
            state = "enabled" if entry["enabled"] else "disabled"
            return f"Skill {skill_id} is now {state}."

        if action == "jobs":
            store = RepoJobStore()
            store.recover_stale()
            store.dispatch_queued()
            rows = store.list(limit=int(parameters.get("limit") or 8))
            if not rows:
                return "No repository-agent jobs yet."
            lines = [f"• {r['job_id']} {r['kind']}:{r['ref']} [{r['status']}] "
                     f"{int(float(r.get('progress') or 0) * 100)}% "
                     f"{str(r.get('message') or '')[:80]}"
                     for r in rows]
            return "Recent jobs:\n" + "\n".join(lines)

        if action in ("job", "status_job"):
            store = RepoJobStore()
            job = store.get(job_id) if job_id else None
            if not job:
                return f"Unknown job '{job_id or '(none given)'}'."
            detail = {"job_id": job["job_id"], "kind": job["kind"],
                      "ref": job["ref"], "status": job["status"],
                      "progress": f"{int(float(job.get('progress') or 0) * 100)}%",
                      "message": job.get("message"), "error": job.get("error")}
            outputs = _loads(job.get("outputs"))
            if outputs:
                detail["outputs"] = outputs[:5]
            return json.dumps(detail, ensure_ascii=False)

        if action == "cancel":
            if not job_id:
                return "Which job id should I cancel?"
            result = RepoJobStore().cancel(job_id)
            return (f"Cancelled {job_id}."
                    if result.get("ok") else
                    f"Could not cancel: {result.get('error')}")

        return (f"Unknown repo-agent action '{action}'. Use install, inspect, "
                "list, status, update, remove, enable, disable, jobs, job or "
                "cancel.")
    except RepoSecurityError as exc:
        return f"Refused: {exc}. Nothing was executed."
    except Exception as exc:                              # noqa: BLE001
        return (f"The repository agent is unavailable: "
                f"{type(exc).__name__}: {str(exc)[:180]}. Nothing was run.")


def _loads(raw) -> list:
    try:
        value = json.loads(raw) if isinstance(raw, str) else (raw or [])
        return value if isinstance(value, list) else []
    except Exception:
        return []


TOOL = {
    "name": "repo_agent",
    "description": (
        "Install, inspect and manage open-source GitHub repositories as "
        "isolated applications/skills under ~/JarvisTools. THIS is the tool "
        "for any github.com URL the user pastes to install a piece of "
        "software — applications, tools, CLIs, AI agents, video generators, "
        "model or data projects (e.g. 'install "
        "https://github.com/owner/repo'). Only repos that are pure JARVIS "
        "skill packages (SKILL.md knowledge or *.py action modules) belong "
        "to skill_manager; when in doubt use this tool. Also use for: 'is "
        "vimax installed', 'list installed repositories', 'update vimax', "
        "'remove vimax' (asks to confirm), 'show repo agent jobs', 'cancel "
        "job rt-…'. Every install is inspected first (licence, "
        "dependencies, static risk scan), then built in an isolated "
        "environment — never with sudo, never by running README scripts. "
        "Installs and updates run detached; I report when they finish. For "
        "creating videos with the installed ViMax skill use video_creator "
        "instead."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("install | inspect | list | status | update | "
                                "remove | enable | disable | jobs | job | "
                                "cancel"),
            },
            "url": {
                "type": "STRING",
                "description": "Canonical GitHub URL (github.com/owner/repo).",
            },
            "name": {
                "type": "STRING",
                "description": "Repository name, e.g. vimax.",
            },
            "skill_id": {
                "type": "STRING",
                "description": "Skill id, e.g. vimax-video.",
            },
            "job_id": {
                "type": "STRING",
                "description": "rt-… job id for job/cancel.",
            },
            "confirm": {
                "type": "BOOLEAN",
                "description": "Must be true for destructive actions (remove).",
            },
        },
        "required": ["action"],
    },
    "handler": repo_agent,
}
