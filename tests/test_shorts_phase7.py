"""
tests/test_shorts_phase7.py — Phase 7: vertical Shorts pipeline.

MOCKED / OFFLINE by design:
    * fake router replies + fake TTS engine (real ffmpeg render, real
      ffprobe validation)
    * the documentary→Shorts split reuses REAL audio fixtures — the reuse
      path must never call TTS
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.repo_agent.jobs import RepoJobStore, start_job     # noqa: E402
from core.video_agent import pipelines                       # noqa: E402
from core.video_agent import spec as vspec                   # noqa: E402
from core.video_agent.runner import execute_video_job        # noqa: E402

sys.path.insert(0, str(BASE / "tests"))
from test_video_agent import _FakeEngine                     # noqa: E402


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


_SHORT_SCRIPT = json.dumps({
    "title": "Bitcoin in 30 seconds",
    "description": "What Bitcoin actually is, fast.",
    "hook": "Bitcoin is money that no bank controls.",
    "beats": ["Every coin is tracked on one public ledger.",
              "No government can print more of it.",
              "You can send it anywhere in minutes."],
    "closing": "That is Bitcoin in one minute.",
    "claims_needing_sources": ["21 million cap"],
}, ensure_ascii=False)


class _ShortLLM:
    name = "fake-router"

    def text(self, prompt, *, system=""):
        if "vertical short" in prompt:
            return _SHORT_SCRIPT
        return json.dumps({"working_title": "t", "chapters": [],
                           "search_queries": []})


def _short_stages():
    return pipelines.stages_for("shorts")


# ── unit pieces ──────────────────────────────────────────────────────────────

def test_vcard_generates_vertical(tmp_path):
    from core.video_agent.pipeline_shorts import _vcard
    out = tmp_path / "title.png"
    _vcard(out, "Bitcoin is money that no bank controls.",
           branding="My Channel")
    assert out.is_file() and out.stat().st_size > 5000
    from PIL import Image
    assert Image.open(out).size == (1080, 1920)


def test_split_documentary_picks_evenly(tmp_path):
    from core.video_agent.pipeline_shorts import split_documentary
    doc = tmp_path / "doc"
    (doc / "narration").mkdir(parents=True)
    segs = [{"id": i, "kind": "chapter" if 1 < i < 7 else "hook",
             "duration_s": 3.0 + i / 10, "text": f"segment {i}",
             "path": str(doc / f"seg{i}.mp3")} for i in range(1, 8)]
    (doc / "narration" / "timings.json").write_text(json.dumps(
        {"engine": "gtts", "segments": segs}))
    picks = split_documentary(doc, 3)
    assert len(picks) == 3
    ids = [p["segment_ids"][0] for p in picks]
    assert len(set(ids)) == 3                       # distinct segments
    assert all(p["path"].endswith(".mp3") for p in picks)


def test_split_documentary_honest_without_narration(tmp_path):
    from core.video_agent.pipeline_shorts import StageError, split_documentary
    with pytest.raises(StageError) as exc:
        split_documentary(tmp_path / "empty", 3)
    assert "no narration timings" in str(exc.value).lower()


# ── end-to-end (offline): full stage machine, real vertical render ───────────

def test_shorts_end_to_end_offline(tmp_path, monkeypatch):
    import core.video_agent.script_writer as sw
    import core.video_agent.tts_runner as tr
    monkeypatch.setattr(sw, "HouseLLM", _ShortLLM)
    monkeypatch.setattr(tr, "default_engines", lambda lang=None: [_FakeEngine("fake")])
    seen, progress = [], lambda ph, pc, msg="": seen.append((ph, pc))
    out = tmp_path / "short-run"
    result = execute_video_job(
        {"job_id": "rt-short"},
        {"spec": {"pipeline": "shorts", "mode": "SHORTS",
                  "topic": "what is bitcoin", "duration_s": 45,
                  "subtitles": True, "branding": "My Channel"},
         "out_dir": str(out)},
        progress=progress, cancelled=lambda: False, log=lambda m: None,
        registry={"shorts": _short_stages()})
    assert result["status"] == "COMPLETED", result
    final = out / "render" / "short.mp4"
    assert final.is_file() and final.stat().st_size > 20000
    facts = pipelines.probe_video(final)
    assert (facts["width"], facts["height"]) == (1080, 1920)   # vertical
    assert facts["video_codec"] == "h264" and facts["audio_codec"] == "aac"
    assert facts["duration_s"] > 6
    # captions: per-segment REAL timing (Phase 10: work/ intermediates are
    # cleaned after the render — verify via the surviving narration record)
    timings = json.loads((out / "narration" / "timings.json").read_text())
    assert timings["word_level_available"] is False
    assert timings["timing_granularity"] == "per-segment"
    assert any("Bitcoin is money" in s["text"] for s in timings["segments"])
    assert not (out / "work").exists()          # bounded cache cleaned
    # optional platform metadata written beside the video
    assert "Bitcoin" in (out / "render" / "metadata.txt").read_text()
    # honest asset manifest
    manifest = json.loads((out / "assets" / "manifest.json").read_text())
    assert manifest["ai_generated_visuals"] == 0
    # branding reached the cards (manifest lists generated assets)
    assert any(a["asset"] == "cards/title.png" for a in manifest["assets"])


def test_shorts_via_worker_end_to_end(isolated, tmp_path):
    """REAL path: start_job → detached worker → COMPLETED vertical MP4.
    Uses the documentary-reuse path (no LLM) so the test is hermetic — the
    free LLM chain is network-dependent and covered by the live run."""
    _make_documentary_run(tmp_path / "doc")
    result = start_job("video", "shorts", source="test", params={
        "spec": {"pipeline": "shorts", "mode": "SHORTS",
                 "topic": "short from my documentary", "duration_s": 45,
                 "reuse_narration_from": str(tmp_path / "doc"),
                 "segment_ids": [2, 3]},
        "out_dir": str(tmp_path / "w-short")})
    assert result.get("ok")
    store = RepoJobStore()
    deadline = __import__("time").time() + 180
    while __import__("time").time() < deadline:
        job = store.get(result["job_id"])
        if job["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        __import__("time").sleep(0.5)
    job = store.get(result["job_id"])
    assert job["status"] == "COMPLETED", job
    res = json.loads((store.path.parent / "results" /
                      f"{result['job_id']}.json").read_text())
    mp4 = Path(res["result"]["outputs"][0])
    facts = pipelines.probe_video(mp4)
    assert (facts["width"], facts["height"]) == (1080, 1920)


# ── documentary → Shorts via the split action ────────────────────────────────

def _make_documentary_run(run_dir: Path) -> None:
    """A minimal completed-documentary shape: REAL playable audio (ffmpeg
    sine tones) + measured timings — the reuse path feeds these to ffmpeg."""
    import subprocess as _sp
    narr = run_dir / "narration"
    narr.mkdir(parents=True)
    segs = []
    for i in range(1, 6):
        wav = narr / f"segment_{i:02d}.mp3"
        _sp.run(["ffmpeg", "-y", "-f", "lavfi",
                 "-i", f"sine=frequency={300 + i * 40}:duration=4.0",
                 "-c:a", "libmp3lame", "-q:a", "9", str(wav)],
                capture_output=True, timeout=60, check=True)
        segs.append({"id": i, "kind": "chapter" if 1 < i < 5 else "hook",
                     "duration_s": 4.0, "text": f"Chapter {i} text.",
                     "path": str(wav), "engine": "gtts"})
    (narr / "timings.json").write_text(json.dumps(
        {"engine": "gtts", "timing_granularity": "per-segment",
         "word_level_available": False, "total_speech_s": 20.0,
         "segments": segs}))


def test_split_action_queues_reuse_jobs(isolated, tmp_path):
    import re
    from actions.video_creator import video_creator
    doc = tmp_path / "doc-run"
    _make_documentary_run(doc)
    params = {"action": "split", "save_dir": str(doc), "count": 3}
    first = video_creator(dict(params))
    token = re.search(r"token ([0-9a-f]{16})", first)
    assert token, first                        # Phase 6 split approval
    answer = video_creator({**params, "approve_token": token.group(1)})
    assert "3 Short(s) queued" in answer
    rows = RepoJobStore().list(limit=10, kind="video")
    assert len(rows) == 3
    for row in rows:
        spec = json.loads(row["params"])["spec"]
        assert spec["pipeline"] == "shorts"
        assert spec["reuse_narration_from"] == str(doc)
        assert spec["segment_ids"]


def test_split_action_reuse_never_calls_tts(isolated, tmp_path):
    """The reuse path must NOT re-synthesize narration — the copied audio
    files ARE the documentary's bytes (mission: reuse where practical)."""
    import shutil
    import core.video_agent.tts_runner as tr

    def _boom(lang=None):
        raise AssertionError("TTS must not run when narration is reused")

    monkeyed = tmp_path / "tts-guard"
    monkeyed.mkdir()
    doc = tmp_path / "doc-run"
    _make_documentary_run(doc)
    picks = json.loads(json.dumps(
        __import__("core.video_agent.pipeline_shorts",
                   fromlist=["split_documentary"]).split_documentary(doc, 2)))
    out = tmp_path / "short-run"
    spec = {"pipeline": "shorts", "mode": "SHORTS", "topic": "t",
            "duration_s": 45, "reuse_narration_from": str(doc),
            "segment_ids": picks[0]["segment_ids"]}
    monkeypatch_guard = tr.default_engines
    tr.default_engines = _boom
    try:
        result = execute_video_job(
            {"job_id": "rt-reuse"},
            {"spec": spec, "out_dir": str(out)},
            progress=lambda *_: None, cancelled=lambda: False,
            log=lambda m: None, registry={"shorts": _short_stages()})
    finally:
        tr.default_engines = monkeypatch_guard
    assert result["status"] == "COMPLETED", result
    assert result["stage_notes"]["reused"] is True
    timings = json.loads((out / "narration" / "timings.json").read_text())
    assert timings["engine"].startswith("reused:")
    src_bytes = (Path(picks[0]["path"])).read_bytes()
    reused = Path(timings["segments"][0]["path"]).read_bytes()
    assert src_bytes == reused                       # same audio bytes


def test_split_action_without_documentary_is_honest(isolated):
    from actions.video_creator import video_creator
    answer = video_creator({"action": "split", "count": 3})
    assert "could not find a completed documentary" in answer.lower()


def test_create_shorts_queues_pipeline(isolated):
    import re
    from actions.video_creator import video_creator
    params = {"action": "create", "topic": "what is bitcoin",
              "request": "a 45-second Short about Bitcoin"}
    first = video_creator(dict(params))
    token = re.search(r"token ([0-9a-f]{16})", first)
    assert token, first
    answer = video_creator({**params, "approve_token": token.group(1),
                            "method": "CANVAS"})
    assert "Short queued" in answer and "1080x1920" in answer  # 9:16 = 1080x1920
    rows = RepoJobStore().list(limit=5, kind="video")
    spec = json.loads(rows[0]["params"])["spec"]
    assert spec["pipeline"] == "shorts" and spec["aspect_ratio"] == "9:16"


def test_stages_text_mentions_shorts(isolated):
    from actions.video_creator import video_creator
    text = video_creator({"action": "stages"})
    assert "shorts" in text.lower()
