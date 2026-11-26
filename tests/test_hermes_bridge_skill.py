"""Offline checks for the jarvis-hermes-bridge skill + repo wiring.

Run:  .venv/bin/python -m pytest tests/test_hermes_bridge_skill.py -q
(no network, no hermes CLI, no dashboard needed)
"""

import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SKILL = BASE / ".agents" / "skills" / "jarvis-hermes-bridge" / "SKILL.md"
HOOK = BASE / "hermes-hooks" / "jarvis-pdf-forward"


def _skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_skill_md_exists_with_frontmatter():
    assert SKILL.exists(), f"missing {SKILL}"
    text = _skill_text()
    assert text.startswith("---"), "SKILL.md must start with YAML frontmatter"
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "frontmatter block not closed"


def test_skill_frontmatter_fields():
    text = _skill_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    fm = m.group(1)
    assert "name: jarvis-hermes-bridge" in fm
    assert "description:" in fm and "Hermes" in fm
    # description must mention trigger phrases (skill-loader convention)
    assert "Triggers:" in fm or "triggers" in fm.lower()


def test_skill_documents_the_three_bridge_pieces():
    text = _skill_text()
    # outbound actions, the inbox entry point, and the gateway hook
    for ref in (
        "actions/hermes_task.py",
        "scripts/jarvis_inbox.py",
        "hermes-hooks/jarvis-pdf-forward",
    ):
        assert ref in text, f"skill must reference {ref}"
    # install instructions and the debug drill must be present.
    # The install must be rsync --delete into an explicit dir: a plain
    # `cp -r src ~/.hermes/hooks/` overwrites files that still exist in the
    # repo but never removes ones that no longer do, so a deleted module (and
    # __pycache__/) lingers in the hooks dir and can be loaded instead of the
    # fix. Assert the correct command is what a reader would copy.
    assert "rsync -a --delete hermes-hooks/jarvis-pdf-forward/" in text
    assert "mkdir -p ~/.hermes/hooks/jarvis-pdf-forward" in text
    # ...and that the cp -r trap is called out, so nobody "simplifies" back to it
    assert "cp -r hermes-hooks/jarvis-pdf-forward" in text
    # backups must not live where the gateway scans for HOOK.yaml
    assert "hook-backups" in text
    assert "knowledge/audit.log" in text


def test_skill_referenced_files_actually_exist():
    text = _skill_text()
    for m in re.finditer(
        r"`?((?:actions|scripts|hermes-hooks|core)/[\w./-]+?\.py|"
        r"(?:actions|scripts|hermes-hooks|core)/[\w./-]+/)`?",
        text,
    ):
        rel = m.group(1).rstrip("/.")
        if "*" in rel:  # wildcard mentions like actions/hermes_*.py
            continue
        p = BASE / rel
        assert p.exists(), f"SKILL.md references missing path: {rel}"


def test_hook_and_inbox_wiring_intact():
    assert (HOOK / "HOOK.yaml").exists()
    handler = (HOOK / "handler.py").read_text(encoding="utf-8")
    inbox = (BASE / "scripts" / "jarvis_inbox.py").read_text(encoding="utf-8")
    # hook must call the inbox script the skill documents
    assert "jarvis_inbox.py" in handler
    # inbox must post to the dashboard endpoint the skill documents
    assert "/api/message" in inbox
    # hook event contract
    hook_yaml = (HOOK / "HOOK.yaml").read_text(encoding="utf-8")
    assert "agent:end" in hook_yaml


def test_readme_documents_the_bridge():
    # Front-page README plus the upstream notes moved to docs/ (7302b4c).
    readme = "\n".join(
        (BASE / name).read_text(encoding="utf-8")
        for name in ("README.md", "docs/mark-lv-readme.md")
    )
    assert "jarvis-hermes-bridge" in readme
    assert "jarvis_inbox.py" in readme
    assert "hermes-hooks/jarvis-pdf-forward" in readme


def test_requirements_mentions_bridge_is_dependency_free():
    req = (BASE / "requirements.txt").read_text(encoding="utf-8")
    assert "Hermes bridge" in req


def test_hook_handler_imports_cleanly():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "jarvis_pdf_forward_handler", HOOK / "handler.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # stdlib-only module: safe to import
    assert callable(mod.handle)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
