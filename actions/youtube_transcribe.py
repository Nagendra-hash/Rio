"""
youtube_transcribe.py — turn a YouTube link into a text file.

Two tiers, cheapest and most faithful first:

  1. CAPTIONS. The video's own transcript via youtube-transcript-api (already
     a dependency). Free, instant, and word-exact — but only exists when the
     uploader or YouTube provided captions, and auto-captions carry no
     punctuation.
  2. GEMINI WATCHES THE VIDEO. When no captions exist, the YouTube URL itself
     is handed to the reasoning ladder (core.gemini, SMART tier). Gemini 2.0+
     ingests public YouTube URLs natively — it hears the actual audio, so this
     also covers videos whose captions are missing or in another language.
     Slower and it costs quota, which is exactly why it is the fallback — and
     why batch mode disables it by default: a list of caption-less videos
     would quietly burn the TEXT quota everything else in JARVIS shares.

Single mode writes "Transcript — <title>.txt" to the Desktop, the path is
logged to the HUD, and a short preview is returned for JARVIS to speak.
action: summarize appends a SUMMARY section to that file.

BATCH MODE (action: batch) reads a text file of links — one per line, blank
lines and #comments ignored, junk lines skipped without failing the run —
and transcribes every video found: one file per video, a batch report, and
optionally one combined file (combine: true). A bad video (private, removed,
no captions) fails ALONE and lands in the report; a 30-video list never dies
on video 3. A 25-minute wall-clock budget stops a pathological list from
holding the action thread all day — remaining videos are reported as skipped.

BATCH DIGEST (summarize: true) adds a per-video summary: each transcript file
gets its SUMMARY section, and all summaries are collected into one
"Digest — …txt" — the file you actually read when the list was twenty
lectures and the question is "which of these are worth my time?". Summaries
ride the FAST tier; one summary failing never fails its video (the transcript
is already safe on disk, and "(summary unavailable)" marks the slot).
"""
from __future__ import annotations

import re
import statistics
import time
from datetime import datetime
from pathlib import Path

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (
        TranscriptsDisabled, NoTranscriptFound, VideoUnavailable,
    )
    _YTT_OK = True
except Exception:                                    # pragma: no cover
    _YTT_OK = False

from core import gemini

_TIMEOUT_CAPTIONS = 30      # seconds
_TIMEOUT_GEMINI = 240       # seconds — Gemini ingests the video in real time
_LINE_TARGET = 90           # chars per wrapped line
_PARA_FACTOR = 2.0          # gap > factor × median gap starts a new paragraph
_OUTPUT_DIR = Path.home() / "Desktop"
_BATCH_SLEEP_S = 1.0        # politeness pause between videos in a batch
_BATCH_BUDGET_S = 1500.0    # 25 min wall-clock cap for a whole batch

# Accepts watch, youtu.be, shorts, embed/live, and naked 11-char IDs pasted
# alone. Deliberately tolerant: the model filling this parameter has seen
# every URL shape YouTube has ever shipped.
_URL_RE = re.compile(
    r"(?:v=|/shorts/|/embed/|/live/|youtu\.be/)([A-Za-z0-9_-]{11})"
    r"|^([A-Za-z0-9_-]{11})$"
)


class _Skip(Exception):
    """One video could not be transcribed — carries the user-facing why."""


def _video_id(raw: str) -> str | None:
    m = _URL_RE.search((raw or "").strip())
    if not m:
        return None
    return m.group(1) or m.group(2)


def _extract_ids(path: Path) -> tuple[list[str], int]:
    """A text file of links → (unique video IDs in order, junk-line count).

    Order is preserved (the file is the user's intent); duplicates collapse
    to the first occurrence. Lines with no recognisable link — notes, prose,
    the odd stray word — are counted, not fatal.
    """
    seen: set[str] = set()
    ids: list[str] = []
    junk = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        vid = _video_id(line)
        if vid is None:
            junk += 1
        elif vid not in seen:
            seen.add(vid)
            ids.append(vid)
    return ids, junk


def _title_for(video_id: str) -> str:
    """Best-effort video title for the filename — falls back to the ID."""
    try:
        import requests
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}",
                    "format": "json"},
            timeout=10,
        )
        if r.ok:
            t = r.json().get("title", "").strip()
            if t:
                return re.sub(r'[\\/:*?"<>|]+', "", t)[:80]
    except Exception:
        pass
    return f"youtube_{video_id}"


def _paragraphs(segments: list[dict]) -> str:
    """Timestamped caption snippets → readable paragraphs.

    Lines break at sentence ends near _LINE_TARGET; paragraphs break where
    the caption cadence pauses (gap > _PARA_FACTOR × median gap) — that pause
    is the speaker changing topic, and it is free structure the raw dump
    throws away.
    """
    if not segments:
        return ""
    gaps = [b["start"] - a["end"]
            for a, b in zip(segments, segments[1:])]
    median_gap = statistics.median(gaps) if gaps else 0.0

    lines, cur, prev_end = [], "", None
    for seg in segments:
        if prev_end is not None and median_gap > 0 and \
                seg["start"] - prev_end > median_gap * _PARA_FACTOR and cur:
            lines.append(cur)
            lines.append("")            # blank line = paragraph break
            cur = ""
        if cur and (len(cur) >= _LINE_TARGET or
                    cur.rstrip().endswith((".", "!", "?"))):
            lines.append(cur)
            cur = seg["text"]
        else:
            cur = f"{cur} {seg['text']}".strip()
        prev_end = seg["end"]
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def _fetch_captions(video_id: str) -> tuple[str | None, str]:
    """Tier 1 — the video's own captions. Returns (transcript, lang_tag)."""
    if not _YTT_OK:
        return None, ""
    try:
        api = YouTubeTranscriptApi()
        listing = api.list(video_id)
        # Prefer a manual English transcript; auto-captions are punctuation-
        # free. Otherwise accept the first transcript in any language, since
        # a Hindi transcript beats making the user wait for a Gemini watch.
        chosen = None
        for t in listing:
            if t.language_code.startswith("en") and not t.is_generated:
                chosen = t
                break
        if chosen is None:
            for t in listing:
                if t.language_code.startswith("en"):
                    chosen = t
                    break
        if chosen is None:
            chosen = next(iter(listing), None)
        if chosen is None:
            return None, ""
        fetched = chosen.fetch()
        segs = [{"start": s.start, "end": s.start + s.duration,
                 "text": s.text.replace("\n", " ").strip()}
                for s in fetched]
        return _paragraphs(segs), chosen.language_code
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
        return None, ""
    except Exception as e:
        print(f"[Transcribe] captions fetch failed: {e}")
        return None, ""


def _gemini_watch(video_url: str) -> str | None:
    """Tier 2 — hand the URL to the reasoning ladder and let Gemini watch it.

    Tier SMART: hearing a whole video is a reasoning-sized job, and the FAST
    rungs' quality on hour-long audio is the difference between a transcript
    and a paraphrase. Returns None on total failure so the caller can say so.
    """
    prompt = (
        "Watch this YouTube video and transcribe what is said in it. "
        "Preserve the speaker's actual words and structure; do NOT summarize "
        "or editorialize. Use paragraphs. If a language other than English "
        "is spoken, transcribe in that language.\n\n"
        f"{video_url}"
    )
    try:
        return gemini.text(prompt, tier=gemini.SMART,
                           timeout_ms=_TIMEOUT_GEMINI * 1000)
    except Exception as e:
        print(f"[Transcribe] Gemini watch failed: {e}")
        return None


def _transcribe_one(vid: str, allow_gemini: bool) -> tuple[str, str]:
    """Transcribe one video → (transcript_text, source_note).

    Raises _Skip with a user-facing reason when both tiers come up empty
    (or when the only path is Gemini and the caller disabled it for batch).
    """
    url = f"https://www.youtube.com/watch?v={vid}"
    text, lang = _fetch_captions(vid)
    if text:
        return text, f"captions ({lang or 'unknown language'})"
    if not allow_gemini:
        raise _Skip("no captions and Gemini fallback disabled")
    text = _gemini_watch(url)
    if not text or not text.strip():
        raise _Skip("no captions, and Gemini could not watch it")
    return text, "Gemini watched the video"


def _summarize(text: str) -> str:
    """One transcript → its summary. Never raises: the transcript is already
    safe on disk, so a dead summary rung degrades to a marked empty slot
    instead of failing a video that succeeded."""
    try:
        summary = gemini.text(
            "Summarize this transcript in a short paragraph, then list the "
            f"key points as bullets:\n\n{text[:60000]}",
            tier=gemini.FAST, timeout_ms=60000)
    except Exception as e:
        print(f"[Transcribe] summary failed: {e}")
        summary = ""
    return summary.strip() or "(summary unavailable)"


def _summary_block(summary: str) -> str:
    return "=" * 60 + "\nSUMMARY\n" + "=" * 60 + f"\n\n{summary}\n"


def _save_transcript(vid: str, text: str, source_note: str,
                     summary: str | None = None) -> Path:
    """Write one transcript file (header + body [+ summary]) → its path.
    `summary` is PRECOMPUTED by the caller, so batch can reuse it for the
    digest without summarising twice."""
    title = _title_for(vid)
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = _OUTPUT_DIR / f"Transcript — {title}.txt"
    header = (f"Transcript — {title}\n"
              f"Source:   https://www.youtube.com/watch?v={vid}\n"
              f"Captured: {stamp}  ({source_note})\n"
              + ("Summary follows the transcript.\n" if summary else "")
              + "=" * 60 + "\n\n")
    body = text if text.endswith("\n") else text + "\n"
    if summary:
        body += "\n" + _summary_block(summary)
    out.write_text(header + body, encoding="utf-8")
    return out


def _run_batch(params: dict, player) -> str:
    """action: batch — every link in a text file, one transcript per video,
    optional per-video summaries collected into a digest."""
    raw = str(params.get("file", "")).strip()
    if not raw:
        return "Give me the path to the file with the YouTube links, sir."
    path = Path(raw).expanduser()
    if not path.exists() and not Path(raw).is_absolute():
        # A bare filename from voice use ("links.txt") — look on the Desktop.
        candidate = _OUTPUT_DIR / raw
        if candidate.exists():
            path = candidate
    if not path.exists():
        return f"I could not find that file, sir: {raw}"

    allow_gemini = bool(params.get("allow_gemini", False))
    combine = bool(params.get("combine", False))
    summarize = bool(params.get("summarize", False))

    ids, junk = _extract_ids(path)
    if not ids:
        return (f"That file has no YouTube links in it, sir — "
                f"one link per line. ({junk} line(s) skipped.)")

    player.write_log(f"[Transcribe] Batch: {len(ids)} videos "
                     f"from {path.name}") if player else None

    t0 = time.monotonic()
    stem = path.stem[:60]
    done: list[tuple[str, Path, int, str, str]] = \
        []                                   # (vid, out, chars, note, summary)
    failed: list[tuple[str, str]] = []
    combined_parts: list[str] = []
    digest_parts: list[str] = []

    for i, vid in enumerate(ids, 1):
        if time.monotonic() - t0 > _BATCH_BUDGET_S:
            failed.extend((v, "skipped — 25-minute batch budget ran out")
                          for v in ids[i - 1:])
            break
        player.write_log(f"[Transcribe] {i}/{len(ids)}: "
                         f"youtube.com/watch?v={vid}") if player else None
        try:
            text, note = _transcribe_one(vid, allow_gemini)
            title = _title_for(vid)
            summary = _summarize(text) if summarize else None
            out = _save_transcript(vid, text, note, summary=summary)
            done.append((vid, out, len(text), note, summary or ""))
            combined_parts.append(
                f"{'=' * 60}\n{title}\n"
                f"https://www.youtube.com/watch?v={vid}\n{'=' * 60}\n\n"
                + (text if text.endswith("\n") else text + "\n"))
            if summarize:
                digest_parts.append(
                    f"## {title}\n"
                    f"https://www.youtube.com/watch?v={vid}\n\n"
                    f"{summary}\n")
        except _Skip as e:
            failed.append((vid, str(e)))
        except Exception as e:
            failed.append((vid, f"{type(e).__name__}: {e}"))
        if i < len(ids):
            time.sleep(_BATCH_SLEEP_S)

    # ── Batch report — written even when everything failed ──────────────
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    report = _OUTPUT_DIR / f"Batch transcripts — {stem} — report.txt"
    lines = [f"Batch transcripts — {stamp}",
             f"Input:   {path}",
             f"Videos:  {len(ids)}  ({junk} non-link line(s) skipped)",
             f"OK: {len(done)}   Failed: {len(failed)}"
             + ("   Digest: yes" if summarize else ""),
             "=" * 60, ""]
    for vid, out, chars, note, _s in done:
        lines.append(f"OK  {vid}  {chars:>7,} chars  ({note})")
        lines.append(f"    → {out.name}")
    for vid, why in failed:
        lines.append(f"✗  {vid} — {why}")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ── Optional combined file ───────────────────────────────────────────
    extras: list[str] = []
    if combine and combined_parts:
        allfile = _OUTPUT_DIR / f"Transcripts — {stem} — all.txt"
        allfile.write_text(
            f"All transcripts from {path.name} — {stamp}\n\n"
            + "\n\n".join(combined_parts), encoding="utf-8")
        extras.append(f"a combined file “{allfile.name}”")

    # ── Digest: every summary in one readable file ───────────────────────
    if summarize and digest_parts:
        digest = _OUTPUT_DIR / f"Digest — {stem}.txt"
        digest.write_text(
            f"Digest — {len(done)} videos from {path.name} — {stamp}\n"
            + "=" * 60 + "\n\n" + ("\n\n" + "-" * 60 + "\n\n").join(digest_parts)
            + ("\n" + "-" * 60 + "\nNot transcribed:\n"
               + "\n".join(f"  {vid} — {why}" for vid, why in failed)
               if failed else ""),
            encoding="utf-8")
        extras.append(f"the digest “{digest.name}”")

    took = time.monotonic() - t0
    print(f"[Transcribe] batch: {len(done)} ok, {len(failed)} failed "
          f"({took:.0f}s) → {report.name}")

    spoken = (f"Batch complete, sir: {len(done)} of {len(ids)} videos "
              f"transcribed in {took:.0f} seconds. ")
    if junk:
        spoken += f"{junk} line(s) had no link and were skipped. "
    if failed:
        first = failed[0]
        spoken += (f"{len(failed)} failed — for example {first[0]}: "
                   f"{first[1]}. ")
    if extras:
        spoken += ("Also on your Desktop: " + " and ".join(extras) + ". ")
    spoken += (f"Transcripts are on your Desktop, "
               f"with the full report in “{report.name}”.")
    return spoken


def youtube_transcribe(parameters: dict, player=None, **_) -> str:
    params = parameters or {}
    action = str(params.get("action", "transcribe")).lower().strip()

    if action == "batch":
        return _run_batch(params, player)

    source = str(params.get("url", "")).strip()
    want_summary = action == "summarize"

    vid = _video_id(source)
    if not vid:
        return ("Give me the YouTube link, sir — I could not find a video "
                "ID in that.")

    player.write_log(f"[Transcribe] Video: {vid}") if player else None
    url = f"https://www.youtube.com/watch?v={vid}"

    # ── Tier 1: captions ─────────────────────────────────────────────────
    t0 = time.monotonic()
    try:
        text, note = _transcribe_one(vid, allow_gemini=True)
    except _Skip as e:
        return f"I could not transcribe that video, sir — {e}."
    # (Single mode keeps allow_gemini on: for ONE video the wait is the
    # user's explicit ask, and silently refusing would feel broken.)

    # ── Save ─────────────────────────────────────────────────────────────
    summary = _summarize(text) if want_summary else None
    out = _save_transcript(vid, text, note, summary=summary)
    took = time.monotonic() - t0
    print(f"[Transcribe] saved {out.name} ({len(text):,} chars, "
          f"{took:.1f}s, {note})")
    preview = " ".join(text.split())[:200]
    more = "…" if len(text) > 200 else ""
    tail = (" A summary is at the bottom of the file."
            if summary else "")
    return (f"Transcript saved to your Desktop as “{out.name}”, sir. "
            f"It begins: “{preview}{more}”{tail}")


TOOL = {
    "name": "youtube_transcribe",
    "description": (
        "Convert YouTube video(s) into text transcript files saved to the "
        "Desktop. Single: uses the video's own captions when available, "
        "otherwise Gemini watches the video directly; action=summarize "
        "appends a summary. Batch: transcribe EVERY YouTube link found in a "
        "text file in one command, with a per-video report, optional "
        "combined file, and optional per-video summaries collected into a "
        "digest. Use for 'transcribe this video', 'convert this YouTube "
        "link to text', or 'transcribe all the links in this file'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "url": {
                "type": "STRING",
                "description": "The YouTube video link (watch, youtu.be, or "
                               "shorts URL). Not used for batch."
            },
            "file": {
                "type": "STRING",
                "description": "Path to a text file containing YouTube links, "
                               "one per line — required for action=batch. A "
                               "bare filename is also looked up on the "
                               "Desktop."
            },
            "action": {
                "type": "STRING",
                "description": "transcribe (default) | summarize (single "
                               "video, appends a summary) | batch (all links "
                               "in the file parameter)."
            },
            "allow_gemini": {
                "type": "BOOLEAN",
                "description": "batch only: let Gemini watch caption-less "
                               "videos. Slower and uses shared quota — "
                               "default false, captions-only."
            },
            "combine": {
                "type": "BOOLEAN",
                "description": "batch only: additionally write all "
                               "transcripts into one combined file. Default "
                               "false."
            },
            "summarize": {
                "type": "BOOLEAN",
                "description": "batch only: also produce a per-video summary "
                               "(appended to each transcript) collected into "
                               "one digest file. Default false."
            }
        },
        "required": []
    },
    "handler": youtube_transcribe,
}
