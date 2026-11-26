"""
core/repo_agent/claude_code_templates.py — adapter for davila7/claude-code-templates.

A collection of Claude Code starter templates (agents, commands, hooks, rules)
for different project types: Rust CLI, Node CLI, Dashboard, Database, etc.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.repo_agent import manifests as M

__all__ = ["REPO_NAME", "SKILL_ID", "register", "skill_definition", "list_templates", "get_template"]

REPO_NAME = "claude-code-templates"
SKILL_ID = "claude-code-templates"


_WRAPPER_SOURCE = '''#!/usr/bin/env python3
"""Wrapper script for claude-code-templates skill operations.

Sets up Python path to include Mark-III root so core modules are importable.
"""
import json
import os
import sys
from pathlib import Path

# Find Mark-III root - try multiple strategies
MARK_III_ROOT = None

# Strategy 1: JARVIS_HOME env var (if set to Mark-III root)
jarvis_home = os.environ.get("JARVIS_HOME", "").strip()
if jarvis_home:
    candidate = Path(jarvis_home).expanduser().resolve()
    if (candidate / "core").exists():
        MARK_III_ROOT = candidate

# Strategy 2: Look for core module relative to this script
if MARK_III_ROOT is None:
    repo_dir = Path(__file__).resolve().parents[1]
    # Check sibling directory "Mark-III"
    candidate = repo_dir.parent / "Mark-III"
    if (candidate / "core").exists():
        MARK_III_ROOT = candidate

# Strategy 3: Default to /home/nagendra/Mark-III
if MARK_III_ROOT is None:
    candidate = Path("/home/nagendra/Mark-III")
    if (candidate / "core").exists():
        MARK_III_ROOT = candidate

if MARK_III_ROOT is None:
    print("ERROR: Could not locate Mark-III root (no core/ directory found)", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(MARK_III_ROOT))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: claude_templates_run.py <op> [args...]", file=sys.stderr)
        sys.exit(1)

    op = sys.argv[1]

    if op == "list_templates":
        from core.repo_agent.claude_code_templates import list_templates
        print(json.dumps(list_templates()))
    elif op == "get_template":
        if len(sys.argv) < 3:
            print("Usage: claude_templates_run.py get_template <template_id>", file=sys.stderr)
            sys.exit(1)
        template_id = sys.argv[2]
        from core.repo_agent.claude_code_templates import get_template
        print(json.dumps(get_template(template_id)))
    else:
        print(f"Unknown operation: {op}", file=sys.stderr)
        sys.exit(1)
'''


def register(dest: Path, repo_manifest: dict[str, Any]) -> dict[str, Any]:
    """Write the skill manifest + wrapper script into <repo>/.jarvis/."""
    commit = str(repo_manifest.get("commit") or "unknown")

    # Create wrapper script that sets up Python path correctly
    wrapper = dest / ".jarvis" / "claude_templates_run.py"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(_WRAPPER_SOURCE, encoding="utf-8")
    os.chmod(wrapper, 0o755)

    skill = skill_definition(commit)
    M.write_json(M.skill_manifest_path(REPO_NAME), skill)
    entry = M.upsert_skill({
        "skill_id": SKILL_ID, "repo": REPO_NAME, "name": skill["name"],
        "commit": commit, "enabled": True, "status": "READY",
    })
    return {"ok": True, "skill_id": SKILL_ID, "status": entry.get("status")}


def skill_definition(commit: str) -> dict[str, Any]:
    """The adapter manifest: allowlisted operations for template management."""
    return {
        "skill_id": SKILL_ID,
        "name": "Claude Code Templates",
        "repo": REPO_NAME,
        "version": commit,
        "description": ("Collection of Claude Code starter templates with agents, "
                        "commands, hooks, and rules for various project types: "
                        "Rust CLI, Node.js CLI, Dashboard, Database, Cloudflare Workers, etc."),
        "task_types": ["template-management"],
        "requires_config": [],
        "env": {},
        "prepare": {"module": "core.repo_agent.claude_code_templates", "function": "prepare"},
        "entry_points": {
            "list_templates": {
                "cmd": ["python3", "{repo}/.jarvis/claude_templates_run.py", "list_templates"],
                "timeout_s": 30
            },
            "get_template": {
                "cmd": ["python3", "{repo}/.jarvis/claude_templates_run.py", "get_template", "{template}"],
                "timeout_s": 30
            },
        },
        "input_schema": {
            "list_templates": {},
            "get_template": {
                "template": {"type": "string", "required": True, "max_chars": 100}
            },
        },
        "outputs": {
            "list_templates": [],
            "get_template": [],
        },
        "limits": {"run_timeout_s": 60, "max_concurrent_runs": 5},
        "cleanup": "no persistent artifacts",
    }


def prepare(*, skill_id: str, op: str, params: dict[str, Any],
            run_dir: Path) -> dict[str, Any]:
    """No special preparation needed."""
    return {}


def list_templates() -> list[dict[str, Any]]:
    """List all available templates in the repo."""
    try:
        base = M.repo_dir(REPO_NAME)
        if not base.exists():
            return [{"error": "repo not installed"}]

        templates = []

        # Root level templates (the .claude directory with agents/commands/hooks)
        root_claude = base / ".claude"
        if root_claude.exists():
            agents = list((root_claude / "agents").glob("*.md")) if (root_claude / "agents").exists() else []
            commands = list((root_claude / "commands").glob("*.md")) if (root_claude / "commands").exists() else []
            hooks = list((root_claude / "hooks").glob("*.py")) if (root_claude / "hooks").exists() else []
            rules = list((root_claude / "rules").glob("*.md")) if (root_claude / "rules").exists() else []

            templates.append({
                "id": "root",
                "name": "Root Template (Global Claude Code Config)",
                "path": ".claude",
                "description": "Global agents, commands, hooks, and rules for any project",
                "agents": [a.stem for a in agents],
                "commands": [c.stem for c in commands],
                "hooks": [h.stem for h in hooks],
                "rules": [r.stem for r in rules],
            })

        # Subdirectory templates
        for subdir in ["cli-rust", "cli-tool", "dashboard", "database", "cloudflare-workers"]:
            sub_path = base / subdir
            if not sub_path.exists():
                continue

            sub_claude = sub_path / ".claude"
            if not sub_claude.exists():
                continue

            agents = list((sub_claude / "agents").glob("*.md")) if (sub_claude / "agents").exists() else []
            commands = list((sub_claude / "commands").glob("*.md")) if (sub_claude / "commands").exists() else []
            hooks = list((sub_claude / "hooks").glob("*.py")) if (sub_claude / "hooks").exists() else []
            rules = list((sub_claude / "rules").glob("*.md")) if (sub_claude / "rules").exists() else []

            # Check for project-specific files
            has_package_json = (sub_path / "package.json").exists()
            has_cargo_toml = (sub_path / "Cargo.toml").exists()
            has_pyproject = (sub_path / "pyproject.toml").exists()

            templates.append({
                "id": subdir,
                "name": f"{subdir.replace('-', ' ').title()} Template",
                "path": subdir,
                "description": f"Starter template for {subdir.replace('-', ' ')} projects",
                "agents": [a.stem for a in agents],
                "commands": [c.stem for c in commands],
                "hooks": [h.stem for h in hooks],
                "rules": [r.stem for r in rules],
                "project_type": "node" if has_package_json else ("rust" if has_cargo_toml else ("python" if has_pyproject else "unknown")),
            })

        return templates
    except Exception as e:
        return [{"error": str(e)}]


def get_template(template_id: str) -> dict[str, Any]:
    """Get detailed info about a specific template."""
    templates = list_templates()
    for t in templates:
        if t.get("id") == template_id:
            # Read the actual agent/command files for more detail
            base = M.repo_dir(REPO_NAME)
            template_path = base / t.get("path", "")

            # For root template, the .claude is directly at template_path
            # For sub-templates, it's at template_path / ".claude"
            if template_id == "root":
                claude_dir = template_path
            else:
                claude_dir = template_path / ".claude"

            detail = dict(t)

            # Read agents
            if claude_dir.exists():
                agents_dir = claude_dir / "agents"
                if agents_dir.exists():
                    detail["agents_detail"] = {}
                    for agent_file in agents_dir.glob("*.md"):
                        try:
                            content = agent_file.read_text(encoding="utf-8")
                            detail["agents_detail"][agent_file.stem] = content[:500] + "..." if len(content) > 500 else content
                        except Exception:
                            pass

                # Read commands
                commands_dir = claude_dir / "commands"
                if commands_dir.exists():
                    detail["commands_detail"] = {}
                    for cmd_file in commands_dir.glob("*.md"):
                        try:
                            content = cmd_file.read_text(encoding="utf-8")
                            detail["commands_detail"][cmd_file.stem] = content[:500] + "..." if len(content) > 500 else content
                        except Exception:
                            pass

                # Read hooks
                hooks_dir = claude_dir / "hooks"
                if hooks_dir.exists():
                    detail["hooks_detail"] = {}
                    for hook_file in hooks_dir.glob("*.py"):
                        try:
                            content = hook_file.read_text(encoding="utf-8")
                            detail["hooks_detail"][hook_file.stem] = content[:500] + "..." if len(content) > 500 else content
                        except Exception:
                            pass

                # Read rules
                rules_dir = claude_dir / "rules"
                if rules_dir.exists():
                    detail["rules_detail"] = {}
                    for rule_file in rules_dir.glob("*.md"):
                        try:
                            content = rule_file.read_text(encoding="utf-8")
                            detail["rules_detail"][rule_file.stem] = content[:500] + "..." if len(content) > 500 else content
                        except Exception:
                            pass

            return detail

    return {"error": f"template '{template_id}' not found"}