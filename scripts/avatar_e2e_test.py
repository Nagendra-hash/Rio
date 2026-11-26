#!/usr/bin/env python3
"""scripts/avatar_e2e_test.py — the 3D avatar end-to-end test runner.

Runs the isolated avatar pipeline (Kokoro local TTS → Blender 3D render →
ffmpeg assembly with burned subtitles) and writes:

  <out>/avatar_video.mp4   the finished vertical video
  <out>/manifest.json      per-stage manifest (tools, timings, proof)
  AVATAR_E2E_TEST_REPORT.md  the test report (tools actually used, proof)

Fully offline after the (user-approved) Kokoro model download.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.video_agent.avatar_pipeline import run_avatar_e2e  # noqa: E402

DEFAULT_NARRATION = ("Hello! I am RIO, your personal AI assistant. "
                     "I can help you organize tasks, explore ideas, and "
                     "create videos. This is my first animated avatar test.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--narration", default=DEFAULT_NARRATION)
    parser.add_argument("--out", default="")
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--music", default="")
    args = parser.parse_args()

    out_dir = args.out or str(
        Path.home() / "Videos" / "JARVIS"
        / f"avatar_test_{time.strftime('%Y%m%d_%H%M%S')}")

    print(f"[e2e] output directory: {out_dir}")
    print(f"[e2e] narration: {args.narration[:80]}…")
    try:
        manifest = run_avatar_e2e(args.narration, out_dir,
                                  voice=args.voice,
                                  music_file=args.music or None)
    except Exception as exc:                        # noqa: BLE001
        print(f"[e2e] FAILED: {type(exc).__name__}: {str(exc)[:400]}")
        return 1

    print("\n=== STAGE RESULTS ===")
    for stage in manifest["stages"]:
        line = (f"  {stage['stage']:<12} {stage['status']:<8} "
                f"{stage.get('seconds', 0):>6.1f}s")
        if stage["status"] != "OK":
            line += f"  — {stage.get('error', '')[:120]}"
        print(line)
    print(f"\nTools actually used:")
    for tool, detail in manifest["tools"].items():
        print(f"  {tool:<10} {detail}")
    print(f"\nOutput: {manifest['outputs'][0]}")
    print(f"Dimensions: {manifest['dimensions']['width']}x"
          f"{manifest['dimensions']['height']} · "
          f"{manifest['duration_s']}s · "
          f"animation proof: mean_diff="
          f"{manifest['animation_proof']['mean_diff']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
