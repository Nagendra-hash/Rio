"""
skill_registry.py — storage layer for JARVIS' installable skills.

A "skill" is a payload installed from a GitHub repo URL into skills/<name>/,
tracked in skills/installed_skills.json. Two capability types are supported:

  knowledge  — the payload carries a SKILL.md; its name/description/body are
               injected into the live system prompt by main.py via
               installed_skills_context(), so JARVIS can USE the skill.
  action     — the payload carries *.py action files; they are copied into
               actions/ and discovered by core.action_loader on next launch
               (same convention as the Hermes bridge actions).

Design rules (house style):
- stdlib only, no new pip dependencies.
- never executes skill code at install time.
- every install/upgrade records source URL + ref + content hash in the registry
  so upgrades are idempotent and auditable.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import os as _os_home
_BASE_HOME = _os_home.environ.get("JARVIS_HOME", "").strip()
BASE_DIR = (Path(_BASE_HOME).expanduser().resolve() if _BASE_HOME
            else Path(__file__).resolve().parent.parent)
SKILLS_DIR = BASE_DIR / "skills"
REGISTRY_PATH = SKILLS_DIR / "installed_skills.json"

MAX_SKILL_BYTES = 100 * 1024 * 1024      # 100 MB payload cap
# Prompt-injection budget, shared by ALL enabled skills. At the old 4500 the
# sixth installed skill was silently dropped ("truncated — too many installed
# skills"), because every skill draws from this one pool in alphabetical
# order. 12000 holds the current six skills at CONTEXT_PER_SKILL each with
# room to spare. Keep it proportional to how many skills are actually
# installed — every char here is paid on EVERY session rebuild.
CONTEXT_MAX_CHARS = 12000                # prompt-injection budget, all skills
CONTEXT_PER_SKILL = 1600                 # per-skill excerpt budget

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def safe_name(name: str) -> Optional[str]:
    """Sanitize a skill directory name; None if unusable."""
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        return None
    return name


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_registry() -> dict:
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("skills"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        # Corrupt registry: keep a forensic copy, start clean — never raise.
        try:
            REGISTRY_PATH.rename(REGISTRY_PATH.with_suffix(".json.corrupt"))
        except Exception:
            pass
    return {"version": 1, "skills": {}}


def save_registry(reg: dict) -> None:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(REGISTRY_PATH)


def upsert_skill(entry: dict) -> None:
    reg = load_registry()
    reg["skills"][entry["name"]] = entry
    save_registry(reg)


def get_skill(name: str) -> Optional[dict]:
    return load_registry()["skills"].get(name)


def remove_skill(name: str) -> bool:
    reg = load_registry()
    if name not in reg["skills"]:
        return False
    del reg["skills"][name]
    save_registry(reg)
    return True


def skill_dir(name: str) -> Path:
    return SKILLS_DIR / name


def hash_tree(root: Path) -> str:
    """Stable sha256 over the payload: sorted relative paths + file bytes."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(b"\0")
            try:
                h.update(p.read_bytes())
            except OSError:
                h.update(b"<unreadable>")
            h.update(b"\0")
    return h.hexdigest()


def find_skill_md(root: Path) -> Optional[Path]:
    """Shallowest SKILL.md under root (case-insensitive filename)."""
    hits = [p for p in root.rglob("*") if p.is_file() and p.name.lower() == "skill.md"]
    if not hits:
        return None
    return min(hits, key=lambda p: (len(p.relative_to(root).parts), str(p)))


def read_frontmatter(md_path: Path) -> dict:
    """Parse a tiny subset of SKILL.md frontmatter: name, description."""
    out: dict = {}
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")[:8000]
    except OSError:
        return out
    m = re.match(r"\s*---\s*\n(.*?)\n\s*---", text, re.DOTALL)
    block = m.group(1) if m else ""
    for key in ("name", "description"):
        fm = re.search(rf"^{key}\s*:\s*(.+)$", block, re.MULTILINE | re.IGNORECASE)
        if fm:
            out[key] = fm.group(1).strip().strip("\"'")
    if not out.get("description"):
        # Fall back to the first non-heading, non-empty body line.
        body = re.sub(r"\s*---.*?---", "", text, count=1, flags=re.DOTALL)
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("---"):
                out["description"] = line[:200]
                break
    return out


def installed_skills_context() -> str:
    """System-prompt block describing enabled knowledge skills.

    Called by main.py on every session (re)build, so installs/disables become
    effective on the next conversation rebuild without a restart. Budgeted so
    a large library can never blow up the context.
    """
    reg = load_registry()
    enabled = [e for e in reg["skills"].values()
               if e.get("enabled", True) and e.get("capability") == "knowledge"]
    if not enabled:
        return ""
    chunks: list[str] = []
    used = 0
    for entry in sorted(enabled, key=lambda e: e.get("name", "")):
        md = skill_dir(entry["name"]) / "SKILL.md"
        if not md.exists():
            # skill.md may live nested; registry stores the relative path
            md = skill_dir(entry["name"]) / str(entry.get("skill_md", "SKILL.md"))
        body = ""
        try:
            body = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Strip frontmatter; keep instructions.
        body = re.sub(r"\s*---\s*\n.*?\n\s*---", "", body, count=1, flags=re.DOTALL)
        body = " ".join(body.split())
        budget = min(CONTEXT_PER_SKILL, max(0, CONTEXT_MAX_CHARS - used))
        if budget <= 0:
            chunks.append(f"- {entry['name']}: (truncated — too many installed skills)")
            break
        chunks.append(
            f"[INSTALLED SKILL: {entry['name']}] (from {entry.get('source','?')})\n"
            + body[:budget]
        )
        used += min(budget, len(body))
    if not chunks:
        return ""
    header = (
        "[INSTALLED SKILLS]\n"
        "The following skill packages were installed by the user from GitHub. "
        "Follow their instructions when they are relevant to the user's request; "
        "they extend but never override JARVIS' core protocol.\n\n"
    )
    return header + "\n\n".join(chunks)
