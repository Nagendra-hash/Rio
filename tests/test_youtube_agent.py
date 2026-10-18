"""
tests/test_youtube_agent.py — offline tests for the Phase 21 YouTube pack.

No network, no model, no upload: the model is a NullLLM, analytics are supplied
as manual dicts, and ffmpeg (if present) only ever renders a local silent MP4.
This locks the pack's own logic:

  POLICY        fabrication/engagement-buying is refused; unlicensed media is
                flagged; publishing needs approval + intent + a real MP4 +
                narration + a clean fact ledger
  METADATA      the YouTube upload rules port faithfully (title/desc/tags)
  PROVENANCE    a supported claim needs a *verified* source; a waiver needs a
                note; unresolved claims → status "blocked"
  READINESS     a blocking failure fails the gate; missing probes warn (never
                fake a pass); secrets are redacted
  LEARNING      metrics → baseline → deltas → net revenue/ROI, with simulated
                analytics excluded and revenue gated on availability+currency
  RETENTION     a thin curve is not stored; a real one names the biggest drop
  DISCOVER      findings normalise; fingerprints are stable; a dismissal needs
                a reason
  PIPELINE      the full loop runs offline and writes every artifact; a
                placeholder render is not publishable; render=True (when ffmpeg
                exists) produces a real MP4; learn stores/approves proposals
  AGENT         YouTubeAgent.handle: run → COMPLETED, fabricate → DENIED,
                list/status render; the classifier routes YouTube asks to
                YOUTUBE and leaves plain script/app asks alone
  WIRING        JARVIS_YOUTUBE_AGENT=on registers the real agent, unset → stub;
                the action TOOL is discoverable

Run:  .venv/bin/python -m pytest tests/test_youtube_agent.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pytest  # noqa: E402

from orchestrator.agents import build_default_registry  # noqa: E402
from orchestrator.agents.youtube import (agents, policy, schema,  # noqa: E402
                                         toolkit)
from orchestrator.agents.youtube.pipeline import YouTubePipeline  # noqa: E402
from orchestrator.agents.youtube.project import YouTubeProject  # noqa: E402
from orchestrator.agents.youtube.tools import (discoverability, learning,  # noqa: E402
                                               metadata, provenance, readiness,
                                               retention)
from orchestrator.agents.youtube.youtube_agent import YouTubeAgent  # noqa: E402
from orchestrator.base import StubAgent  # noqa: E402
from orchestrator.classifier import IntentClassifier  # noqa: E402
from orchestrator.contracts import (AgentRequest, TaskStatus,  # noqa: E402
                                    TaskType, new_task_id)


class NullLLM:
    """No model: every stage must fall back to its deterministic path."""
    def text(self, prompt, system=""):        # noqa: D401
        return None


CHANNEL = {
    "name": "Espresso Weekends",
    "objective": "help home baristas pull better shots",
    "audience": "home espresso beginners",
    "value_proposition": "clear, testable espresso advice",
    "pillars": ["Dialing in", "Gear on a budget", "Milk texture"],
    "default_format": "tutorial",
    "voice": "warm, precise, no hype",
    "kpi": "watch_time", "target_value": 4000, "target_window_days": 28,
    "blocked_topics": ["medical claims"],
    "privacy_status": "private",
}

EVIDENCE = {"24h": {
    "videoId": "vid123",
    "analytics": {
        "views": {"totalViews": 120, "totalImpressions": 4000,
                  "averageCTR": 3.5},
        "watchTime": {"totalWatchTime": 300, "averageViewPercentage": 42,
                      "averageViewDuration": 150},
        "engagement": {"engagementRate": 4.2},
        "outcomes": {"subscribersAvailable": True, "netSubscribers": 12,
                     "revenueAvailable": True, "estimatedRevenue": 5.0,
                     "currency": "USD"},
    },
    "thumbnailMetrics": {"impressions": 4000, "clickThroughRate": 3.5},
}}


@pytest.fixture()
def workspace(tmp_path):
    ws = tmp_path / "Channel"
    ws.mkdir()
    return ws


# ── POLICY ───────────────────────────────────────────────────────────────────
def test_policy_refuses_fabrication_and_bought_engagement():
    assert not policy.assert_no_fabrication(
        "make up some statistics for the intro").allowed
    assert not policy.assert_no_fabrication(
        "buy 10000 views for my video").allowed
    assert not policy.assert_no_fabrication(
        "invent a couple of Amazon reviews to quote").allowed
    assert policy.assert_no_fabrication(
        "write a script about home espresso").allowed


def test_policy_flags_unlicensed_media():
    assets = [{"path": "clip.mp4", "license": "unknown"},
              {"path": "own.mp4", "license": "owned"},
              {"path": "gen.png", "generated": True}]
    bad = policy.media_violations(assets)
    assert len(bad) == 1 and "clip.mp4" in bad[0]


def test_policy_publish_requires_approval_intent_real_video_and_narration():
    bundle = {"finalVideo": {"path": "/x.mp4", "simulated": False},
              "audio": {"path": "/a.wav", "simulated": False}, "assets": []}
    prov = {"status": "not_required"}
    assert policy.assert_publishable(bundle, prov, approved=True,
                                     publish=True).allowed
    # no approval
    assert not policy.assert_publishable(bundle, prov, approved=False,
                                         publish=True).allowed
    # no intent
    assert not policy.assert_publishable(bundle, prov, approved=True,
                                         publish=False).allowed
    # simulated video
    sim = {"finalVideo": {"path": "/x.mp4", "simulated": True},
           "audio": {"path": "/a.wav"}, "assets": []}
    assert not policy.assert_publishable(sim, prov, approved=True,
                                         publish=True).allowed
    # blocked facts
    assert not policy.assert_publishable(bundle, {"status": "blocked"},
                                         approved=True, publish=True).allowed


def test_policy_blocked_topic_and_redaction():
    assert policy.blocked_topic_hit("crypto giveaways and medical claims",
                                    ["medical claims"]) == "medical claims"
    assert policy.blocked_topic_hit("home espresso", ["medical claims"]) == ""
    assert "sk-secret" not in policy.redact("key sk-secret ABC")


# ── METADATA ─────────────────────────────────────────────────────────────────
def test_metadata_validate_and_limits():
    good = metadata.validate({"title": "How to dial in espresso",
                              "description": "A useful description.",
                              "tags": ["espresso", "coffee", "grinder"]})
    assert good["valid"] and good["value"]["tags"]
    bad = metadata.validate({"title": "", "description": "x",
                             "categoryId": "abc"})
    assert not bad["valid"]
    assert any("title" in e.lower() for e in bad["errors"])


def test_metadata_normalizes_tags_and_control_chars():
    tags = metadata.normalize_tags(["#Coffee", "coffee", "  grinder ",
                                    "a" * 400])
    assert tags[0] == "Coffee"
    assert tags.count("Coffee") + tags.count("coffee") == 1
    assert sum(len(t) for t in tags) <= metadata.MAX_TAGS_LENGTH
    assert "\x00" not in metadata.normalize(
        {"title": "a\x00b"}).get("title", "") or metadata.clean_text("a\x00b") == "a b"


# ── PROVENANCE ───────────────────────────────────────────────────────────────
def test_provenance_supported_claim_needs_verified_source():
    src = [{"url": "https://example.com/a", "status": "pending"}]
    claim = [{"text": "Coffee has caffeine", "status": "supported",
              "sourceUrls": ["https://example.com/a"]}]
    try:
        provenance.build({"sources": src, "claims": claim})
        assert False, "should have refused an unverified 'supported' claim"
    except provenance.Invalid:
        pass
    saved = provenance.build({
        "sources": [{"url": "https://example.com/a", "status": "verified"}],
        "claims": claim})
    assert saved["status"] == "verified"


def test_provenance_waiver_needs_note_and_status_blocked():
    try:
        provenance.build({"sources": [], "claims": [
            {"text": "claim", "status": "waived"}]})
        assert False
    except provenance.Invalid:
        pass
    blocked = provenance.build({"sources": [], "claims": [
        {"text": "unresolved"}]})
    assert blocked["status"] == "blocked"
    assert provenance.build({"sources": [], "claims": []})["status"] == "not_required"


# ── READINESS ────────────────────────────────────────────────────────────────
def test_readiness_blocking_failure_fails_and_missing_warns():
    def boom():
        raise RuntimeError("no provider")
    report = readiness.run({"text_provider": boom})
    assert report["status"] == "failed"
    assert "text_provider" in report["blockingFailures"]
    assert readiness.remediation_for("text_provider")
    with pytest.raises(readiness.Blocked):
        readiness.assert_ready(report, "Publishing")

    ok = readiness.run({"text_provider": lambda: "ready"})
    assert ok["status"] in ("warning", "passed")


def test_readiness_redacts_secrets():
    def leak():
        raise RuntimeError("bad key sk-ABCDEF123456 and Bearer xyz.abc")
    report = readiness.run({"text_provider": leak})
    msg = report["checks"][0]["message"]
    assert "sk-ABCDEF" not in msg and "[redacted]" in msg


# ── LEARNING ─────────────────────────────────────────────────────────────────
def test_learning_normalizes_and_gates_economics():
    metrics = learning.normalize_metrics(EVIDENCE["24h"], {
        "productionCost": {"amount": 1.0, "currency": "USD",
                           "complete": True}})
    assert metrics["views"] == 120
    assert metrics["netRevenue"] == 4.0
    assert metrics["roi"] == 400.0
    # incomplete cost → no ROI
    m2 = learning.normalize_metrics(EVIDENCE["24h"], {
        "productionCost": {"amount": 1.0, "currency": "USD"}})
    assert m2["roi"] is None


def test_learning_excludes_simulated_analytics():
    rep = {"videoId": "s", "analytics": {"simulated": True, "views":
                                         {"totalViews": 5}}}
    snap = learning.capture("24h", rep, {})
    assert snap["simulated"] and snap["confidence"] == "unverified"


def test_learning_baseline_and_deltas():
    prior = [{"contentAttributes": {"surface": "long_form"},
              "metrics": {"views": 100}}]
    snap = learning.capture("7d", EVIDENCE["24h"], {}, prior)
    assert snap["baseline"]["views"] == 100
    assert snap["deltas"]["views"] == 20.0


# ── RETENTION ────────────────────────────────────────────────────────────────
def test_retention_thin_curve_is_not_stored():
    assert retention.capture({"points": [{"elapsedRatio": 0.5,
                                          "audienceWatchRatio": 0.8}]},
                             {"retentionScenes": [{"duration": 10}]}) is None


def test_retention_maps_scenes_and_names_the_worst():
    points = [{"elapsedRatio": i / 20, "audienceWatchRatio": 1.0 - i * 0.03,
               "relativeRetentionPerformance": 0.5,
               "totalSegmentImpressions": 100} for i in range(1, 21)]
    scenes = [{"label": "Hook", "duration": 10},
              {"label": "Middle", "duration": 10},
              {"label": "End", "duration": 10}]
    snap = retention.capture({"videoId": "v", "points": points,
                              "durationSeconds": 30},
                             {"retentionScenes": scenes})
    assert snap and snap["sceneMetrics"]
    assert snap["summary"]["biggestDropScene"]
    rec = retention.build_recommendation(snap)
    assert rec is None or rec["status"] == "pending"


# ── DISCOVERABILITY ──────────────────────────────────────────────────────────
def test_discoverability_findings_and_dismiss_reason():
    pkg = discoverability.content_package(
        {"script": {"title": "espresso", "sections": [
            {"title": "a"}, {"title": "b"}, {"title": "c"}],
            "fullScript": "text"}, "seo": {"title": "espresso",
                                           "description": "hi"}})
    report = discoverability.audit(pkg)
    assert report["findings"]
    ids = {f["ruleId"] for f in report["findings"]}
    assert "title.no_hook_word" in ids
    fp = {f["ruleId"]: f["fingerprint"] for f in report["findings"]}
    report2 = discoverability.audit(pkg)
    assert {f["ruleId"]: f["fingerprint"] for f in report2["findings"]} == fp
    with pytest.raises(ValueError):
        discoverability.review_finding("dismissed", "no")
    assert discoverability.review_finding("dismissed", "false positive")["status"] \
        == "dismissed"


# ── SCHEMA ───────────────────────────────────────────────────────────────────
def test_schema_channel_normalisation():
    ch = schema.parse_channel({"name": "X", "default_format": "nonsense",
                               "kpi": "vibes",
                               "pillars": "a, b", "blocked_topics": "t1, t2"})
    assert ch.default_format == "explainer"
    assert ch.kpi == "views"
    assert ch.pillars == ["a", "b"] and ch.blocked_topics == ["t1", "t2"]


# ── PIPELINE (offline) ───────────────────────────────────────────────────────
def test_pipeline_offline_runs_every_stage(workspace):
    pipe = YouTubePipeline(workspace, llm=NullLLM())
    result = pipe.run(channel=CHANNEL)
    assert result["stages"]["research"]["ok"]
    assert result["stages"]["script"]["ok"]
    assert result["stages"]["production"]["ok"]
    assert result["stages"]["review"]["ok"]
    # The run COMPLETED (artifacts written) even though it is not yet
    # publishable (no real render) — a human-blocked run is not a failed run.
    assert result["ok"] is True
    assert result["ready"] is False
    assert result["stages"]["review"]["ready"] is False
    # every artifact is written
    names = {Path(a).name for a in result["artifacts"]}
    for required in ("research.md", "script.md", "seo.json", "production.md",
                     "review.md", "review-studio.html", "state.json"):
        assert required in names, required
    # no model → deterministic, and no claims invented
    script = pipe.project.load_script()
    assert script.sections and script.claims == []


def test_pipeline_publish_blocked_without_approval_or_real_render(workspace):
    pipe = YouTubePipeline(workspace, llm=NullLLM())
    pipe.run(channel=CHANNEL)
    r = pipe.run(steps=["publish"])
    assert r["stages"]["publish"]["allowed"] is False
    r2 = pipe.run(steps=["publish"], approved=True, publish=True)
    # still blocked: the render is simulated (no real MP4)
    assert r2["stages"]["publish"]["allowed"] is False


def test_pipeline_render_produces_real_mp4_when_ffmpeg_present(workspace):
    if not toolkit.ffmpeg_path():
        pytest.skip("ffmpeg not installed")
    pipe = YouTubePipeline(workspace, llm=NullLLM())
    pipe.run(channel=CHANNEL)
    res = agents.BY_NAME["production"].run(pipe.project, None, render=True,
                                           narration_text="__silent__")
    assert res["rendered"] and not res["simulated"]
    bundle = pipe.project.load_production()
    assert policy.real_video(bundle)
    assert Path(bundle["finalVideo"]["path"]).stat().st_size > 1000
    assert policy.narration_present(bundle)      # intentional silence recorded
    # and now publish is allowed with approval
    pipe.run(steps=["review"], resume=False)
    pub = pipe.run(steps=["publish"], approved=True, publish=True, resume=False)
    assert pub["stages"]["publish"]["allowed"] is True


def test_pipeline_narration_file_is_real_audio(workspace):
    if not toolkit.ffmpeg_path():
        pytest.skip("ffmpeg not installed")
    import subprocess
    pipe = YouTubePipeline(workspace, llm=NullLLM())
    pipe.run(channel=CHANNEL)
    media = pipe.project.media_dir
    media.mkdir(parents=True, exist_ok=True)
    narr = media / "my-narration.wav"
    subprocess.run([toolkit.ffmpeg_path(), "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", str(narr)],
                   check=True, capture_output=True)
    res = agents.BY_NAME["production"].run(pipe.project, None, render=True,
                                           narration_file=str(narr))
    assert res["rendered"]
    bundle = pipe.project.load_production()
    assert not bundle["audio"]["simulated"]
    assert policy.narration_present(bundle)
    assert any(a["kind"] == "audio" for a in bundle["assets"])


def test_pipeline_injected_tts_synthesizes_narration(workspace):
    if not toolkit.ffmpeg_path():
        pytest.skip("ffmpeg not installed")
    import subprocess
    calls: list[str] = []

    def fake_tts(text, out):
        calls.append(text[:24])
        subprocess.run([toolkit.ffmpeg_path(), "-y", "-f", "lavfi",
                        "-i", "anullsrc=r=44100:cl=mono", "-t", "1", out],
                       check=True, capture_output=True)
        return True

    pipe = YouTubePipeline(workspace, llm=NullLLM(), tts=fake_tts)
    pipe.run(channel=CHANNEL)
    res = pipe.run(steps=["production"], render=True, resume=False)
    assert calls, "the injected synthesizer must be called"
    assert res["stages"]["production"]["rendered"]
    bundle = pipe.project.load_production()
    assert bundle["audio"]["provider"] == "jarvis-tts"
    assert policy.narration_present(bundle)


def test_action_exposes_narrate_and_tts_glue():
    from actions import youtube_agent as yt
    props = yt.TOOL["parameters"]["properties"]
    assert "narrate" in props and "narration_file" in props
    assert yt._jarvis_tts("", "/tmp/opencode/none.wav") is False


VERIFIED_SOURCE = {
    "id": "src_lighting",
    "url": "https://www.ccohs.ca/oshanswers/ergonomics/lighting/lighting_general.html",
    "title": "Lighting Ergonomics — General", "status": "verified",
    "sourceType": "official",
}


def test_claims_from_script_marks_supported_only_for_verified():
    script = schema.Script(
        title="T", hook="H", cta="C",
        claims=[schema.Claim("Appropriate lighting reduces eye fatigue.",
                             source_urls=[VERIFIED_SOURCE["url"]]),
                schema.Claim("Uncited assertion.")])
    got = agents._claims_from_script(script, [VERIFIED_SOURCE])
    assert got[0]["status"] == "supported"
    assert got[1]["status"] == "pending"
    # an unverified source never promotes a claim
    pending = dict(VERIFIED_SOURCE, status="pending")
    assert agents._claims_from_script(
        script, [pending])[0]["status"] == "pending"


def test_apply_waivers_needs_a_note_and_matches_a_substring():
    claims = [{"text": "Alpha claim.", "status": "pending"},
              {"text": "Beta claim.", "status": "pending"}]
    assert agents._apply_waivers(claims, ["alpha"], "") == []
    assert claims[0]["status"] == "pending"
    done = agents._apply_waivers(claims, ["alpha"], "Checked by hand.")
    assert done == ["Alpha claim."]
    assert claims[0]["status"] == "waived" and claims[0]["notes"]


def test_review_resolves_claims_with_a_verified_source(workspace):
    pipe = YouTubePipeline(workspace, llm=NullLLM())
    pipe.run(channel=CHANNEL)
    claims = [{"text": "Appropriate lighting reduces eye fatigue.",
               "riskLevel": "standard", "status": "supported",
               "sourceIds": ["src_lighting"]}]
    r = pipe.run(steps=["review"], resume=False, sources=[VERIFIED_SOURCE],
                 claims=claims)
    rev = r["stages"]["review"]
    assert rev["gates"]["provenance_ok"], rev.get("findings")
    assert rev["provenance_status"] == "verified"


def test_review_refuses_a_supported_claim_without_a_verified_source(workspace):
    pipe = YouTubePipeline(workspace, llm=NullLLM())
    pipe.run(channel=CHANNEL)
    claims = [{"text": "A claim.", "status": "supported",
               "sourceIds": ["src_pending"]}]
    r = pipe.run(steps=["review"], resume=False,
                 sources=[dict(VERIFIED_SOURCE, id="src_pending",
                               status="pending")], claims=claims)
    rev = r["stages"]["review"]
    assert rev["ok"] is True                       # the report exists...
    assert rev["gates"]["provenance_ok"] is False  # ...but the gate blocks
    assert rev["provenance_status"] == "blocked"
    assert any("verified source" in f for f in rev["findings"])


def test_review_waive_requires_a_note(workspace):
    pipe = YouTubePipeline(workspace, llm=NullLLM())
    pipe.run(channel=CHANNEL)
    claims = [{"text": "Fresh beans taste better.", "status": "pending"}]
    rev = pipe.run(steps=["review"], resume=False, claims=claims,
                   waive=["*"], waive_note="")["stages"]["review"]
    assert rev["gates"]["provenance_ok"] is False
    assert any("waiv" in f.lower() for f in rev["findings"])


def test_review_waive_with_a_note_resolves(workspace):
    pipe = YouTubePipeline(workspace, llm=NullLLM())
    pipe.run(channel=CHANNEL)
    claims = [{"text": "Fresh beans taste better.", "status": "pending"}]
    rev = pipe.run(steps=["review"], resume=False, claims=claims,
                   waive=["fresh beans"],
                   waive_note="Reviewed by hand; freshness is well "
                              "established.")["stages"]["review"]
    assert rev["gates"]["provenance_ok"]
    assert rev["provenance_status"] == "verified"


def test_pipeline_learn_measures_and_approves(workspace):
    pipe = YouTubePipeline(workspace, llm=NullLLM())
    pipe.run(channel=CHANNEL)
    r = pipe.run(steps=["learn"], evidence=EVIDENCE, approve=["*"])
    learn = r["stages"]["learn"]
    assert learn["snapshots"]
    assert learn["recommendations"]
    assert all(rec["status"] == "approved" for rec in learn["recommendations"])
    # approved learnings steer the next plan
    r2 = pipe.run(steps=["research"], resume=False)
    assert r2["stages"]["research"]["research"].approved_learnings


def test_pipeline_learn_without_evidence_invents_nothing(workspace):
    pipe = YouTubePipeline(workspace, llm=NullLLM())
    pipe.run(channel=CHANNEL)
    r = pipe.run(steps=["learn"])
    learn = r["stages"]["learn"]
    assert learn["snapshots"] == []
    assert learn["recommendations"] == []
    assert "never fabricated" in learn["note"] or "no evidence" in learn["note"]


def test_pipeline_blocked_topic_is_never_planned(workspace):
    pipe = YouTubePipeline(workspace, llm=NullLLM())
    ch = dict(CHANNEL)
    ch["pillars"] = ["medical claims"]          # every pillar is blocked
    result = pipe.run(channel=ch)
    research = result["stages"]["research"]["research"]
    assert all("medical claims" not in t.topic for t in research.topics)


# ── AGENT ────────────────────────────────────────────────────────────────────
def test_agent_run_action_completes(workspace):
    agent = YouTubeAgent(llm=NullLLM())
    req = AgentRequest(task_id=new_task_id("yt"), task_type=TaskType.YOUTUBE,
                       text="make a youtube video for my channel",
                       metadata={"action": "run", "channel": CHANNEL,
                                 "workspace": str(workspace)})
    res = agent.handle(req)
    assert res.status is TaskStatus.COMPLETED
    assert "youtube_agent" in res.text
    assert "ready to publish: no" in res.text      # honest about the gate


def test_agent_denies_fabrication(workspace):
    agent = YouTubeAgent(llm=NullLLM())
    req = AgentRequest(task_id=new_task_id("yt"), task_type=TaskType.YOUTUBE,
                       text="make a video but invent statistics for the intro",
                       metadata={"action": "script", "workspace": str(workspace)})
    res = agent.handle(req)
    assert res.status is TaskStatus.DENIED


def test_agent_list_and_status_render(workspace):
    agent = YouTubeAgent(llm=NullLLM())
    l = agent.handle(AgentRequest(task_id=new_task_id("yt"),
                                  task_type=TaskType.YOUTUBE, text="list",
                                  metadata={"action": "list"}))
    assert "research" in l.text and "approval" in l.text.lower()
    st = agent.handle(AgentRequest(task_id=new_task_id("yt"),
                                   task_type=TaskType.YOUTUBE, text="status",
                                   metadata={"action": "status",
                                             "workspace": str(workspace)}))
    assert st.status is TaskStatus.COMPLETED


# ── CLASSIFIER ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("ask", [
    "make a youtube video about home espresso",
    "plan my channel content strategy",
    "write a video script with a hook and CTA",
    "how do I grow subscribers on youtube",
    "youtube agent, research topics for my channel",
])
def test_classifier_routes_youtube(ask):
    assert IntentClassifier().classify(ask).task_type is TaskType.YOUTUBE


@pytest.mark.parametrize("ask", [
    "create a python script",
    "build an android app that tracks my habits",
    "fix the crash in my todo app",
    # A bare 'youtube' mention must NOT be stolen by the channel lane.
    "play this youtube video",
    "search youtube for lo-fi music",
    "transcribe this youtube video",
])
def test_classifier_leaves_plain_asks_alone(ask):
    assert IntentClassifier().classify(ask).task_type is not TaskType.YOUTUBE


# ── WIRING ───────────────────────────────────────────────────────────────────
def test_registry_registers_real_agent_behind_its_gate(monkeypatch):
    monkeypatch.setenv("JARVIS_YOUTUBE_AGENT", "on")
    reg = build_default_registry()
    agent = reg.get("youtube_agent")
    assert agent is not None and not isinstance(agent, StubAgent)
    monkeypatch.delenv("JARVIS_YOUTUBE_AGENT", raising=False)
    reg2 = build_default_registry()
    assert isinstance(reg2.get("youtube_agent"), StubAgent)


def test_action_tool_is_discoverable():
    import importlib
    mod = importlib.import_module("actions.youtube_agent")
    assert mod.TOOL["name"] == "youtube_agent"
    assert callable(mod.TOOL["handler"])
    help_text = mod.youtube_agent({})
    assert "research" in help_text.lower() and "approval" in help_text.lower()
