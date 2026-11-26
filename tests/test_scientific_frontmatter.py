"""Offline tests for the SKILL.md frontmatter parser."""

from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.scientific import frontmatter  # noqa: E402
from core.scientific.config import SKILLS_ROOT  # noqa: E402

SAMPLE = """---
name: demo-skill
description: >
  A folded description that
  spans two lines.
license: MIT
allowed-tools: Read Write Bash
metadata:
  version: "1.2"
  last-reviewed: "2026-10-01"
  openclaw:
    primaryEnv: DEMO_KEY
    envVars:
    - name: DEMO_KEY
      required: true
      description: needed for the demo API
    - name: OPTIONAL_KEY
      required: false
tags: [alpha, "beta, gamma"]
---

# Demo
Body text.
"""


def test_split_frontmatter():
    raw, body = frontmatter.split_frontmatter(SAMPLE)
    assert raw.startswith("name: demo-skill")
    assert body.strip().startswith("# Demo")


def test_missing_and_unterminated_frontmatter():
    assert frontmatter.split_frontmatter("# no frontmatter") == ("", "# no frontmatter")
    assert frontmatter.split_frontmatter("---\nname: x\nno end") == ("", "---\nname: x\nno end")


def test_parses_scalars_folded_and_nested_lists():
    meta = frontmatter.parse(SAMPLE)
    assert meta["name"] == "demo-skill"
    assert meta["description"] == "A folded description that spans two lines."
    assert meta["allowed-tools"] == "Read Write Bash"
    assert meta["metadata"]["version"] == "1.2"
    assert meta["tags"] == ["alpha", "beta, gamma"]
    env = meta["metadata"]["openclaw"]["envVars"]
    assert [entry["name"] for entry in env] == ["DEMO_KEY", "OPTIONAL_KEY"]
    assert env[0]["required"] is True
    assert env[1]["required"] is False


def test_booleans_and_nulls():
    meta = frontmatter.parse("---\na: true\nb: false\nc: null\nd: ~\n---\n")
    assert meta == {"a": True, "b": False, "c": None, "d": None}


def test_literal_block_keeps_newlines():
    meta = frontmatter.parse("---\ntext: |\n  line one\n  line two\n---\n")
    assert meta["text"] == "line one\nline two"


def test_quoted_values_and_comments():
    meta = frontmatter.parse('---\na: "x: y"\nb: \'single\'\n# comment\nc: v # trailing\n---\n')
    assert meta["a"] == "x: y"
    assert meta["b"] == "single"
    assert meta["c"] == "v # trailing"


def test_load_never_raises_on_garbage(tmp_path):
    bad = tmp_path / "SKILL.md"
    bad.write_text("---\n::: not yaml :::\n---\nbody", encoding="utf-8")
    meta, body = frontmatter.load(bad)
    assert isinstance(meta, dict)
    assert "body" in body
    meta2, body2 = frontmatter.load(tmp_path / "missing.md")
    assert meta2 == {} and body2 == ""


def test_every_vendored_skill_parses():
    """The real contract: all 177 upstream files must parse losslessly."""
    dirs = sorted(p for p in SKILLS_ROOT.iterdir() if p.is_dir())
    assert len(dirs) >= 170, f"expected the vendored library, found {len(dirs)}"
    for directory in dirs:
        meta, body = frontmatter.load(directory / "SKILL.md")
        assert meta.get("name"), directory.name
        assert meta.get("description"), directory.name
        assert body.strip(), directory.name
        assert (meta.get("metadata") or {}).get("version"), directory.name
