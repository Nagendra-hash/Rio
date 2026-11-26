"""
orchestrator/agents/youtube/toolkit.py — one import surface over the tools.

Wraps the vendored tools (see ``tools/ATTRIBUTION.md``) in structured,
never-raising functions the agents call, and adds the deterministic helpers the
pipeline falls back to when no model is available (so every stage still
produces an honest, inspectable artifact — never a fake one):

    tools        validate_metadata / build_provenance / readiness / learning /
                 retention / audit
    planning     topics_for / fallback_script / fallback_seo / seo_score
    production   scenes_from_script / ffmpeg_command / probe_* (readiness)

Every helper that can fail returns a dict with ``ok`` rather than raising.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from orchestrator.agents.youtube import policy, schema
from orchestrator.agents.youtube.tools import (
    discoverability,
    learning,
    metadata,
    provenance,
    readiness,
    retention,
)


# ── upload metadata ──────────────────────────────────────────────────────────
def validate_metadata(seo: dict | schema.SEO | None) -> dict:
    data = seo.to_metadata() if isinstance(seo, schema.SEO) else (seo or {})
    try:
        return {"ok": True, **metadata.validate(data)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ── provenance ───────────────────────────────────────────────────────────────
def build_provenance(
    sources: list | None, claims: list | None, contains_synthetic_media: bool = False
) -> dict:
    try:
        return {
            "ok": True,
            **provenance.build(
                {
                    "sources": sources or [],
                    "claims": claims or [],
                    "containsSyntheticMedia": contains_synthetic_media,
                }
            ),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "status": "blocked",
        }


# ── readiness ────────────────────────────────────────────────────────────────
def run_readiness(probes: dict | None = None, **kwargs) -> dict:
    try:
        return {"ok": True, **readiness.run(probes or {}, **kwargs)}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "status": "failed",
            "blockingFailures": ["readiness"],
            "checks": [],
        }


def probe_text(llm) -> dict:
    """A live text-provider probe (bounded), else a warning."""
    if llm is None:
        return {
            "status": "warning",
            "message": "No model wired; text " "provider unverified.",
        }
    try:
        out = llm.text("Reply with exactly READY.")
        if out and "READY" in out.upper():
            return {
                "message": f"Model returned a live response "
                f"({getattr(llm, 'name', 'router')})."
            }
        return {
            "status": "warning",
            "message": "Model returned no usable text " "for the probe.",
        }
    except Exception as exc:
        return {
            "status": "warning",
            "message": f"Text probe failed: {policy.redact(str(exc))[:160]}",
        }


def probe_ffmpeg() -> dict:
    exe = ffmpeg_path()
    if not exe:
        return {
            "status": "warning",
            "message": "ffmpeg is not installed; " "video assembly cannot run here.",
        }
    return {"message": f"ffmpeg found at {exe}."}


def probe_youtube() -> dict:
    """Is a YouTube credential available for a real upload?"""
    creds = youtube_credentials()
    if not creds.get("configured"):
        return {
            "status": "warning",
            "message": "No YouTube credentials "
            f"configured ({creds.get('detail', 'none found')}). Upload is "
            "planned only.",
        }
    return {"message": f"YouTube credentials present ({creds.get('detail')})."}


def probe_narration(bundle: dict | None) -> dict:
    bundle = bundle or {}
    audio = bundle.get("audio") or {}
    if bundle.get("intentionalSilence"):
        return {"message": "Intentional silence recorded."}
    if audio.get("path") and not audio.get("simulated"):
        return {"message": f"Narration present: {audio.get('path')}."}
    return {
        "status": "warning",
        "message": "No real narration produced "
        "(no TTS provider configured) — intentional silence or a real TTS "
        "render is required before publishing.",
    }


def probe_metadata(seo: dict | None) -> dict:
    result = validate_metadata(seo)
    if not result.get("ok"):
        return {"status": "failed", "message": result.get("error", "invalid")}
    if result.get("valid"):
        warns = result.get("warnings") or []
        return {
            "status": "warning" if warns else "passed",
            "message": "Upload metadata valid"
            + (f" with {len(warns)} warning(s)" if warns else "."),
            "details": {"warnings": warns},
        }
    return {
        "status": "failed",
        "message": "Upload metadata invalid: " + " ".join(result.get("errors") or []),
        "details": {"errors": result.get("errors")},
    }


# ── the closed loop ──────────────────────────────────────────────────────────
def capture_learning(
    window: str, report: dict, context: dict, prior: list[dict] | None = None
) -> dict:
    try:
        return {"ok": True, **learning.capture(window, report, context, prior)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def capture_retention(
    report: dict, context: dict, window: str = "rolling"
) -> dict | None:
    try:
        return retention.capture(report, context, window)
    except Exception:
        return None


def audit_discoverability(production: dict, channel: schema.Channel) -> dict:
    pkg = discoverability.content_package(production, {"channel_name": channel.name})
    try:
        return {"ok": True, **discoverability.audit(pkg)}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            **discoverability.unavailable_report(str(exc)),
        }


# ── deterministic planning (the no-model baseline) ───────────────────────────
def topics_for(
    channel: schema.Channel, count: int = 3, source_catalog: list[dict] | None = None
) -> list[schema.Topic]:
    """Evergreen topics from the operator's own pillars — never invented."""
    channels = channel.normalised()
    pillars = channels.pillars or [channels.niche or channels.name or "your topic"]
    angles = (
        "a practical walkthrough",
        "the common mistakes to avoid",
        "what to do first",
        "a beginner's starting point",
    )
    catalog = source_catalog or []
    urls = [s.get("url") for s in catalog if s.get("url")][:3]
    out: list[schema.Topic] = []
    for i in range(max(1, count)):
        pillar = pillars[i % len(pillars)]
        topic = _clean(
            f"{pillar}: a practical guide for "
            f"{channels.audience or 'your audience'}"
        )
        if policy.blocked_topic_hit(topic, channels.blocked_topics):
            continue
        out.append(
            schema.Topic(
                topic=topic,
                pillar=pillar,
                angle=f"{pillar} through the lens of "
                f"{channels.value_proposition or channels.objective or 'your audience'}",
                rationale=(
                    "Builds an evergreen topic from the operator's own "
                    "pillar when no live research signal is supplied."
                ),
                format=(
                    channels.default_format
                    if i == 0
                    else ("explainer", "tutorial", "list")[i % 3]
                ),
                length=channels.default_length,
                source_urls=urls if i == 0 else [],
            )
        )
    return out


def filter_blocked(
    topics: list[schema.Topic], blocked: list[str]
) -> tuple[list[schema.Topic], list[str]]:
    """Drop any topic that touches the operator's blocked list."""
    kept, dropped = [], []
    for t in topics:
        hit = policy.blocked_topic_hit(t.topic, blocked)
        if hit:
            dropped.append(f"{t.topic} (blocked term: {hit})")
        else:
            kept.append(t)
    return kept, dropped


def fallback_script(topic: schema.Topic, channel: schema.Channel) -> schema.Script:
    """A deterministic script skeleton — a real outline, not filler."""
    ch = channel.normalised()
    hook = (
        f"Most people get {topic.topic} wrong the first time. Here is the "
        f"short version that actually works."
    )
    sections = [
        schema.Section(
            "Why this matters",
            [
                f"{topic.topic} fits {ch.audience or 'viewers'} who want "
                f"{ch.value_proposition or 'a clear answer'}.",
                "We will cover the whole thing in order, with nothing skipped.",
            ],
            duration=60,
        ),
        schema.Section(
            "The core idea",
            [
                f"Start with the one thing that changes the outcome of "
                f"{topic.pillar or 'this topic'}.",
                "Then layer the details on top, one at a time.",
            ],
            duration=120,
        ),
        schema.Section(
            "Do this next",
            [
                "A three-step plan you can follow today.",
                "The single mistake to avoid while you do it.",
            ],
            duration=120,
        ),
    ]
    keywords = _keywords(topic.topic + " " + " ".join(ch.pillars))
    return schema.Script(
        title=_title_from(topic.topic),
        hook=hook,
        sections=sections,
        cta=ch.cta,
        tone=ch.voice,
        pacing="steady",
        keywords=keywords,
        claims=[],
        duration_estimate=sum(s.duration for s in sections) + 20,
        scene_plan=[{"label": s.title, "duration": s.duration} for s in sections],
    )


def fallback_seo(
    script: schema.Script,
    channel: schema.Channel,
    research: schema.Research | None = None,
) -> schema.SEO:
    ch = channel.normalised()
    title = script.title[:100]
    chapters = []
    cursor = 0
    for s in script.sections:
        chapters.append({"time": _mmss(cursor), "title": s.title})
        cursor += s.duration
    body = [script.hook, ""]
    for s in script.sections:
        body.append(f"- {s.title}")
    body += [
        "",
        script.cta,
        "",
        "Sources are listed in the description when a claim needs one.",
    ]
    description = "\n".join(body)[:5000]
    return schema.SEO(
        title=title,
        description=description,
        tags=_tags(title, ch, script),
        chapters=chapters,
        category_id="22",
        language="en",
        score=seo_score(title, description, _tags(title, ch, script)),
    )


def seo_score(title: str, description: str, tags: list) -> int:
    """A small, honest 0-100 discoverability score (deterministic)."""
    score = 0
    if title:
        score += 30
    if 30 <= len(title or "") <= 70:
        score += 15
    if any(w in (title or "").lower() for w in ("how", "why", "best", "guide")):
        score += 10
    if len(description or "") >= 150:
        score += 20
    if re.search(r"http", description or ""):
        score += 5
    if len(tags or []) >= 5:
        score += 20
    return min(100, score)


def scenes_from_script(
    script: schema.Script, *, image_provider: str = "", license_: str = "generated"
) -> list[schema.Scene]:
    scenes: list[schema.Scene] = []
    scenes.append(
        schema.Scene(
            id="scene_0",
            index=0,
            label="Hook",
            narration=script.hook,
            duration=5.0,
            visual={
                "kind": "title_card",
                "provider": image_provider or "slideshow",
                "license": license_,
            },
        )
    )
    for i, s in enumerate(script.sections, 1):
        scenes.append(
            schema.Scene(
                id=f"scene_{i}",
                index=i,
                label=s.title,
                narration=" ".join(s.content),
                duration=float(s.duration),
                visual={
                    "kind": "visual",
                    "provider": image_provider or "slideshow",
                    "license": license_,
                },
            )
        )
    if script.cta:
        scenes.append(
            schema.Scene(
                id=f"scene_{len(scenes)}",
                index=len(scenes),
                label="Call to action",
                narration=script.cta,
                duration=10.0,
                visual={
                    "kind": "end_card",
                    "provider": image_provider or "slideshow",
                    "license": license_,
                },
            )
        )
    return scenes


def thumbnail_concept(
    script: schema.Script, channel: schema.Channel
) -> schema.Thumbnail:
    words = [w for w in re.findall(r"[A-Za-z0-9']+", script.title)][:4]
    primary = " ".join(words).upper() or "WATCH"
    return schema.Thumbnail(
        concept=f"A bold, single-idea thumbnail for '{script.title}'.",
        composition="subject left, large text right, high contrast",
        primary_text=primary[:24],
        secondary_text=(channel.niche or channel.name or "")[:24],
        palette=["#0F172A", "#F8FAFC", "#F59E0B"],
        prompt=(
            f"YouTube thumbnail, 16:9, bold sans-serif text '{primary[:24]}', "
            f"clean high-contrast composition, no logos, no watermarks, "
            f"original art only."
        ),
    )


# ── production helpers ───────────────────────────────────────────────────────
def ffmpeg_path() -> str:
    from shutil import which
    import os

    return os.environ.get("FFMPEG_PATH") or which("ffmpeg") or ""


def ffmpeg_command(
    concat_list: str, audio: str, out: str, *, with_audio: bool = True
) -> list[str]:
    exe = ffmpeg_path() or "ffmpeg"
    cmd = [exe, "-y", "-f", "concat", "-safe", "0", "-i", concat_list]
    if with_audio and audio:
        cmd += ["-i", audio, "-c:a", "aac", "-shortest"]
    cmd += ["-vsync", "vfr", "-pix_fmt", "yuv420p", out]
    return cmd


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def youtube_credentials() -> dict:
    """Look for a YouTube OAuth client/credentials file (never prints it)."""
    import os
    import json

    candidates = [
        # The order an operator would expect: an explicit env override, then
        # this repo's shared Google token (written by scripts/youtube_auth.py),
        # then the upstream layout, then the JARVIS home.
        "config/youtube_token.json",
        os.path.expanduser("~/.config/jarvis/youtube_token.json"),
        "config/youtube_credentials.json",
        os.path.expanduser("~/.config/jarvis/youtube_credentials.json"),
        os.path.expanduser(
            "~/Desktop/youtube-automation-agent/config/" "credentials.json"
        ),
    ]
    env = os.environ.get("YOUTUBE_CREDENTIALS") or ""
    if env:
        candidates.insert(0, env)
    for path in candidates:
        if path and Path(path).is_file():
            detail = path
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if data.get("token") or data.get("refresh_token"):
                        detail = f"{path} (token present)"
                    elif data.get("installed") or data.get("web"):
                        detail = f"{path} (OAuth client only)"
            except Exception:
                pass
            return {"configured": True, "path": path, "detail": detail}
    return {"configured": False, "path": "", "detail": "no credentials file"}


def which_tts() -> str:
    """A system TTS we may use for real narration offline (best effort)."""
    for exe in ("espeak-ng", "espeak", "say"):
        if shutil.which(exe):
            return exe
    return ""


# ── misc ─────────────────────────────────────────────────────────────────────
def _title_from(topic: str) -> str:
    t = re.sub(r"\s+", " ", topic).strip()
    return (t[:1].upper() + t[1:])[:100] if t else "Untitled video"


def _keywords(text: str, limit: int = 8) -> list[str]:
    stop = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "for",
        "with",
        "your",
        "you",
        "how",
        "what",
        "is",
        "are",
        "in",
        "on",
        "it",
        "that",
        "this",
    }
    seen, out = set(), []
    for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()):
        if w in stop or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= limit:
            break
    return out


def _tags(title: str, channel: schema.Channel, script: schema.Script) -> list[str]:
    base = [w for w in _keywords(title, 6)]
    base += [w for w in script.keywords[:6]]
    base += [w for w in _keywords(channel.niche or "", 3)]
    base += ["youtube", "how to"]
    return metadata.normalize_tags(base)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _mmss(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"
