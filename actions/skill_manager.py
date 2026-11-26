"""
skill_manager.py — install / upgrade / remove JARVIS skills from a GitHub URL.

Paste any GitHub repo URL in the chat box (or say "install <url>") and JARVIS
installs it as a skill. Two capability types are detected automatically:

  knowledge — repo carries a SKILL.md: name/description/instructions are
              injected into the live system prompt (core/skill_registry.py),
              so JARVIS can follow them on the next conversation rebuild.
  action    — repo carries *.py action files: they are copied into actions/
              and picked up by core.action_loader on JARVIS' next launch
              (same convention as the hermes_* bridge actions).

House rules: stdlib only, never executes skill code at install time, hard size
cap, path-traversal safe, every install recorded with URL + ref + hash.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from core.skill_registry import (
    MAX_SKILL_BYTES,
    find_skill_md,
    get_skill,
    hash_tree,
    installed_skills_context,
    load_registry,
    read_frontmatter,
    remove_skill,
    safe_name,
    skill_dir,
    upsert_skill,
)

# The app tree is NOT the writable home: BASE_DIR follows JARVIS_HOME (pendrive
# mode) so skills/ lands on the drive, but action payloads must install next to
# the code itself, wherever this file physically is. Deriving _ACTIONS_DIR from
# BASE_DIR made installs vanish when launched via portable.sh/portable.bat.
_ACTIONS_DIR = Path(__file__).resolve().parent
_TIMEOUT = 60  # seconds per HTTP fetch

_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)"
    r"(?:\.git)?(?:/(?:tree|blob)/([^/\s]+)(?:/([^\s]*))?)?/?$"
)


def _parse_url(url: str) -> tuple[str, str, str, str] | None:
    """→ (owner, repo, ref, subpath) or None. '' means default branch / repo root.
    Accepts /tree/<ref>/<sub/dir> and /blob/<ref>/<path> (path = skill root)."""
    m = _URL_RE.match((url or "").strip())
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3) or "", (m.group(4) or "").strip("/")


def _http(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "jarvis-skill-installer/1.0"})
    with urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.read()


def _resolve_ref(owner: str, repo: str, ref: str) -> str:
    if ref:
        return ref
    try:
        data = json.loads(
            _http(f"https://api.github.com/repos/{owner}/{repo}").decode()
        )
        return data.get("default_branch") or "main"
    except Exception:
        return "main"


def _fetch_zip(owner: str, repo: str, ref: str) -> zipfile.ZipFile:
    url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{ref}"
    buf = io.BytesIO(_http(url))
    zf = zipfile.ZipFile(buf)
    total = sum(i.file_size for i in zf.infolist())
    if total > MAX_SKILL_BYTES:
        raise ValueError(f"repo payload too large ({total // (1024 * 1024)} MB > cap)")
    return zf


def _extract_safely(zf: zipfile.ZipFile, dest: Path, sub: str = "") -> None:
    """Extract the repo subtree into dest, zipsafe. With sub (a path inside the
    repo, from a /tree/<ref>/<path> URL), only that subtree is extracted and it
    becomes the skill root — monorepo URLs install just the pointed-at skill."""
    names = zf.namelist()
    if not names:
        raise ValueError("empty archive")
    root_prefix = names[0].split("/")[0] + "/"
    sub_prefix = f"{sub}/" if sub else ""
    found_sub = not sub_prefix
    for info in zf.infolist():
        rel = info.filename
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise ValueError(f"unsafe path in archive: {rel}")
        if not rel.startswith(root_prefix):
            continue
        rel = rel[len(root_prefix) :]
        if sub_prefix:
            if not (rel == sub.rstrip("/") or rel.startswith(sub_prefix)):
                continue
            found_sub = True
            rel = rel[len(sub_prefix) :]
            if not rel:  # the subtree dir itself
                continue
        target = (dest / rel).resolve()
        if not str(target).startswith(str(dest.resolve())):
            raise ValueError(f"unsafe path escapes install dir: {rel}")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    if not found_sub:
        raise ValueError(f"subpath '{sub}' not found in the repo archive")


def _is_action_payload(py: Path) -> bool:
    """True when the .py file declares a module-level TOOL (= a JARVIS action).
    Pure static check — skill code is never executed at install time."""
    try:
        return (
            re.search(
                r"^TOOL\s*=",
                py.read_text(encoding="utf-8", errors="replace")[:200000],
                re.MULTILINE,
            )
            is not None
        )
    except OSError:
        return False


def _classify(root: Path) -> tuple[str, str]:
    """→ (capability, skill_md_relpath_or_''). Action beats knowledge when both."""
    if any(_is_action_payload(p) for p in root.rglob("*.py") if p.is_file()):
        return "action", ""
    md = find_skill_md(root)
    if md is not None:
        return "knowledge", str(md.relative_to(root))
    return "unknown", ""


def _install_action_files(root: Path, name: str) -> list[str]:
    """Copy *.py action payloads into actions/ as <name>_<file>.py."""
    installed: list[str] = []
    for py in sorted(root.rglob("*.py")):
        if py.name.startswith("_") or py.name.startswith("."):
            continue
        rel = py.relative_to(root)
        # flatten: skillname_subpath_File.py keeps names unique and discoverable
        flat = (
            "_".join(rel.parts[:-1]) + "_" + py.stem if len(rel.parts) > 1 else py.stem
        )
        target_name = safe_name(f"{name}_{flat}" if flat else name) or f"{name}_skill"
        target = _ACTIONS_DIR / f"{target_name}.py"
        shutil.copyfile(py, target)
        installed.append(target.name)
    return installed


def _handoff_to_repo_agent(owner: str, repo: str) -> str:
    """Queue an APPLICATION install through core.repo_agent (best effort).

    A repo with no SKILL.md and no action modules is a normal application,
    not a JARVIS skill — dead-ending the user there was the 2026-10-24 bug
    ("install agency-agents" → "nothing JARVIS can install"). The repository
    agent is the correct home for it: it inspects licence/manifests, runs a
    static risk scan and installs in an isolated environment. Returns the
    sentence to speak; any failure degrades to an instruction, never an
    exception in the skill path.
    """
    url = f"https://github.com/{owner}/{repo}"
    try:
        from core.repo_agent.jobs import start_job

        result = start_job(
            "install", repo.lower(), source="skill_manager", params={"url": url}
        )
        if result.get("ok"):
            job_id = result.get("job_id")
            if result.get("deduplicated"):
                return (
                    f"It is already queued as an application install "
                    f"(job {job_id})."
                )
            return (
                f"It looks like an application, not a skill package, so I "
                f"handed it to the repository agent (job {job_id}): it is "
                "being inspected, risk-scanned and built in an isolated "
                "environment under ~/JarvisTools. I will report when it is "
                "ready or if it needs configuration."
            )
    except Exception:
        pass
    return (
        f"It looks like an application, not a skill package. Say "
        f"'install {url}' and I will set it up with the repository agent."
    )


def skill_manager(parameters: dict, player=None) -> str:
    p = parameters or {}
    op = str(p.get("operation", "install")).strip().lower()
    url = str(p.get("url", "")).strip()
    name_arg = str(p.get("name", "")).strip()
    task = str(p.get("task", "")).strip()

    # ── install / upgrade ────────────────────────────────────────────────
    if op in ("install", "upgrade"):
        parsed = _parse_url(url)
        if not parsed:
            return (
                "That does not look like a GitHub repo URL. Paste a link like "
                "https://github.com/owner/repo (optionally /tree/branch)."
            )
        owner, repo, ref_in_url, sub = parsed
        ref = _resolve_ref(owner, repo, ref_in_url)
        try:
            zf = _fetch_zip(owner, repo, ref)
        except Exception as e:
            return (
                f"Could not fetch {owner}/{repo}@{ref}: {e}. If this is an "
                f"application rather than a skill package, say 'install "
                f"https://github.com/{owner}/{repo}' and I will use the "
                "repository agent instead."
            )

        # name: explicit arg > SKILL.md frontmatter > repo[-subdir] name
        name = safe_name(name_arg) if name_arg else None
        staging = skill_dir(f".staging-{repo}")
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        try:
            _extract_safely(zf, staging, sub)
            md = find_skill_md(staging)
            fm = read_frontmatter(md) if md else {}
            if not name:
                name = safe_name(fm.get("name", "")) or safe_name(repo)
            if not name:
                return f"Could not derive a safe skill name from {repo}."
            capability, skill_md_rel = _classify(staging)
            if capability == "unknown":
                # Not a skill package — an ordinary application repository.
                # Hand it to the repository agent instead of dead-ending, and
                # never leave the staging checkout behind.
                shutil.rmtree(staging, ignore_errors=True)
                return (
                    f"{owner}/{repo} carries no SKILL.md and no .py action "
                    "modules, so it is not a JARVIS skill package and "
                    "nothing was added to skills/. "
                    + _handoff_to_repo_agent(owner, repo)
                )

            dest = skill_dir(name)
            if dest.exists():
                shutil.rmtree(dest)
            staging.rename(dest)

            entry = {
                "name": name,
                "source": f"https://github.com/{owner}/{repo}"
                + (f"/tree/{ref}/{sub}" if sub else ""),
                "ref": ref,
                "capability": capability,
                "skill_md": skill_md_rel,
                "installed_at": __import__("datetime")
                .datetime.now(__import__("datetime").timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "hash": hash_tree(dest),
                "enabled": True,
                "description": (fm.get("description") or "")[:200],
            }

            notes = []
            if capability == "action":
                files = _install_action_files(dest, name)
                entry["action_files"] = files
                notes.append(
                    f"{len(files)} action file(s) copied to actions/ — "
                    "active on JARVIS' next launch."
                )

            prev = get_skill(name)
            if prev and prev.get("hash") == hash_tree(dest) and prev.get("ref") == ref:
                return (
                    f"Skill '{name}' is already installed at this version "
                    f"({prev.get('source')}@{ref}). Say 'upgrade' with a new "
                    "version/branch URL if you want the latest changes."
                )
            upgrading = bool(prev and prev.get("hash") != hash_tree(dest))
            upsert_skill(entry)
            verb = "Upgraded" if upgrading else "Installed"
            summary = f"{verb} skill '{name}' from {owner}/{repo}@{ref}."
            if capability == "knowledge":
                summary += (
                    f" Its instructions ({skill_md_rel or 'SKILL.md'}) are now in "
                    "my live context — I can act on them right away."
                )
            if notes:
                summary += " " + " ".join(notes)
            return summary
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # ── remove ───────────────────────────────────────────────────────────
    if op == "remove":
        name = safe_name(name_arg) or (safe_name(url) if url else None)
        if not name or not get_skill(name):
            return f"No installed skill named '{name_arg or url}'. Say 'list skills' to see what's installed."
        dest = skill_dir(name)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        for f in (get_skill(name) or {}).get("action_files", []):
            (_ACTIONS_DIR / f).unlink(missing_ok=True)
        remove_skill(name)
        return f"Removed skill '{name}' (action files deactivate on next launch)."

    # ── enable / disable (knowledge skills: instant via prompt rebuild) ──
    if op in ("enable", "disable"):
        name = safe_name(name_arg) or (safe_name(url) if url else None)
        entry = get_skill(name or "")
        if not entry:
            return f"No installed skill named '{name_arg or url}'."
        entry["enabled"] = op == "enable"
        upsert_skill(entry)
        when = (
            "now (next conversation rebuild)"
            if entry["capability"] == "knowledge"
            else "on next launch"
        )
        return f"Skill '{name}' {op}d — effective {when}."

    # ── list ─────────────────────────────────────────────────────────────
    if op in ("list", ""):
        reg = load_registry()
        skills = reg.get("skills", {})
        if not skills:
            return (
                "No skills installed yet. Paste a GitHub repo URL in the chat "
                "(or ask me to install one) and I'll take it from there."
            )
        lines = []
        for name, e in sorted(skills.items()):
            state = "on" if e.get("enabled", True) else "off"
            cap = e.get("capability", "?")
            lines.append(
                f"- {name} [{cap}, {state}] — {e.get('source')}@{e.get('ref')} "
                f"({e.get('description', '')[:80]})"
            )
        return f"{len(skills)} installed skill(s):\n" + "\n".join(lines)

    if op == "context":
        return installed_skills_context() or "No knowledge skills installed."

    if op == "run":
        if not task:
            return "Give me the task to perform with the installed skills."
        return (
            "Installed skills guide my own behaviour automatically — just state the "
            f"task and I'll apply them. Task received: {task}"
        )

    return f"Unknown operation '{op}'. Use install | upgrade | remove | enable | disable | list."


TOOL = {
    "name": "skill_manager",
    "description": (
        "Install, upgrade, remove, enable/disable, or list JARVIS SKILL "
        "PACKAGES from a GitHub repo. A skill package is ONLY a repo that "
        "carries a SKILL.md (knowledge instructions injected into the prompt) "
        "or top-level *.py action modules. Use this tool only when that is "
        "what the user wants. It never installs applications or tools: for "
        "any other repository — video generators, coding agents, CLIs, model "
        "or data tools — and whenever the user says 'install <github url>' "
        "for a piece of software, call repo_agent instead (it inspects the "
        "repo, risk-scans it and installs it in an isolated environment under "
        "~/JarvisTools). Also use operation='list' when the user asks which "
        "SKILLS are installed; for installed applications/repositories use "
        "repo_agent with action='list'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {
                "type": "STRING",
                "description": "install | upgrade | remove | enable | disable | list",
            },
            "url": {
                "type": "STRING",
                "description": "GitHub repo URL, e.g. https://github.com/owner/repo (or /tree/branch)",
            },
            "name": {
                "type": "STRING",
                "description": "Optional skill name (for remove/enable/disable/upgrade targeting)",
            },
            "task": {
                "type": "STRING",
                "description": "Optional task text the user wants done with the skills",
            },
        },
        "required": ["operation"],
    },
    "handler": skill_manager,
}
