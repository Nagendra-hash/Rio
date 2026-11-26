#!/usr/bin/env python3
"""scripts/dance_video_test.py — the 3D character animation e2e runner.

Runs the CHARACTER_ANIMATION pipeline (original music → Kokoro TTS →
Blender two-character render → assemble with subtitles) and writes:

  <out>/dance_video.mp4   the finished vertical video
  <out>/manifest.json     per-stage manifest (tools, motion proof)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.video_agent.pipeline_character_animation import (  # noqa: E402
    run_character_animation)

DEFAULT_NARRATION = ("One sunny morning, a curious little boy hears a "
                     "happy rhythm in the playground! He starts to dance. "
                     "His friend, the psychology expert, joins in. Dancing "
                     "together makes every day brighter!")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--narration", default=DEFAULT_NARRATION)
    parser.add_argument("--out", default="")
    parser.add_argument("--voice", default="af_heart")
    args = parser.parse_args()

    out_dir = args.out or str(
        Path.home() / "Videos" / "JARVIS"
        / f"dance_test_{time.strftime('%Y%m%d_%H%M%S')}")

    print(f"[e2e] output directory: {out_dir}")
    try:
        manifest = run_character_animation(args.narration, out_dir,
                                           voice=args.voice)
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
    print(f"\nMotion proof: within-shot diff="
          f"{manifest['motion_proof']['within_shot']['mean_diff']} · "
          f"shot-cut diff={manifest['motion_proof']['shot_cut']['mean_diff']}")
    print(f"Output: {manifest['outputs'][0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
