#!/usr/bin/env python
"""scripts/benchmark_encoders.py — Phase 6 validation.

Encodes the SAME synthetic 1080x1920 @ 30 fps source (video + audio) with
Intel VAAPI H.264 and CPU libx264 at MATCHED quality targets (same bitrate
ladder, same AAC audio), then measures speed, size, PSNR/SSIM against the
source and playback integrity (duration, streams, decode errors, A/V drift).

No CUDA, no NVENC, no paid APIs. Results → memory/vaapi_benchmark.json.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.video_agent import encoder as enc          # noqa: E402

W, H, FPS, DUR = 1080, 1920, 30, 12
BITRATE, MAXRATE, BUFSIZE = "5M", "7M", "10M"
WORK = Path("/tmp/opencode/encoder_bench")
REF = WORK / "source.mp4"


def sh(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in args], capture_output=True,
                          text=True, timeout=timeout)


def make_source() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i",
            f"testsrc2=size={W}x{H}:rate={FPS}:duration={DUR}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={DUR}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-ar", "44100", "-shortest", str(REF)]
    proc = sh(args)
    if proc.returncode != 0 or not REF.is_file():
        raise SystemExit(f"source generation failed: {proc.stderr[-300:]}")


def encode_with(backend: str) -> dict:
    chosen = enc.select_encoder("vaapi" if backend == "vaapi" else "libx264")
    if chosen.backend != backend:
        return {"backend": backend, "ok": False,
                "error": f"requested {backend}, selection returned "
                         f"{chosen.backend}: {chosen.reason}"}
    out = WORK / f"out_{backend}.mp4"
    base = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},fps={FPS}")

    def build(e):
        args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                *e.input_args(), "-i", str(REF),
                "-filter_complex", e.upload_filter(base) + "[v]",
                "-map", "[v]", "-map", "0:a"]
        if e.hardware:
            args += ["-c:v", "h264_vaapi", "-profile:v", "high",
                     "-b:v", BITRATE, "-maxrate", MAXRATE,
                     "-bufsize", BUFSIZE]
        else:
            args += ["-c:v", "libx264", "-preset", "veryfast",
                     "-b:v", BITRATE, "-maxrate", MAXRATE,
                     "-bufsize", BUFSIZE, "-threads", "4"]
        args += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                 "-movflags", "+faststart", str(out)]
        return args, e.env()

    result = enc.run_encode(build, cwd=WORK, log=lambda _m: None,
                            what=f"benchmark {backend}", prefer=backend)
    return {"backend": backend, "ok": True, "output": str(out),
            "elapsed_s": result["elapsed_s"],
            "fell_back": result["fell_back"]}


def analyze(row: dict) -> dict:
    out = Path(row["output"])
    probe = json.loads(sh(["ffprobe", "-v", "quiet", "-print_format", "json",
                           "-show_format", "-show_streams",
                           str(out)]).stdout or "{}")
    fmt = probe.get("format") or {}
    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    row["size_mb"] = round(int(fmt.get("size") or 0) / 1048576, 2)
    row["duration_s"] = round(float(fmt.get("duration") or 0), 2)
    row["video_codec"] = video.get("codec_name")
    row["audio_codec"] = audio.get("codec_name")
    row["resolution"] = f"{video.get('width')}x{video.get('height')}"
    row["fps"] = video.get("r_frame_rate")
    # speed: frames / elapsed
    frames = int(video.get("nb_frames") or 0) or int(DUR * FPS)
    if row.get("elapsed_s"):
        row["encode_fps"] = round(frames / row["elapsed_s"], 1)
    # integrity: full decode, count errors
    dec = sh(["ffmpeg", "-v", "error", "-xerror", "-i", str(out),
              "-f", "null", "-"], timeout=300)
    row["decode_errors"] = dec.stderr.count("Error") if dec.returncode else 0
    # A/V drift: duration difference between the two streams
    try:
        v_dur = float(video.get("duration") or 0)
        a_dur = float(audio.get("duration") or 0)
        row["av_drift_s"] = round(abs(v_dur - a_dur), 3) if v_dur and a_dur \
            else None
    except (TypeError, ValueError):
        row["av_drift_s"] = None
    # quality vs source (PSNR / SSIM), decoded frames only — CPU metrics,
    # honestly labelled: the METRIC runs on CPU for both encoders alike
    for name in ("ssim", "psnr"):
        proc = sh(["ffmpeg", "-hide_banner", "-i", str(out), "-i", str(REF),
                   "-lavfi", f"[0:v][1:v]{name}", "-f", "null", "-"],
                  timeout=300)
        for line in (proc.stderr or "").splitlines():
            if f"Parsed_{name}" in line:
                if name == "psnr" and "average:" in line:
                    row[name] = line.split("average:")[1].strip() \
                        .split(" ")[0]
                    break
                if name == "ssim" and "All:" in line:
                    row[name] = line.split("All:")[1].strip() \
                        .split(" ")[0].rstrip("]") or \
                        line.split("All:")[1].strip().split(" ")[0]
                    break
    return row


def main() -> int:
    print(f"[bench] source: synthetic {W}x{H} @ {FPS} fps, {DUR}s, "
          f"bitrate ladder {BITRATE}/{MAXRATE} (matched)")
    make_source()
    print(f"[bench] source ok: {REF.stat().st_size / 1048576:.1f} MB")
    results = []
    for backend in ("vaapi", "libx264"):
        print(f"[bench] encoding with {backend}…")
        row = encode_with(backend)
        if not row["ok"]:
            print(f"[bench] {backend}: FAILED — {row['error']}")
            results.append(row)
            continue
        results.append(analyze(row))
        r = results[-1]
        print(f"[bench] {backend}: {r['encode_fps']} fps encode, "
              f"{r['size_mb']} MB, {r['duration_s']}s, "
              f"PSNR {r.get('psnr')}, SSIM {r.get('ssim')}, "
              f"decode_errors {r['decode_errors']}, "
              f"A/V drift {r.get('av_drift_s')}s")
    status = enc.probe_vaapi(force=True)
    payload = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {"resolution": f"{W}x{H}", "fps": FPS, "duration_s": DUR,
                   "bitrate": BITRATE, "maxrate": MAXRATE},
        "vaapi_probe": {"available": status["available"],
                        "device": status["device"],
                        "driver": status["driver"],
                        "detail": status["detail"]},
        "results": results,
    }
    out = Path(__file__).resolve().parents[1] / "memory" / \
        "vaapi_benchmark.json"
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"[bench] saved → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
