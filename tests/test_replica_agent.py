"""
tests/test_replica_agent.py — offline tests for the Phase 20 replica pack.

No network, no model, no gradle: the Play listing is supplied as a manual
dict, the model is a NullLLM, and the vendored tools run for real on a temp
workspace. This locks the pack's own logic:

  POLICY   the clean-room guardrails refuse APK mirrors and decompile asks,
           and allow Play listings (and the refund-free 'read behaviour' path)
  SCHEMA   the feature matrix round-trips; priorities/normalisation hold
  STORE    a Play listing page parses; a manual listing loads
  TOOLS    parity / contrast / listing-lint / sweep / reviews wrap cleanly
  AGENTS   the full pack runs offline and produces every artifact; the critic
           never invents reviews; the builder plans without gradle
  READY    the verify gates say SHIPPABLE only when every gate passes
  AGENT    ReplicaAgent.handle: clone → COMPLETED, decompile → DENIED,
           list/status render; the classifier routes Play/clone asks to
           REPLICA and leaves plain 'build an android app' in CODING
  WIRING   JARVIS_REPLICA_AGENT=on registers the real agent, unset → stub;
           the action TOOL is discoverable

Run:  .venv/bin/python -m pytest tests/test_replica_agent.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pytest  # noqa: E402

from orchestrator.agents import build_default_registry  # noqa: E402
from orchestrator.agents.replica import (
    agents,
    policy,
    schema,
    store,  # noqa: E402
    toolkit,
)
from orchestrator.agents.replica.pipeline import ReplicaPipeline  # noqa: E402
from orchestrator.agents.replica.project import ReplicaProject  # noqa: E402
from orchestrator.agents.replica.replica_agent import ReplicaAgent  # noqa: E402
from orchestrator.base import StubAgent  # noqa: E402
from orchestrator.classifier import IntentClassifier  # noqa: E402
from orchestrator.contracts import (
    AgentRequest,
    TaskStatus,  # noqa: E402
    TaskType,
)


class NullLLM:
    """No model: every stage must fall back to its deterministic path."""

    def text(self, prompt, system=""):  # noqa: D401
        return None


LISTING = {
    "ok": True,
    "url": "https://play.google.com/store/apps/details?id=com.example.notes",
    "name": "NotesPro",
    "package": "com.example.notes",
    "category": "Productivity",
    "rating": 4.2,
    "installs": "1M+",
    "description": (
        "NotesPro keeps notes in sync.\n"
        "\u2022 Create notes instantly\n"
        "\u2022 Sync across devices\n\u2022 Dark mode\n"
        "\u2022 Export to PDF\n\u2022 Reminders for notes"
    ),
    "screenshots": [],
}

REVIEWS_CSV = (
    "source,url,date,rating,text\n"
    "google-play,https://play.google.com/store/apps/details?id=com.example."
    "notes#r1,2026-01-02,1,The sync is broken and I lost my notes\n"
    "google-play,https://play.google.com/store/apps/details?id=com.example."
    "notes#r2,2026-01-03,2,Too expensive for what it does\n"
    "google-play,https://play.google.com/store/apps/details?id=com.example."
    "notes#r3,2026-02-01,2,Please add offline support\n"
    "google-play,https://play.google.com/store/apps/details?id=com.example."
    "notes#r4,2026-02-02,1,App crashes when I export\n"
)


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "MyRebuild"
    ws.mkdir()
    (ws / "reviews.csv").write_text(REVIEWS_CSV, encoding="utf-8")
    return ws


# ── POLICY ───────────────────────────────────────────────────────────────────
def test_policy_allows_play_listing():
    assert policy.assert_source_allowed(
        "https://play.google.com/store/apps/details?id=com.x.y"
    ).allowed


def test_policy_refuses_apk_mirrors_and_decompile():
    assert not policy.assert_source_allowed(
        "https://apkpure.com/notes/com.example.notes"
    ).allowed
    assert not policy.assert_source_allowed("https://example.com/app.apk").allowed
    assert not policy.assert_no_decompile("clone it by decompiling with jadx").allowed
    # An ordinary clone ask is fine.
    assert policy.assert_no_decompile("clone this play store app").allowed


def test_policy_rebrand_checklist():
    assert len(policy.rebrand_checklist()) >= 3
    assert "clean room" in policy.clean_room_banner().lower()


# ── SCHEMA ───────────────────────────────────────────────────────────────────
def test_feature_normalisation_and_csv_roundtrip():
    feats = [
        schema.Feature("a", "core", "P0", "yes", "YES"),
        schema.Feature("b", "settings", "weird", "no", "skip"),
    ]
    norm = [f.normalised() for f in feats]
    assert norm[0].priority == "must" and norm[0].clone == "yes"
    assert norm[1].priority == "could" and norm[1].original == "no"
    recon = schema.ReconMap(app="X", features=norm)
    parsed = schema.parse_features_csv(recon.render_features_csv())
    assert [f.feature for f in parsed] == ["a", "b"]
    assert parsed[1].original == "no"


def test_recon_render_and_stats():
    recon = schema.ReconMap(
        app="X",
        features=[
            schema.Feature("m1", "core", "must", "yes", "yes"),
            schema.Feature("m2", "core", "must", "yes", "no"),
            schema.Feature("s1", "nav", "should", "yes", "no"),
            schema.Feature("e1", "enhancement", "should", "no", "yes"),
        ],
    )
    stats = toolkit.feature_stats(recon.features)
    assert stats["must_total"] == 2 and stats["must_done"] == 1
    assert stats["extras"] == 1 and stats["shippable"] is False
    missing = toolkit.missing_in_build_order(recon.features)
    assert [f.feature for f in missing] == ["m2", "s1"]
    assert "Size:" in recon.render_recon_md()


# ── STORE ────────────────────────────────────────────────────────────────────
def test_play_url_parts_and_manual_listing(tmp_path):
    pkg, hl, gl = store.play_url_parts(
        "https://play.google.com/store/apps/details?id=com.a.b&hl=en&gl=IN"
    )
    assert pkg == "com.a.b" and hl == "en" and gl == "IN"
    p = tmp_path / "listing.json"
    p.write_text(json.dumps({"name": "Y", "description": "d"}), encoding="utf-8")
    loaded = store.load_manual_listing(p)
    assert loaded["ok"] and loaded["name"] == "Y"


def test_parse_listing_html_extracts_fields():
    page = (
        "<html><head>"
        '<meta property="og:title" content="NotesPro - Apps on Google Play">'
        '<meta name="description" content="Notes, done right.">'
        "<title>NotesPro - Apps on Google Play</title>"
        "</head><body>"
        # a nav category link with no visible label must be skipped
        '<a href="/store/apps/category/FAMILY"></a>'
        '<a href="/store/apps/category/PRODUCTIVITY">Productivity</a>'
        '<span aria-label="Rated 4.6 stars out of five stars">4.6</span>'
        '"a fairly long description string that has definitely more than '
        'twelve words in it so the parser picks it up as the description"'
        "https://play-lh.googleusercontent.com/abc123=w526-h296"
        "</body></html>"
    )
    data = store.parse_listing_html(page, "https://play.google.com/x")
    assert data["name"] == "NotesPro"  # suffix stripped
    assert data["short_description"] == "Notes, done right."
    assert data["rating"] == 4.6  # from the a11y label
    assert data["category"] == "Productivity"  # not the nav FAMILY link
    assert data["screenshots"]


# ── TOOLS ────────────────────────────────────────────────────────────────────
def test_toolkit_parity_contrast_listing_sweep(tmp_path):
    (tmp_path / "features.csv").write_text(
        "feature,area,priority,original,clone,notes\n"
        "a,core,must,yes,yes,\nb,core,must,yes,no,\n",
        encoding="utf-8",
    )
    parity = toolkit.parity_score(tmp_path / "features.csv")
    assert parity["ok"] and parity["overall"] == pytest.approx(50.0, abs=0.1)

    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps(agents.DEFAULT_TOKENS), encoding="utf-8")
    contrast = toolkit.contrast_check(tokens)
    assert contrast["ok"] and contrast["passed"], contrast.get("rendered")

    lint = toolkit.lint_listing(
        {
            "avoid": ["NotesPro"],
            "google_play": {
                "title": "NotesPro Rebuild",
                "short_description": "x",
                "full_description": "y",
            },
        }
    )
    assert lint["ok"] and not lint["passed"]  # the original's name slipped in

    # sweep finds a leftover of the original in a source file
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "Main.kt").write_text(
        "val x = NotesPro.FOO\n", encoding="utf-8"
    )
    sweep = toolkit.sweep_project(tmp_path, ["NotesPro"], [], [])
    assert sweep["ok"] and not sweep["clean"] and sweep["hits"]


def test_toolkit_rank_reviews(tmp_path):
    csv = tmp_path / "reviews.csv"
    csv.write_text(REVIEWS_CSV, encoding="utf-8")
    result = toolkit.rank_reviews(csv)
    assert result["ok"] and result["reviews"] == 4
    insights = toolkit.reviews_to_insights(result)
    assert insights and all(i.count >= 1 for i in insights)


def test_measure_palette_reads_a_png(tmp_path):
    from orchestrator.agents.replica.tools import imgdiff

    rows = [[(80, 40, 200) for _ in range(32)] for _ in range(32)]
    png = tmp_path / "shot.png"
    imgdiff.write_png(str(png), 32, 32, rows)
    measured = agents.measure_palette([png])
    assert measured and measured[0]["hex"] == "#5020C0"


# ── AGENTS (full pack, offline) ──────────────────────────────────────────────
def test_full_pack_offline_produces_artifacts(workspace):
    pipe = ReplicaPipeline(workspace, llm=NullLLM())
    res = pipe.run(
        listing=LISTING,
        app_name="NotesRebuild",
        build=False,
        resume=False,
        fail_under=0,
    )
    stages = res["stages"]
    for name in (
        "recon",
        "architecture",
        "design",
        "critic",
        "enhance",
        "build",
        "verify",
    ):
        assert name in stages
    assert stages["recon"]["ok"] and stages["architecture"]["ok"]
    assert stages["design"]["ok"] and stages["critic"]["ok"]
    assert stages["enhance"]["ok"] and stages["build"]["ok"]
    # every artifact the pack promises exists
    for p in (
        "recon.md",
        "features.csv",
        "architecture.md",
        "design/tokens.json",
        "design/components.md",
        "feedback.md",
        "fixes.md",
        "positioning.md",
        "brand.json",
        "brand.md",
        "launch/listing.json",
        "parity.md",
    ):
        assert (workspace / "replica" / p).is_file(), p
    # no enhancements unless reviews were ranked — here they were
    assert stages["enhance"]["enhancements"]
    # the rebuilt name must not carry the original's identity
    assert all("NotesPro" not in n for n in stages["enhance"]["names"])
    assert "NotesPro" not in stages["enhance"]["name_title"]
    assert stages["enhance"]["lint"]["passed"]


def test_critic_never_invents_reviews(tmp_path):
    ws = tmp_path / "NoReviews"
    ws.mkdir()
    pipe = ReplicaPipeline(ws, llm=NullLLM())
    res = pipe.run(listing=LISTING, app_name="X", build=False, resume=False)
    critic = res["stages"]["critic"]
    assert critic["ok"] and critic["ranked"] is False
    assert "no reviews" in critic["note"].lower()
    assert "No reviews collected" in (ws / "replica" / "feedback.md").read_text()


def test_builder_plans_without_gradle(workspace):
    proj = ReplicaProject(root=workspace / "replica").ensure()
    agents.ScoutStep().run(proj, NullLLM(), listing=LISTING)
    agents.StudyStep().run(proj, NullLLM())
    out = agents.BuilderStep().run(proj, NullLLM(), build=False)
    assert out["ok"] and out["built"] is False
    assert proj.build_log_md.is_file()


# ── READY GATE ───────────────────────────────────────────────────────────────
def test_verify_says_shippable_only_when_every_gate_passes(workspace):
    proj = ReplicaProject(root=workspace / "replica").ensure()
    agents.ScoutStep().run(proj, NullLLM(), listing=LISTING)
    agents.StudyStep().run(proj, NullLLM())
    agents.DesignStep().run(proj, NullLLM())
    agents.EnhanceStep().run(proj, NullLLM(), fixes=[])

    # Every counted must/should feature is done, and the listing is clean.
    proj.write(
        proj.features_csv,
        "feature,area,priority,original,clone,notes\n"
        "create notes,core,must,yes,yes,\n"
        "sync notes,core,must,yes,yes,\n",
    )
    proj.write(
        proj.listing_json,
        json.dumps(
            {
                "avoid": ["NotesPro"],
                "google_play": {
                    "title": "Productivity Companion",
                    "short_description": "Notes, done better for once.",
                    "full_description": "An independent clean-room rebuild.",
                },
            }
        ),
    )
    proj.write(
        proj.brand_json,
        json.dumps({"avoid": ["NotesPro"], "domains": [], "colors": []}),
    )
    out = agents.VerifyStep().run(proj, NullLLM(), project_dir=str(workspace))
    assert out["ready"] is True, out["gates"]
    assert "SHIPPABLE" in proj.parity_md.read_text()


# ── THE AGENT ────────────────────────────────────────────────────────────────
def _req(text, **meta):
    return AgentRequest(
        task_id="task-test-replica-0001",
        task_type=TaskType.REPLICA,
        text=text,
        metadata=meta,
    )


def test_agent_clone_completes_offline(workspace):
    agent = ReplicaAgent(llm=NullLLM())
    res = agent.handle(
        _req(
            "clone this play store app and make it better: " + LISTING["url"],
            workspace=str(workspace),
            listing=LISTING,
            app_name="NotesRebuild",
            resume=False,
        )
    )
    assert res.status is TaskStatus.COMPLETED, res.text
    assert res.data["stages"]["recon"]["ok"]
    assert "Artifacts under" in res.text
    assert "clean room" in res.text.lower()


def test_agent_refuses_decompile():
    agent = ReplicaAgent(llm=NullLLM())
    res = agent.handle(_req("decompile this apk with jadx and copy its code"))
    assert res.status is TaskStatus.DENIED
    assert "refused" in res.text.lower()


def test_agent_list_and_status(workspace):
    agent = ReplicaAgent(llm=NullLLM())
    listed = agent.handle(_req("replica agent list", action="list"))
    assert listed.status is TaskStatus.COMPLETED
    assert "clone" in listed.text and "never decompiles" in listed.text.lower()
    status = agent.handle(_req("status", action="status", workspace=str(workspace)))
    assert status.status is TaskStatus.COMPLETED
    assert "status" in status.text.lower()


# ── CLASSIFIER ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "ask",
    [
        "clone this app from the play store and make it better",
        "https://play.google.com/store/apps/details?id=com.example.notes",
        "make my own version of this android app",
        "replica agent, rebuild this play store app",
    ],
)
def test_classifier_routes_replica(ask):
    assert IntentClassifier().classify(ask).task_type is TaskType.REPLICA


@pytest.mark.parametrize(
    "ask",
    [
        "build an android app that tracks my habits",
        "fix the crash in my todo app",
        "create a python script",
    ],
)
def test_classifier_leaves_plain_coding_alone(ask):
    # Plain coding asks must not be stolen by the replica lane.
    assert IntentClassifier().classify(ask).task_type is not TaskType.REPLICA


# ── WIRING ───────────────────────────────────────────────────────────────────
def test_registry_registers_real_agent_behind_its_gate(monkeypatch):
    monkeypatch.setenv("JARVIS_REPLICA_AGENT", "on")
    reg = build_default_registry()
    agent = reg.get("replica_agent")
    assert agent is not None and not isinstance(agent, StubAgent)
    monkeypatch.delenv("JARVIS_REPLICA_AGENT", raising=False)
    reg2 = build_default_registry()
    assert isinstance(reg2.get("replica_agent"), StubAgent)


def test_action_tool_is_discoverable():
    import importlib

    mod = importlib.import_module("actions.replica_agent")
    assert mod.TOOL["name"] == "replica_agent"
    assert callable(mod.TOOL["handler"])
    help_text = mod.replica_agent({})
    assert "play" in help_text.lower() and "decompile" in help_text.lower()
