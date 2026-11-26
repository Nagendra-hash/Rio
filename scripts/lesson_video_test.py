#!/usr/bin/env python
"""
scripts/lesson_video_test.py — end-to-end runner for the RIO 3D lesson
video (narrated, lip-synced, subtitled).

    .venv/bin/python scripts/lesson_video_test.py --excerpt
    .venv/bin/python scripts/lesson_video_test.py --full
    .venv/bin/python scripts/lesson_video_test.py --smoke 6     # 6 seconds

--smoke renders only the first N seconds of the excerpt: enough to catch a
pipeline break in ~2 minutes instead of ~15.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.video_agent import lesson_content as lc          # noqa: E402
from core.video_agent import pipeline_lesson as pl        # noqa: E402

OUT_ROOT = Path.home() / "Videos" / "JARVIS"


def _clip(direction: dict, seconds: float) -> dict:
    """Truncate a direction document to the first `seconds` (smoke tests)."""
    d = json.loads(json.dumps(direction))
    d["duration"] = round(seconds, 3)
    for scene in d["scenes"]:
        if scene["t0"] >= seconds:
            scene["t1"] = seconds
            scene["shots"] = []
            scene["cues"] = []
            continue
        scene["t1"] = min(scene["t1"], seconds)
        scene["shots"] = [s for s in scene["shots"] if s["t0"] < seconds]
        for s in scene["shots"]:
            s["t1"] = min(s["t1"], seconds)
        scene["cues"] = [c for c in scene["cues"] if c["t0"] < seconds]
        for c in scene["cues"]:
            c["t1"] = min(c["t1"], seconds)
    return d


def _proof_scenes() -> list[dict]:
    """Three lines in the classroom, then three in the playground.

    Chosen so the proof covers the things a still frame cannot: the cut
    between the two sets, an expert close-up, a boy close-up, and the boy's
    OWN viseme track (his mouth must move on his line and stay shut on the
    expert's).
    """
    classroom = json.loads(json.dumps(lc.SCENES[0]))
    classroom["lines"] = classroom["lines"][:3]
    classroom["shots"] = [
        {"focus": "two", "lines": [0, 1], "aim_z": 1.10, "lens": 45},
        {"focus": "expert", "lines": [2, 2], "cov": 1.80, "aim_z": 1.26,
         "lens": 65, "side": 20},
    ]
    classroom["cues"] = [
        {"who": "expert", "action": "talk", "from": 0, "to": 2},
        {"who": "boy", "action": "idle", "from": 0, "to": 2},
        {"who": "boy", "action": "look", "from": 1, "to": 2, "yaw": 24.0},
    ]

    playground = json.loads(json.dumps(lc.SCENES[3]))
    playground["lines"] = playground["lines"][:3]
    playground["shots"] = [
        {"focus": "two", "lines": [0, 0], "aim_z": 1.05, "lens": 45},
        {"focus": "boy", "lines": [1, 1], "cov": 1.35, "aim_z": 0.66,
         "lens": 65, "side": -20},
        {"focus": "expert", "lines": [2, 2], "cov": 1.90, "aim_z": 1.15,
         "lens": 55, "side": 20},
    ]
    playground["cues"] = [
        {"who": "expert", "action": "talk", "from": 0, "to": 0},
        {"who": "boy", "action": "idle", "from": 0, "to": 0},
        {"who": "boy", "action": "talk", "from": 1, "to": 1},
        {"who": "expert", "action": "talk", "from": 2, "to": 2},
        {"who": "boy", "action": "bounce", "from": 2, "to": 2, "hops": 2},
    ]
    return [classroom, playground]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--excerpt", action="store_true",
                    help="render scenes 1-2 only (the proof render)")
    ap.add_argument("--full", action="store_true", help="render all scenes")
    ap.add_argument("--proof", action="store_true",
                    help="~30 s proof: 3 lines in the classroom then 3 in the "
                         "playground (tests the scene cut and both speakers)")
    ap.add_argument("--smoke", type=float, default=0.0,
                    help="render only the first N seconds (pipeline check)")
    ap.add_argument("--out", default="", help="output directory")
    args = ap.parse_args()

    if not (args.excerpt or args.full or args.smoke or args.proof):
        ap.error("choose --proof, --excerpt, --full or --smoke N")

    scenes = [dict(s) for s in lc.SCENES]
    if args.proof:
        scenes = _proof_scenes()
    elif args.excerpt:
        scenes = scenes[:2]
    elif args.smoke:
        scenes = scenes[:1]

    stamp = args.smoke and f"smoke_{int(args.smoke)}s" or (
        "proof" if args.proof else "excerpt" if args.excerpt else "full")
    out = Path(args.out) if args.out else OUT_ROOT / f"rio_lesson_{stamp}"

    print(f"[run] {lc.word_count()} words in the full script; "
          f"{sum(len(s['lines']) for s in scenes)} lines, "
          f"{len(scenes)} scenes → {out}")

    if args.smoke:
        # run the stages by hand so the direction can be clipped
        speech = pl.synthesize_script(scenes, out, lc.VOICES)
        tracks, proofs = pl.build_viseme_tracks(speech["lines"])
        direction = pl.build_direction(scenes, speech["lines"],
                                       speech["total_s"])
        direction = _clip(direction, args.smoke)
        (out / "direction.json").write_text(json.dumps(direction, indent=1))
        (out / "visemes.json").write_text(json.dumps({"tracks": tracks}))
        timings = [{"text": x["text"], "start": x["start"], "end": x["end"]}
                   for x in speech["lines"] if x["start"] < args.smoke]
        from core.video_agent.avatar_pipeline import build_srt
        srt = build_srt(timings, out / "lesson.srt")
        render = pl.render_lesson(out, direction, tracks)
        final = pl.assemble_lesson(Path(render["video"]),
                                   Path(speech["wav"]), srt, out,
                                   float(direction["duration"]))
        checks = pl.validate_lesson(Path(final["output"]),
                                    float(direction["duration"]))
        print(json.dumps({"output": final["output"], "checks": checks},
                         indent=1))
        return 0

    manifest = pl.run_lesson(scenes, out, voices=lc.VOICES, title=lc.TITLE)
    print(json.dumps(manifest, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
