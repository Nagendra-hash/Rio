"""
tests/test_video_agent.py — Phase 2: Video Creation Agent machinery.

MOCKED / OFFLINE by design:
    * every test runs against a tmp JARVIS_HOME + JARVIS_TOOLS_ROOT
    * the smoke pipeline's ffmpeg render is REAL and LOCAL (no network, no
      provider keys, no cost) — its CONTENT is SIMULATED and always labelled
    * worker tests spawn the real detached worker, like test_repo_agent.py

NOT covered here (later phases, honestly labelled when they arrive):
    real documentary script generation (Phase 4), real TTS narration
    (Phase 3/4), real AI video generation (Phase 6, billing-blocked).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.video_agent import pipelines                      # noqa: E402
from core.video_agent import spec as vspec                  # noqa: E402
from core.video_agent.runner import (describe_job,          # noqa: E402
                                     execute_video_job)
from core.repo_agent.jobs import RepoJobStore, start_job    # noqa: E402


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "jarvis"
    tools = tmp_path / "JarvisTools"
    home.mkdir()
    tools.mkdir()
    monkeypatch.setenv("JARVIS_HOME", str(home))
    monkeypatch.setenv("JARVIS_TOOLS_ROOT", str(tools))
    monkeypatch.setenv("JARVIS_REPO_AGENT_NOTIFY_HERMES", "0")
    return {"home": home, "tools": tools}


def _fake_progress():
    seen = []

    def progress(phase, pct, msg=""):
        seen.append((phase, float(pct)))

    return seen, progress


# ── 1. mode detection ────────────────────────────────────────────────────────

def test_detect_mode_documentary():
    mode, why = vspec.detect_mode(
        "create a 10-minute documentary about the psychology of "
        "procrastination")
    assert mode == vspec.DOCUMENTARY and "documentary" in why


def test_detect_mode_cinematic():
    mode, why = vspec.detect_mode("cinematic AI video of a futuristic city")
    assert mode == vspec.AI_CINEMATIC and "cinematic" in why


def test_detect_mode_shorts_beats_documentary():
    # 'vertical short' wins even when the word documentary also appears
    mode, why = vspec.detect_mode(
        "a vertical short version of my bitcoin documentary")
    assert mode == vspec.SHORTS and "short" in why


def test_detect_mode_duration_heuristics():
    assert vspec.detect_mode("a 20 minute video about tea")[0] == \
        vspec.DOCUMENTARY
    assert vspec.detect_mode("a 30 second video about tea")[0] == vspec.SHORTS
    mode, why = vspec.detect_mode("a video about tea")
    assert mode == vspec.DOCUMENTARY and "default" in why


def test_parse_duration_s():
    assert vspec.parse_duration_s("a 10-minute documentary") == 600
    assert vspec.parse_duration_s("45-second short") == 45
    assert vspec.parse_duration_s("2 min explainer") == 120
    assert vspec.parse_duration_s("no numbers here") is None


# ── 2. spec building ─────────────────────────────────────────────────────────

def test_build_spec_documentary_defaults():
    spec = vspec.build_spec({"topic": "procrastination"},
                            "a 10 minute documentary about procrastination")
    assert spec["mode"] == vspec.DOCUMENTARY
    assert spec["duration_s"] == 600
    assert spec["aspect_ratio"] == "16:9"
    assert spec["resolution"] == "1080p"
    assert spec["subtitles"] is True
    assert spec["pipeline"] == "documentary"


def test_build_spec_shorts_defaults():
    spec = vspec.build_spec({"topic": "what is bitcoin"},
                            "45-second YouTube Short about Bitcoin")
    assert spec["mode"] == vspec.SHORTS
    assert spec["duration_s"] == 45
    assert spec["aspect_ratio"] == "9:16"
    assert spec["subtitles"] is True


def test_build_spec_explicit_params_win():
    spec = vspec.build_spec({"topic": "tea", "mode": "SHORTS",
                             "duration_s": 60, "aspect_ratio": "1:1",
                             "music": "none", "subtitles": False})
    assert spec["duration_s"] == 60 and spec["aspect_ratio"] == "1:1"
    assert spec["music"] == "none" and spec["subtitles"] is False


def test_build_spec_asks_one_question_when_topic_missing():
    with pytest.raises(vspec.SpecError) as exc:
        vspec.build_spec({"duration_s": 45}, "make a short")
    assert "topic" in str(exc.value).lower()
    assert exc.value.question.count("?") == 1      # ONE clear question


def test_build_spec_clamps_duration_with_note():
    spec = vspec.build_spec({"topic": "tea", "mode": "SHORTS",
                             "duration_s": 5})
    assert spec["duration_s"] == 15 and spec["notes"]
    spec = vspec.build_spec({"topic": "tea", "mode": "SHORTS",
                             "duration_s": 500})
    assert spec["duration_s"] == 90 and spec["notes"]


def test_build_spec_smoke_override():
    spec = vspec.build_spec({"topic": "tea", "smoke": True},
                            "a video about tea")
    assert spec["pipeline"] == "smoke"


# ── 3. stage machine ─────────────────────────────────────────────────────────

def test_stage_order_matches_specification():
    assert vspec.VIDEO_STAGES == ("planning", "scripting", "asset_collection",
                                  "generating", "editing", "validating")
    pcts = [vspec.STAGE_PROGRESS[s] for s in vspec.VIDEO_STAGES]
    assert pcts == sorted(pcts) and pcts[0] > 0 and pcts[-1] < 1.0


def test_job_state_mapping():
    assert vspec.job_state({"status": "QUEUED"}) == "QUEUED"
    assert vspec.job_state({"status": "RUNNING", "phase": "scripting"}) == \
        "SCRIPTING"
    assert vspec.job_state({"status": "RUNNING", "phase": "starting"}) == \
        "RUNNING"
    assert vspec.job_state({"status": "COMPLETED"}) == "COMPLETED"
    assert vspec.job_state({"status": "NEEDS_CONFIGURATION"}) == \
        "NEEDS_CONFIGURATION"
    assert vspec.job_state(None) == "QUEUED"


# ── 4. smoke pipeline: real machinery, simulated content ─────────────────────

def test_smoke_pipeline_end_to_end(tmp_path):
    """Real ffmpeg render + ffprobe validation through all six stages.
    Content is SIMULATED (colour cards + tone) — labelled as such."""
    seen, progress = _fake_progress()
    out = tmp_path / "run"
    result = execute_video_job(
        {"job_id": "rt-test"}, {"spec": {"pipeline": "smoke",
                                         "mode": "DOCUMENTARY",
                                         "topic": "machinery test"},
                                "out_dir": str(out)},
        progress=progress, cancelled=lambda: False, log=lambda m: None)
    assert result["status"] == "COMPLETED"
    assert result["simulated"] is True and "SIMULATED" in result["message"]
    # stages ran in order with monotonic progress
    assert [p for p, _ in seen] == list(vspec.VIDEO_STAGES)
    assert [pct for _, pct in seen] == sorted(pct for _, pct in seen)
    # final artifact exists and is ffprobe-valid
    mp4 = out / "render" / "smoke.mp4"
    assert mp4.is_file() and mp4.stat().st_size > 0
    facts = pipelines.probe_video(mp4)
    assert facts["video_codec"] == "h264" and facts["audio_codec"] == "aac"
    assert (facts["width"], facts["height"]) == (640, 360)
    assert 9.0 <= facts["duration_s"] <= 12.0
    # durable stage state for later resume support
    state = json.loads((out / "stage_state.json").read_text())
    assert state["completed_stages"] == list(vspec.VIDEO_STAGES)
    # the mp4 is the primary output
    assert result["outputs"][0] == str(mp4)


def test_runner_cancellation_between_stages(tmp_path):
    """Cancel after planning: scripting must never run, result CANCELLED."""
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 1          # planning runs, then cancel flips

    with pytest.raises(Exception) as exc:
        execute_video_job(
            {"job_id": "rt-x"},
            {"spec": {"pipeline": "smoke", "topic": "t"},
             "out_dir": str(tmp_path / "c")},
            progress=lambda *_: None, cancelled=cancelled,
            log=lambda m: None)
    assert "cancel" in str(exc.value).lower()
    state = json.loads((tmp_path / "c" / "stage_state.json").read_text())
    assert state["completed_stages"] == ["planning"]


def test_unknown_pipeline_is_honest(tmp_path):
    from core.video_agent.pipelines import StageNotImplemented
    with pytest.raises(StageNotImplemented) as exc:
        execute_video_job(
            {"job_id": "rt-x"},
            {"spec": {"pipeline": "holodeck", "topic": "t"},
             "out_dir": str(tmp_path / "d")},
            progress=lambda *_: None, cancelled=lambda: False,
            log=lambda m: None)
    assert "not implemented" in str(exc.value).lower()


def test_describe_job_uses_mission_states():
    assert "SCRIPTING" in describe_job({"job_id": "rt-1",
                                        "status": "RUNNING",
                                        "phase": "scripting", "progress": .25})
    line = describe_job({"job_id": "rt-2", "status": "COMPLETED",
                         "outputs": json.dumps(["/x/v.mp4"])})
    assert "COMPLETED" in line and "/x/v.mp4" in line


# ── 5. worker path: real detached worker executes a video job ────────────────

def test_video_job_end_to_end_via_worker(isolated, tmp_path):
    """The REAL path: start_job → detached worker → stage machine →
    COMPLETED with the result file written before the terminal state."""
    out = tmp_path / "w-run"
    result = start_job("video", "smoke", source="test",
                       params={"spec": {"pipeline": "smoke",
                                        "mode": "DOCUMENTARY",
                                        "topic": "worker walk"},
                               "out_dir": str(out)})
    assert result.get("ok") and result.get("job_id")
    job_id = result["job_id"]
    store = RepoJobStore()
    deadline = time.time() + 120
    while time.time() < deadline:
        job = store.get(job_id)
        if job["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.5)
    job = store.get(job_id)
    assert job["status"] == "COMPLETED", job
    # the store's terminal convention sets phase="done"; the durable stage
    # trail lives in <run_dir>/stage_state.json (checked below)
    assert job["phase"] in ("done", "validating")
    res = json.loads((store.path.parent / "results" /
                      f"{job_id}.json").read_text())
    assert res["state"] == "COMPLETED" and res["result"]["simulated"] is True
    mp4 = Path(res["result"]["outputs"][0])
    assert mp4.is_file() and mp4.stat().st_size > 0
    assert store.recover_stale() == []


def test_restart_recovery_marks_stale_video_job_failed(isolated):
    """A video job whose worker died is never stuck in RUNNING after a
    restart — recover_stale() fails it with an honest message."""
    store = RepoJobStore()
    created = store.create("video", "smoke", params={"spec": {}})
    job_id = created["job_id"]
    store.update(job_id, status="RUNNING", pid=999999,      # dead pid
                 phase="editing", progress=0.85)
    recovered = store.recover_stale()
    assert any(r["job_id"] == job_id for r in recovered)
    job = store.get(job_id)
    assert job["status"] == "FAILED" and "restart" in str(job["error"]).lower()


def test_duplicate_video_requests_dedup(isolated, tmp_path):
    """Repeated speech events / Telegram retries → one job, not two."""
    params = {"spec": {"pipeline": "smoke", "topic": "same topic"},
              "out_dir": str(tmp_path / "dd")}
    first = RepoJobStore().create("video", "smoke", params=params)
    second = RepoJobStore().create("video", "smoke", params=params)
    assert first["job_id"] == second["job_id"]
    assert second["deduplicated"] is True


# ── 6. action layer ──────────────────────────────────────────────────────────

def _approve_and_queue(params: dict, method: str = "CANVAS") -> str:
    """Walk the mandatory method-approval flow (Phase 6): create → question
    with token → create again with token + chosen method."""
    import re
    from actions.video_creator import video_creator
    first = video_creator(dict(params))
    match = re.search(r"token ([0-9a-f]{16})", first)
    assert match, f"no approval token in reply:\n{first}"
    return video_creator({**params, "approve_token": match.group(1),
                          "method": method})


def test_video_creator_smoke_action_queues_job(isolated, tmp_path):
    answer = _approve_and_queue({"action": "create", "topic": "tea ceremony",
                                 "smoke": True,
                                 "save_dir": str(tmp_path / "s-out")})
    assert "queued" in answer.lower() and "SIMULATED" in answer
    store = RepoJobStore()
    rows = store.list(limit=5, kind="video")
    assert rows and rows[0]["status"] in ("QUEUED", "RUNNING", "COMPLETED",
                                          "INSTALLING")


def test_video_creator_stages_action(isolated):
    from actions.video_creator import video_creator
    text = video_creator({"action": "stages"})
    assert "PLANNING" in text and "VALIDATING" in text
    assert "smoke" in text.lower() and "SIMULATED" in text


def test_video_creator_where_action(isolated):
    from actions.video_creator import video_creator
    assert "No completed video jobs" in video_creator({"action": "where"})


def test_documentary_create_queues_sample_first(isolated, tmp_path):
    """Phase-4 contract: a 10-minute documentary request defaults to the
    mission's sample-first policy — a ~30s verification cut, honestly
    described (motion graphics + user assets, NOT AI video)."""
    answer = _approve_and_queue(
        {"action": "create", "topic": "psychology of procrastination",
         "request": "a 10-minute documentary",
         "save_dir": str(tmp_path / "d-out")})
    low = answer.lower()
    assert "documentary job queued" in low
    assert "sample" in low
    assert "never pass stills off as ai video" in low.replace("—", " ")
    rows = RepoJobStore().list(limit=5, kind="video")
    assert rows and rows[0]["ref"] == "documentary"
    spec = json.loads(rows[0]["params"])["spec"]
    assert spec["sample"] is True and spec["duration_s"] == 30
    assert any("sample-first" in n for n in spec["notes"])


def test_full_documentary_flag_renders_full(isolated, tmp_path):
    _approve_and_queue(
        {"action": "create", "topic": "psychology of procrastination",
         "request": "a 10-minute documentary", "full": True,
         "save_dir": str(tmp_path / "f-out")})
    rows = RepoJobStore().list(limit=5, kind="video")
    assert rows
    spec = json.loads(rows[0]["params"])["spec"]
    assert spec.get("sample") is None and spec["duration_s"] == 600


def test_video_creator_default_mode_is_documentary(isolated):
    """An unqualified 'create a video about X' is a documentary request —
    queued sample-first; it must NEVER silently become a paid ViMax render."""
    answer = _approve_and_queue({"action": "create", "topic": "tea"})
    assert "documentary job queued" in answer.lower()
    assert not (RepoJobStore().list(limit=50, kind="run_skill"))


def test_video_creator_asks_one_question(isolated):
    from actions.video_creator import video_creator
    answer = video_creator({"action": "create", "duration_s": 45})
    assert answer.count("?") == 1 and "topic" in answer.lower()


def test_tool_declares_modes_and_actions():
    from actions.video_creator import TOOL
    desc = TOOL["description"].lower()
    for kw in ("documentary", "cinematic", "shorts", "smoke"):
        assert kw in desc
    assert "stages" in TOOL["parameters"]["properties"]["action"][
        "description"]


# ── 7. provider capability registry (Phase 3) ────────────────────────────────

def test_editor_probe_is_real(isolated):
    from core.video_agent import providers
    entry = providers.probe_editor()
    assert entry["health"] == "OK"
    assert "ffmpeg" in str(entry["detail"]).lower()
    assert entry["free_or_paid"].startswith("free")
    assert entry["last_tested"]


def test_voice_probe_reports_installed_truth(isolated):
    """gtts IS installed on this machine; kokoro was user-APPROVED and
    installed (2026-10-28) — the registry must reflect reality either way:
    an uninstalled kokoro must stay BLOCKED naming the approval-needed
    download, an installed one reports INSTALLED (never silently)."""
    from core.video_agent import providers
    from importlib.util import find_spec
    voice = providers.probe_voice()
    by_name = {p["provider"]: p for p in voice["providers"]}
    assert by_name["gtts"]["health"] == "INSTALLED"
    kokoro_installed = find_spec("kokoro") is not None
    if kokoro_installed:
        assert by_name["kokoro"]["health"] == "INSTALLED"
    else:
        assert by_name["kokoro"]["health"].startswith("BLOCKED")
        assert "approval" in by_name["kokoro"]["health"] or \
            "install" in by_name["kokoro"]["detail"].lower()
    assert voice["available"] is True


def test_video_generator_probe_is_honestly_blocked(isolated):
    from core.video_agent import providers
    gen = providers.probe_video_generator()
    assert gen["available"] is False
    for p in gen["providers"]:
        assert p["health"].startswith("BLOCKED")
        assert p["free_or_paid"] != "free" or "unusable" in p["free_or_paid"]
    # measured quota evidence is carried, not invented
    text = json.dumps(gen)
    assert "402" in text and "429" in text
    # local wan/ltx entry exists because this GPU has no driver
    assert any("hardware" in p["health"].lower()
               for p in gen["providers"])


def test_llm_roles_compose_the_house_registry(isolated):
    """SCRIPT_WRITER/RESEARCH come from the EXISTING orchestrator capability
    registry (no duplicate router) — free tiers first, gemini last."""
    from core.video_agent import providers
    roles = providers.probe_llm_roles()
    rows = [r for r in roles["providers"] if r.get("health") != "FAIL"]
    assert rows, "the house registry exposed no models for RESEARCH"
    tiers = [r["free_or_paid"] for r in rows]
    assert tiers[0].startswith("local/free")          # free-first
    assert any("gemini" in t for t in tiers)          # budget-guarded tail
    for r in rows:
        assert r["api_compat"] == "orchestrator-model-router"


def test_probe_all_persists_registry(isolated):
    from core.video_agent import providers
    from core.repo_agent.jobs import db_path
    providers.probe_all()
    path = db_path().parent / "video_providers.json"
    assert path.is_file()
    data = json.loads(path.read_text())
    assert "editor" in data["last_probe"]
    assert data["last_probe"]["editor"]["health"] == "OK"


def test_registry_table_renders_all_roles(isolated):
    from core.video_agent import providers
    table = providers.registry_table()
    for role in ("SCRIPT_WRITER", "VOICE_GENERATOR", "VIDEO_EDITOR",
                 "VIDEO_GENERATOR"):
        assert role in table
    assert "never passed off as AI-generated video" in table


def test_video_creator_providers_action(isolated):
    from actions.video_creator import video_creator
    out = video_creator({"action": "providers"})
    assert "capability registry" in out.lower()
    assert "BLOCKED" in out


# ── 8. script writer (Phase 3) ───────────────────────────────────────────────

class _FakeLLM:
    def __init__(self, reply=None, fail_times=0):
        self.reply = reply
        self.fail_times = fail_times
        self.calls = 0
        self.name = "fake"

    def text(self, prompt, *, system=""):
        self.calls += 1
        if self.fail_times >= self.calls:
            return None
        return self.reply


_GOOD_SCRIPT = json.dumps({
    "title": "Why We Procrastinate",
    "hook": "Everyone delays. Here is why.",
    "chapters": [
        {"title": "The brain's trade", "narration": "Chapter one text.",
         "visual_notes": "clock", "estimated_seconds": 30},
        {"title": "Feelings, not laziness", "narration": "Chapter two text.",
         "visual_notes": "diagram", "estimated_seconds": 30}],
    "ending": "And that is why.",
    "estimated_total_seconds": 60,
    "claims_needing_sources": ["mood repair theory"],
}, ensure_ascii=False)


def test_generate_script_parses_router_reply():
    from core.video_agent.script_writer import generate_script
    result = generate_script({"mode": "DOCUMENTARY", "topic": "procrastination",
                              "duration_s": 600}, llm=_FakeLLM(_GOOD_SCRIPT))
    assert result["ok"] is True
    assert result["script"]["title"] == "Why We Procrastinate"
    assert len(result["script"]["chapters"]) == 2
    assert result["script"]["timing_is_estimate"] is True
    assert result["script"]["estimated_total_seconds"] == 60


def test_generate_script_handles_json_fences():
    from core.video_agent.script_writer import generate_script
    fenced = "Here is your script:\n```json\n" + _GOOD_SCRIPT + "\n```"
    result = generate_script({"mode": "SHORTS", "topic": "x", "duration_s": 45},
                             llm=_FakeLLM(fenced))
    assert result["ok"] is True and result["script"]["ending"] == "And that is why."


def test_generate_script_never_fabricates():
    from core.video_agent.script_writer import generate_script
    result = generate_script({"mode": "DOCUMENTARY", "topic": "t"},
                             llm=_FakeLLM(None))
    assert result["ok"] is False and "fabricat" in result["error"].lower()
    assert "script" not in result


def test_generate_script_rejects_non_json_reply():
    from core.video_agent.script_writer import generate_script
    result = generate_script({"mode": "DOCUMENTARY", "topic": "t"},
                             llm=_FakeLLM("Once upon a time, in prose..."))
    assert result["ok"] is False and "parsable" in result["error"]


def test_script_prompt_forbids_fabrication():
    from core.video_agent.script_writer import _script_prompt
    prompt, system = _script_prompt({"mode": "DOCUMENTARY", "topic": "t",
                                     "duration_s": 600})
    assert "NEEDS SOURCE" in system and "fabricat" in system.lower()
    body = json.loads(prompt)
    assert body["duration_s"] == 600 and body["output_format"]["chapters"]


# ── 9. tts runner (Phase 3) ──────────────────────────────────────────────────

class _FakeEngine:
    """Writes a REAL tiny wav so duration measurement is real."""

    def __init__(self, name="fake", fail_first=0):
        self.name = name
        self.free_or_paid = "free"
        self.fail_first = fail_first

    def synth_to_file(self, path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        import wave as _w
        # ~12 chars/second speaking pace — realistic enough that scene
        # durations behave like real narration
        frames = int(24000 * max(1.5, len(text) / 12.0))
        with _w.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(b"\x00\x00" * frames)
        return {"path": str(path)}


def test_segment_script_splits_hook_chapters_ending():
    from core.video_agent.tts_runner import segment_script
    script = json.loads(_GOOD_SCRIPT)
    segs = segment_script(script)
    kinds = [s["kind"] for s in segs]
    assert kinds == ["hook", "chapter", "chapter", "ending"]
    assert segs[0]["text"].startswith("Everyone delays")


def test_synthesize_narration_measures_real_durations(tmp_path):
    from core.video_agent.tts_runner import synthesize_narration
    timings = synthesize_narration(json.loads(_GOOD_SCRIPT),
                                   tmp_path / "n", engines=[_FakeEngine()])
    assert timings["engine"] == "fake"
    assert timings["word_level_available"] is False
    assert timings["timing_granularity"] == "per-segment"
    assert len(timings["segments"]) == 4
    for seg in timings["segments"]:
        assert seg["duration_s"] > 0.5          # measured from the wav
        assert Path(seg["path"]).is_file()
    assert abs(timings["total_speech_s"] -
               sum(s["duration_s"] for s in timings["segments"])) < 0.01
    saved = json.loads((tmp_path / "n" / "narration" /
                        "timings.json").read_text())
    assert saved["segments"][0]["path"]


def test_synthesize_narration_retries_and_falls_back(tmp_path):
    from core.video_agent.tts_runner import synthesize_narration

    class Flaky(_FakeEngine):
        def synth_to_file(self, path, text):
            self.fail_first -= 1
            if self.fail_first >= 0:
                from core.video_agent.tts_runner import TTSError
                raise TTSError("engine hiccup")
            return super().synth_to_file(path, text)

    timings = synthesize_narration(json.loads(_GOOD_SCRIPT),
                                   tmp_path / "n2",
                                   engines=[Flaky("flaky", fail_first=1),
                                            _FakeEngine("backup")],
                                   retries=2)
    seg1 = timings["segments"][0]
    assert seg1["engine"] in ("flaky", "backup") and seg1["attempts"] <= 2
    assert len(timings["segments"]) == 4


def test_synthesize_narration_honest_failure(tmp_path):
    from core.video_agent.tts_runner import TTSError, synthesize_narration

    class Always(_FakeEngine):
        def synth_to_file(self, path, text):
            from core.video_agent.tts_runner import TTSError
            raise TTSError("down")

    with pytest.raises(TTSError) as exc:
        synthesize_narration(json.loads(_GOOD_SCRIPT), tmp_path / "n3",
                             engines=[Always()], retries=2)
    assert "incomplete" in str(exc.value).lower()
    assert "faked" in str(exc.value).lower()


def test_default_engines_reflect_installed_packages():
    """The ladder is reality-based: on this machine gtts is installed and
    kokoro/edge are not — so the default ladder contains gtts and never
    contains engines whose packages are missing."""
    from core.video_agent import tts_runner
    engines = tts_runner.default_engines()
    names = [e.name for e in engines]
    import importlib.util
    if importlib.util.find_spec("gtts"):
        assert "gtts" in names
    assert "kokoro" not in names or importlib.util.find_spec("kokoro")
    assert "edge-tts" not in names or importlib.util.find_spec("edge_tts")


def test_gtts_real_synth_when_online(tmp_path):
    """REAL free-cloud TTS integration (Phase 11 scenario 6, free tier, no
    key). Skipped honestly when offline — a mocked pass is not proof."""
    from core.video_agent.tts_runner import GttsEngine
    try:
        info = GttsEngine().synth_to_file(tmp_path / "real.mp3",
                                          "Testing narration.")
    except Exception as exc:                        # noqa: BLE001
        pytest.skip(f"gtts unreachable (offline?): {type(exc).__name__}")
    from core.video_agent.tts_runner import _wav_duration
    assert Path(info["path"]).stat().st_size > 500
    assert _wav_duration(Path(info["path"])) > 0.3


def test_kokoro_synth_to_wav_method_exists_offline():
    """The additive engine method is present and offline-safe: importing the
    class must not require the kokoro package (only CALLING synth does)."""
    from core.tts import KokoroTTSEngine
    assert callable(KokoroTTSEngine.synth_to_wav)


# ── 10. documentary pipeline (Phase 4) ───────────────────────────────────────

_OUTLINE_JSON = json.dumps({
    "working_title": "Why We Procrastinate",
    "angle": "Procrastination is emotion management, not time management.",
    "chapters": [
        {"title": "The mood repair loop", "key_points": ["short-term relief"],
         "estimated_seconds": 30},
        {"title": "Breaking the loop", "key_points": ["self-forgiveness"],
         "estimated_seconds": 30}],
    "search_queries": ["why people procrastinate psychology"],
    "claims_needing_sources": ["procrastination links to mood repair"],
    "opinions": ["deadlines make it worse"],
}, ensure_ascii=False)

_FAKE_SCRIPT = json.dumps({
    "title": "Why We Procrastinate",
    "hook": "Everyone delays. Here is why.",
    "chapters": [
        {"title": "The mood repair loop", "narration": "We delay tasks to feel better now.",
         "visual_notes": "clock", "estimated_seconds": 8},
        {"title": "Breaking the loop", "narration": "Small starts beat big plans.",
         "visual_notes": "diagram", "estimated_seconds": 8}],
    "ending": "Start small, today.",
    "estimated_total_seconds": 20,
    "claims_needing_sources": ["mood repair"],
    "sources_used": [],
}, ensure_ascii=False)


class _SequencedLLM:
    """Test double for the router: outline ask → outline JSON, script ask →
    script JSON (content-based, robust across per-stage instances)."""
    name = "fake-router"

    def text(self, prompt, *, system=""):
        if "research outline" in prompt:
            return _OUTLINE_JSON
        return _FAKE_SCRIPT


def test_apply_sample_policy():
    from core.video_agent.pipeline_doc import apply_sample_policy
    spec = apply_sample_policy({"duration_s": 600, "mode": "DOCUMENTARY",
                                "notes": []})
    assert spec["sample"] is True and spec["duration_s"] == 30
    assert any("sample-first" in n for n in spec["notes"])
    full = apply_sample_policy({"duration_s": 600, "mode": "DOCUMENTARY",
                                "full": True, "notes": []})
    assert full["duration_s"] == 600 and not full.get("sample")
    short = apply_sample_policy({"duration_s": 60, "mode": "DOCUMENTARY",
                                 "notes": []})
    assert short["duration_s"] == 60 and not short.get("sample")


def test_documentary_card_generation(tmp_path):
    from core.video_agent.pipeline_doc import _card
    out = tmp_path / "title.png"
    _card(out, "Why We Procrastinate", "a documentary", 1280, 720)
    assert out.is_file() and out.stat().st_size > 5000
    from PIL import Image
    img = Image.open(out)
    assert img.size == (1280, 720)


def test_srt_uses_real_segment_timings():
    from core.video_agent.pipeline_doc import _srt
    segs = [{"text": "Hello narration one.", "duration_s": 2.5},
            {"text": "Narration two, slightly longer.", "duration_s": 4.0}]
    srt = _srt(segs, [0.0, 3.2])
    assert srt.splitlines()[0] == "1"
    assert "00:00:00,000 --> 00:00:02,500" in srt
    assert "00:00:03,200 -->" in srt            # second scene starts at its
    assert "Narration two" in srt               # REAL cumulative start


def test_research_outline_honest_without_llm(tmp_path):
    """No model available → honest StageError, no fabricated outline."""
    from core.video_agent.pipeline_doc import research_outline

    class Dead:
        name = "dead"

        def text(self, prompt, *, system=""):
            return None

    ctx = {"run_dir": tmp_path / "r", "spec": {"topic": "t", "duration_s": 30},
           "log": lambda m: None, "llm": Dead()}
    with pytest.raises(Exception) as exc:
        research_outline(ctx)
    assert "outline failed" in str(exc.value)


def test_documentary_end_to_end_offline(tmp_path, monkeypatch):
    """Full stage machine, REAL ffmpeg render, offline: fake router replies
    (labelled test content) + fake TTS. Verifies scenes, concat, burned
    subtitles and honest asset manifest."""
    import core.video_agent.script_writer as sw
    import core.video_agent.tts_runner as tr
    monkeypatch.setattr(sw, "HouseLLM", _SequencedLLM)
    monkeypatch.setattr(tr, "default_engines", lambda lang=None: [_FakeEngine("fake")])
    from core.video_agent.pipeline_doc import STAGES as DOC
    seen, progress = _fake_progress()
    out = tmp_path / "doc-run"
    result = execute_video_job(
        {"job_id": "rt-doc"},
        {"spec": {"pipeline": "documentary", "mode": "DOCUMENTARY",
                  "topic": "procrastination", "duration_s": 30,
                  "subtitles": True},
         "out_dir": str(out)},
        progress=progress, cancelled=lambda: False, log=lambda m: None,
        registry={"documentary": DOC})
    assert result["status"] == "COMPLETED", result
    # every stage ran, in order
    assert [p for p, _ in seen] == list(vspec.VIDEO_STAGES)
    final = out / "render" / "documentary.mp4"
    assert final.is_file() and final.stat().st_size > 20000
    facts = pipelines.probe_video(final)
    assert facts["video_codec"] == "h264" and facts["audio_codec"] == "aac"
    assert (facts["width"], facts["height"]) == (1280, 720)
    assert facts["duration_s"] > 8              # 4 narration scenes + tails
    # research artifacts are real files with honest flags
    outline = json.loads((out / "research" / "outline.json").read_text())
    assert outline["provider"] == "fake-router"
    assert "claims_needing_sources" in outline
    # asset manifest records licenses + the honest no-AI note
    manifest = json.loads((out / "assets" / "manifest.json").read_text())
    assert manifest["ai_generated_visuals"] == 0
    assert all("license" in a for a in manifest["assets"])
    # per-segment captions with real timing granularity (Phase 10: the
    # work/ intermediates are cleaned — verify via the surviving record)
    assert not (out / "work").exists()
    timings = json.loads((out / "narration" / "timings.json").read_text())
    assert timings["word_level_available"] is False
    # the final MP4 is the primary output
    assert result["outputs"][0] == str(final)


def test_documentary_uses_user_images(tmp_path, monkeypatch):
    """User-provided images are matched to chapters in order and recorded as
    user-provided in the manifest (license honesty)."""
    import core.video_agent.script_writer as sw
    import core.video_agent.tts_runner as tr
    user_dir = tmp_path / "my-images"
    user_dir.mkdir()
    from PIL import Image
    for i in (1, 2):
        Image.new("RGB", (640, 360), (i * 40, 30, 30)).save(
            user_dir / f"photo_{i}.jpg")
    monkeypatch.setattr(sw, "HouseLLM", _SequencedLLM)
    monkeypatch.setattr(tr, "default_engines", lambda lang=None: [_FakeEngine("fake")])
    from core.video_agent.pipeline_doc import STAGES as DOC
    out = tmp_path / "doc-user"
    result = execute_video_job(
        {"job_id": "rt-doc2"},
        {"spec": {"pipeline": "documentary", "mode": "DOCUMENTARY",
                  "topic": "procrastination", "duration_s": 30,
                  "user_assets_dir": str(user_dir)},
         "out_dir": str(out)},
        progress=lambda *_: None, cancelled=lambda: False,
        log=lambda m: None, registry={"documentary": DOC})
    assert result["status"] == "COMPLETED", result
    manifest = json.loads((out / "assets" / "manifest.json").read_text())
    user_assets = [a for a in manifest["assets"]
                   if a.get("source", "").startswith("user-provided:")]
    assert len(user_assets) == 2


def test_script_writer_receives_gathered_sources():
    """The script prompt carries the research sources so claims can be
    grounded (mission: avoid unsupported claims)."""
    from core.video_agent.script_writer import _script_prompt
    prompt, system = _script_prompt({
        "mode": "DOCUMENTARY", "topic": "t", "duration_s": 600,
        "sources": [{"title": "Study", "url": "https://example.com/s",
                     "snippet": "mood repair"}]})
    body = json.loads(prompt)
    assert body["available_sources"][0]["url"] == "https://example.com/s"
    assert "available_sources" in system or "sources" in system
