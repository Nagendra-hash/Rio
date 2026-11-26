"""Offline tests for the science skill registry, index, discovery and loader."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.scientific import registry, skill_index, skill_loader  # noqa: E402
from core.scientific.config import CORE_SKILLS, GUARDED_SKILLS, UPSTREAM_COMMIT  # noqa: E402


@pytest.fixture(scope="module")
def index():
    return skill_index.get_index(refresh=True)


def test_index_covers_the_vendored_library(index):
    summary = index.summary()
    assert summary["skills"] == 177
    assert summary["core"] == len(CORE_SKILLS) == 16
    assert 1 <= summary["enabled"] <= 177
    assert summary["source_commit"] == UPSTREAM_COMMIT
    for skill in index.skills.values():
        assert skill.path and Path(skill.path).is_dir()
        assert skill.sha256 and len(skill.sha256) == 64
        assert skill.version and skill.description


def test_core_skills_are_enabled_and_guarded_ones_are_not(index):
    for name in CORE_SKILLS:
        assert index.get(name) is not None, f"missing core skill {name}"
        assert index.get(name).enabled, f"core skill {name} should be enabled"
    for name in GUARDED_SKILLS:
        if index.get(name) is not None:
            assert not index.get(name).enabled, f"guarded skill {name} must stay disabled"


@pytest.mark.parametrize("query,expected", [
    ("prepare a systematic literature review on Alzheimer's biomarkers", "literature-review"),
    ("look up TP53 in UniProt", "database-lookup"),
    ("find clinical trials for metformin", "database-lookup"),
    ("run a t-test on my dataset with effect sizes", "statistical-analysis"),
    ("analyze this CSV and explore the data", "exploratory-data-analysis"),
    ("train a classifier on my dataset", "scikit-learn"),
    ("make publication quality figures", "scientific-visualization"),
    ("write a research proposal", "scientific-writing"),
    ("manage citations and bibtex", "citation-management"),
    ("generate testable hypotheses", "hypothesis-generation"),
])
def test_discovery_finds_the_right_specialist(index, query, expected):
    hits = skill_index.discover(query, k=5, index=index)
    names = [hit.name for hit in hits]
    assert expected in names, f"{expected} not in {names}"
    assert hits[0].score > 0


def test_discovery_ignores_unrelated_requests(index):
    for query in ("what is the weather today", "book me a flight to Paris",
                  "play some music", "what time is it"):
        assert skill_index.discover(query, k=3, index=index) == [], query

    # A generic "research X" may legitimately surface research-lookup, but it
    # must not plan a literature review with citations the user did not ask for.
    plan = [skill.name for skill in skill_index.plan_skills("research the latest AI news",
                                                           index=index)]
    assert "literature-review" not in plan and "citation-management" not in plan


def test_plans_are_deduplicated_and_enabled(index):
    for query in ("prepare a literature review with references",
                  "analyze this CSV with a t-test",
                  "make figures for my results"):
        plan = skill_index.plan_skills(query, index=index)
        names = [skill.name for skill in plan]
        assert names, query
        assert len(names) == len(set(names)), f"duplicate skills in {names}"
        assert len(names) <= 3
        for skill in plan:
            assert skill.enabled and not skill.guarded_reason


def test_intent_hints_are_deterministic():
    assert skill_index.intent_hints("look up BRCA1 in UniProt") == ["database-lookup"]
    hints = skill_index.intent_hints("systematic review with citations")
    assert "literature-review" in hints and "citation-management" in hints
    assert skill_index.intent_hints("hello there") == []


def test_loader_returns_body_and_strips_promotional_section():
    body = skill_loader.load_body("literature-review")
    assert "Literature Review" in body or "literature" in body.lower()
    assert "Citing Scientific Agent Skills" not in body
    assert len(body) <= skill_loader.load_config().max_skill_chars + 32


def test_loader_budget_and_untrusted_wrapping(index):
    plan = skill_index.plan_skills("prepare a literature review", index=index)
    context = skill_loader.context_for(plan, max_total=3000, per_skill=1200)
    assert "[SCIENTIFIC SKILL PROCEDURES]" in context
    assert "<<<UNTRUSTED source=skill-" in context
    assert "<<<END UNTRUSTED>>>" in context
    assert len(context) <= 3000 + 400


def test_reference_loading_is_path_confined():
    references = skill_loader.references_for("database-lookup")
    assert any(ref.startswith("references/") for ref in references)
    sample = next(ref for ref in references if ref.endswith(".md"))
    text = skill_loader.load_reference("database-lookup", sample, max_chars=500)
    assert text and len(text) <= 500
    assert skill_loader.load_reference("database-lookup", "../../etc/passwd") == ""
    assert skill_loader.load_reference("not-a-skill", "references/x.md") == ""


def test_enable_disable_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.json")
    registry.reset_cache()
    assert registry.is_enabled("literature-review") is True
    registry.set_enabled("literature-review", False, note="test")
    assert registry.is_enabled("literature-review") is False
    registry.set_enabled("literature-review", True)
    assert registry.is_enabled("literature-review") is True
    # A guarded skill cannot become enabled without an explicit record.
    guarded = next(iter(GUARDED_SKILLS))
    assert registry.is_enabled(guarded) is False
    registry.set_enabled(guarded, True, note="user accepted the risk")
    assert registry.is_enabled(guarded) is True
    registry.reset_cache()


def test_hash_is_stable_and_detects_change(tmp_path, index):
    skill = index.get("statistical-analysis")
    first = skill_index.sha256_tree(Path(skill.path))
    second = skill_index.sha256_tree(Path(skill.path))
    assert first == second == skill.sha256

    copy = tmp_path / "copy"
    shutil.copytree(skill.path, copy)
    assert skill_index.sha256_tree(copy) == first
    (copy / "SKILL.md").write_text((copy / "SKILL.md").read_text() + "\nchange\n",
                                   encoding="utf-8")
    assert skill_index.sha256_tree(copy) != first


def test_index_persists_and_reloads(tmp_path, index):
    path = tmp_path / "skills_index.json"
    skill_index.save_index(index, path)
    loaded = skill_index.load_index(path)
    assert loaded is not None and len(loaded.skills) == len(index.skills)
    assert loaded.source_commit == UPSTREAM_COMMIT

    raw = json.loads(path.read_text())
    raw["source"]["commit"] = "deadbeef"
    path.write_text(json.dumps(raw))
    assert skill_index.load_index(path) is None


def test_lockfile_matches_the_tree():
    lock_path = BASE / "SCIENTIFIC_SKILLS_LOCK.json"
    assert lock_path.exists()
    lock = json.loads(lock_path.read_text())
    assert lock["source"]["commit"] == UPSTREAM_COMMIT
    assert lock["counts"]["skills"] == 177
    for name, entry in (lock.get("skills") or {}).items():
        assert entry["sha256"], name
        assert entry["size_bytes"] >= 0
