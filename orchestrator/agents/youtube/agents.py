"""
orchestrator/agents/youtube/agents.py — the six stages of the pack.

Each stage is a small, independently callable agent with the same shape as the
replica pack's: it takes the project, the model, and its options, writes its
artifact under ``youtube/``, and returns ``{"stage", "ok", "note", **extras}``.
None of them raises to the caller: a failure is captured in the result and in
``state.json`` — never a fake success.

    research    niche + audience + blocked topics → an editorial plan, with
                evidence the operator supplied (never invented trends)
    script      hook, narrative, CTA, claims, metadata (title/desc/tags/chapters)
                and a thumbnail concept
    production  narration + visuals + a real assembly, honest about "simulated"
    review      readiness + provenance + an advisory SEO audit → gates, plus a
                local Review Studio page
    publish     schedule + (optionally) upload — ONLY with human approval
    learn       capture 24h/7d evidence, measure the KPI and economics, and
                propose the next move (pending until approved)

Every prompt carries ``policy``'s rules, so no stage is asked to invent a
statistic, a source, a quote or a metric.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.agents.youtube import policy, schema, toolkit
from orchestrator.agents.youtube.llm import extract_json, extract_json_array


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── RESEARCH ─────────────────────────────────────────────────────────────────
RESEARCH_SYSTEM = (
    "You are the research lead for a YouTube channel. You turn the channel's "
    "own strategy and the evidence the operator supplied into a focused "
    "editorial plan. You never invent trend data, statistics, sources or URLs. "
    "Answer with ONE JSON object only.\n" + policy.policy_banner())
RESEARCH_PROMPT = """Turn this channel strategy and the supplied evidence into a
content plan.

Objective: {objective}
Audience: {audience}
Value proposition: {value_proposition}
Content pillars: {pillars}
Preferred format: {fmt}   Preferred length: {length}
Success metric (KPI): {kpi}  Target: {target} per {window} days
Constraints: {constraints}
Blocked topics (never include these): {blocked}
Recent topics to avoid repeating: {recent}
Operator-approved learnings to apply (only these): {learnings}
Supplied evidence (use ONLY these; do not add): {signals}
Allowed source catalog (cite ONLY these exact URLs): {catalog}

Return ONE JSON object with exactly {count} items:
{{"topics": [{{"topic": "specific video topic",
               "pillar": "one exact pillar above",
               "angle": "a distinct audience-relevant angle",
               "rationale": "why this advances the objective, using the supplied evidence",
               "format": "explainer|tutorial|list|review|story",
               "length": "short|medium|long",
               "source_urls": ["exact URL from the catalog, or empty"]}}]}}

If the supplied evidence is weak, prefer evergreen topics built from the
pillars. Never reference a source that is not in the catalog. Never include a
blocked topic. Apply only the supplied approved learnings.
"""


class ResearchStep:
    name = "research"
    role = "find topics and build the editorial plan"

    def run(self, project, llm, *, channel: schema.Channel | dict | None = None,
            topic: str = "", count: int = 3,
            research_signals: list | None = None,
            source_catalog: list | None = None,
            approved_learnings: list | None = None,
            recent_topics: list | None = None, **_) -> dict:
        project.ensure()
        ch = _as_channel(channel) or project.load_channel()
        if channel is not None:
            project.save_channel(ch)
        count = max(1, min(5, int(count or 3)))

        # Approved learnings come from the learn stage, and only when approved.
        if approved_learnings is None:
            approved_learnings = [r for r in project.load_recommendations()
                                  if (r.get("status") or "").lower() == "approved"]
        if recent_topics is None:
            recent_topics = _recent_topics(project)

        signals = _clean_signals(research_signals)
        catalog = _clean_catalog(source_catalog)
        used_llm = False

        topics = toolkit.topics_for(ch, count, catalog)
        if topic.strip():
            topics = [schema.Topic(topic=topic.strip(),
                                   pillar=(ch.pillars or [""])[0],
                                   angle=f"For {ch.audience or 'the audience'}",
                                   rationale="Requested by the operator.",
                                   format=ch.default_format, length=ch.default_length)]

        if llm is not None:
            raw = llm.text(RESEARCH_PROMPT.format(
                objective=ch.objective or "—", audience=ch.audience or "—",
                value_proposition=ch.value_proposition or "—",
                pillars=", ".join(ch.pillars) or "—", fmt=ch.default_format,
                length=ch.length_hint or ch.default_length, kpi=ch.kpi,
                target=ch.target_value or "—", window=ch.target_window_days,
                constraints=ch.constraints or "none",
                blocked=", ".join(ch.blocked_topics) or "none",
                recent=json.dumps(recent_topics), learnings=json.dumps(
                    [{"title": r.get("title"), "rationale": r.get("rationale")}
                     for r in approved_learnings]),
                signals=json.dumps(signals), catalog=json.dumps(catalog),
                count=count), system=RESEARCH_SYSTEM)
            data = extract_json(raw or "")
            items = extract_json_array(raw or "")
            if isinstance(data, dict) and isinstance(data.get("topics"), list):
                items = data["topics"]
            if items:
                parsed = _normalize_topics(items, ch, catalog, count)
                if parsed:
                    topics = parsed
                    used_llm = True

        topics, dropped = toolkit.filter_blocked(topics, ch.blocked_topics)
        if not topics:
            topics = toolkit.topics_for(ch, count, catalog)
            topics, dropped2 = toolkit.filter_blocked(topics, ch.blocked_topics)
            dropped += dropped2
        topics = topics[:count]

        research = schema.Research(
            generated_at=_now(),
            sources=([f"{len(signals)} supplied signal(s)"] if signals else
                     ["No live research signals supplied — evergreen plan built "
                      "from your own pillars (nothing invented)"]),
            source_catalog=catalog, notes="",
            approved_learnings=[{"title": r.get("title"),
                                 "rationale": r.get("rationale")}
                                for r in approved_learnings],
            topics=topics)
        if dropped:
            research.notes = "Dropped blocked topic(s): " + "; ".join(dropped)
        project.save_research(research, ch)
        recent = list(project.state.get("recent_topics") or [])
        recent += [t.topic for t in topics]
        project.state["recent_topics"] = recent[-50:]
        project.mark(self.name, bool(topics),
                     f"{len(topics)} topic(s)" + ("" if used_llm
                                                  else " (deterministic)"))
        return {"stage": self.name, "ok": bool(topics), "research": research,
                "note": f"{len(topics)} topic(s) planned"
                        + ("" if used_llm else " without a model — built from "
                                               "your pillars"),
                "dropped": dropped, "used_llm": used_llm}


def _normalize_topics(items: list, ch: schema.Channel,
                      catalog: list[dict], count: int) -> list[schema.Topic]:
    allowed = {s["url"] for s in catalog}
    seen: set[str] = set()
    out: list[schema.Topic] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("topic") or "").strip()[:200]
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        fmt = str(item.get("format") or ch.default_format).lower()
        if fmt not in schema.FORMATS:
            fmt = ch.default_format
        length = str(item.get("length") or ch.default_length).lower()
        if length not in schema.LENGTHS:
            length = ch.default_length
        urls = [u for u in (item.get("source_urls") or []) if u in allowed]
        out.append(schema.Topic(
            topic=text, pillar=str(item.get("pillar") or "")[:80],
            angle=str(item.get("angle") or "")[:300],
            rationale=str(item.get("rationale") or "")[:400],
            format=fmt, length=length, source_urls=urls))
        if len(out) >= count:
            break
    return out


# ── SCRIPT ───────────────────────────────────────────────────────────────────
SCRIPT_SYSTEM = (
    "You are writing a YouTube script plan. You write the hook, the sections "
    "and the CTA, and you list every externally verifiable factual claim so a "
    "reviewer can check it. You never invent statistics, quotes or sources. "
    "Answer with ONE JSON object only.\n" + policy.policy_banner())
SCRIPT_PROMPT = """Write a YouTube script plan.

Topic: {topic}
Pillar: {pillar}
Angle: {angle}
Format: {fmt}   Desired length: {length}
Audience: {audience}
Tone / brand voice: {voice}
Channel goal: {objective}
Preferred call to action: {cta}
Keywords: {keywords}
Research sources (cite ONLY these exact URLs): {sources}

Return ONE JSON object:
{{"title": "compelling title under 100 characters",
  "hook": "opening hook in one sentence",
  "sections": [{{"title": "section title",
                 "content": ["spoken line"],
                 "duration": 60}}],
  "cta": "clear call to action",
  "claims": [{{"text": "a specific factual claim a reviewer must verify",
               "riskLevel": "standard|high",
               "sourceUrls": ["exact URL from the sources above, or empty"]}}]}}

Avoid fabricated statistics, unsupported claims and fake urgency. Use only the
exact URLs above; leave sourceUrls empty when the sources do not support a
claim. Aim for 3-6 sections.
"""


class ScriptStep:
    name = "script"
    role = "write the hook, narrative, CTA, metadata and claims"

    def run(self, project, llm, *, topic: str = "", topic_index: int = 0,
            **_) -> dict:
        project.ensure()
        ch = project.load_channel()
        research = project.load_research()
        chosen = _pick_topic(research, topic, topic_index)

        script = toolkit.fallback_script(chosen, ch)
        used_llm = False
        if llm is not None:
            raw = llm.text(SCRIPT_PROMPT.format(
                topic=chosen.topic, pillar=chosen.pillar or "—",
                angle=chosen.angle or "—", fmt=chosen.format,
                length=ch.length_hint or chosen.length,
                audience=ch.audience or "—", voice=ch.voice or "—",
                objective=ch.objective or "—", cta=ch.cta or "—",
                keywords=", ".join(toolkit._keywords(chosen.topic)),
                sources=json.dumps(research.source_catalog
                                   or [{"url": u} for u in chosen.source_urls])),
                system=SCRIPT_SYSTEM)
            data = extract_json(raw or "")
            if data and data.get("title") and data.get("hook"):
                script = _script_from_model(data, ch, chosen, research)
                used_llm = True

        seo = toolkit.fallback_seo(script, ch, research)
        thumbnail = toolkit.thumbnail_concept(script, ch)
        validation = toolkit.validate_metadata(seo)

        project.save_script(script, seo, thumbnail)
        project.mark(self.name, bool(script.title and script.hook),
                     f"{len(script.sections)} sections, "
                     f"{len(script.claims)} claim(s)"
                     + ("" if used_llm else " (deterministic)"))
        return {"stage": self.name, "ok": bool(script.title and script.hook),
                "script": script, "seo": seo, "thumbnail": thumbnail,
                "validation": validation,
                "note": f"script + metadata written "
                        f"({len(script.sections)} sections, "
                        f"metadata {'valid' if validation.get('valid') else 'INVALID'})"
                        + ("" if used_llm else "; no model — outline baseline"),
                "used_llm": used_llm}


def _script_from_model(data: dict, ch: schema.Channel, topic: schema.Topic,
                       research: schema.Research) -> schema.Script:
    allowed = {s.get("url") for s in research.source_catalog if s.get("url")}
    sections: list[schema.Section] = []
    for s in (data.get("sections") or [])[:8]:
        if not isinstance(s, dict):
            continue
        content = s.get("content")
        if isinstance(content, str):
            content = [content]
        content = [str(c) for c in (content or []) if str(c).strip()]
        if not content:
            continue
        sections.append(schema.Section(
            title=str(s.get("title") or f"Section {len(sections) + 1}")[:120],
            content=content, duration=int(s.get("duration") or 60)))
    if not sections:
        sections = toolkit.fallback_script(topic, ch).sections

    claims: list[schema.Claim] = []
    for c in (data.get("claims") or [])[:20]:
        if not isinstance(c, dict):
            continue
        text = str(c.get("text") or c.get("claim") or "").strip()
        if not text:
            continue
        urls = [u for u in (c.get("sourceUrls") or []) if u in allowed] \
            if allowed else []
        claims.append(schema.Claim(
            text=text[:600],
            risk_level=("high" if str(c.get("riskLevel")).lower() == "high"
                        else "standard"),
            source_urls=urls))

    return schema.Script(
        title=str(data.get("title") or topic.topic)[:100],
        hook=str(data.get("hook") or "").strip()[:400],
        sections=sections, cta=str(data.get("cta") or ch.cta)[:300],
        tone=ch.voice, pacing="steady",
        keywords=toolkit._keywords(topic.topic + " " + " ".join(ch.pillars)),
        claims=claims,
        duration_estimate=sum(s.duration for s in sections) + 20,
        scene_plan=[{"label": s.title, "duration": s.duration}
                    for s in sections])


# ── PRODUCTION ───────────────────────────────────────────────────────────────
class ProductionStep:
    name = "production"
    role = "generate narration and visuals and assemble a real MP4"

    def run(self, project, llm, *, render: bool = False, voice: str = "",
            provider: str = "slideshow", narration_text: str = "",
            narration_file: str = "", tts=None, **_) -> dict:
        project.ensure()
        script = project.load_script()
        scenes = toolkit.scenes_from_script(script, image_provider=provider)
        provider = provider or "slideshow"

        assets: list[schema.Asset] = []
        for s in scenes:
            assets.append(schema.Asset(kind="visual", path="", provider=provider,
                                       license="generated", generated=True,
                                       simulated=not render,
                                       notes=s.label))

        audio: dict = {}
        narration = narration_text or script.full_text()
        # Narration priority: your own file > one already in media/ > the
        # injected synthesizer (JARVIS's TTS) > a system TTS > an honest
        # placeholder. A real file is real narration; a placeholder never is.
        if render:
            provided = _existing_file(narration_file, project) \
                or _existing_media_narration(project)
            if provided:
                audio = {"path": str(provided), "provider": "provided",
                         "license": "generated", "simulated": False}
        if render and not audio and narration and tts is not None:
            dest = project.media_dir / "narration.wav"
            if _synth_call(tts, narration, dest):
                audio = {"path": str(dest), "provider": "jarvis-tts",
                         "license": "generated", "simulated": False}
        if render and not audio and narration and toolkit.which_tts():
            dest = project.media_dir / "narration.wav"
            if _synthesize(narration, dest, voice=voice):
                audio = {"path": str(dest), "provider": toolkit.which_tts(),
                         "license": "generated", "simulated": False}
        if not audio:
            audio = {"path": "", "provider": "", "license": "generated",
                     "simulated": True,
                     "notes": "No narration was produced — this is a "
                              "placeholder. Record it, pass a narration file, "
                              "or record intentional silence explicitly."}
        intentional_silence = bool(audio.get("simulated")) and bool(
            narration_text == "__silent__")

        total = sum(s.duration for s in scenes) or 15.0
        assembly: list[str] = []
        final: dict = {"path": "", "simulated": True, "exists": False}
        rendered = False
        if render and toolkit.ffmpeg_path():
            out = project.media_dir / "final.mp4"
            if _render_video(project, scenes, audio, out, total):
                rendered = True
                final = {"path": str(out), "provider": "ffmpeg",
                         "license": "generated", "simulated": False,
                         "exists": True}
                assembly = _last_cmd
        if not rendered:
            final = {"path": "", "produced": "slideshow", "simulated": True,
                     "exists": False,
                     "notes": ("Plan only (render=False) or no ffmpeg/TTS "
                               "available in this environment. A simulated "
                               "result may NOT be published.")}
        if audio.get("path"):
            assets.append(schema.Asset(kind="audio", path=audio["path"],
                                       provider=audio.get("provider", ""),
                                       license="generated", generated=True,
                                       simulated=bool(audio.get("simulated"))))
        if final.get("path"):
            assets.append(schema.Asset(kind="video", path=final["path"],
                                       provider=final.get("provider", ""),
                                       license="generated", generated=True))

        bundle = schema.ProductionBundle(
            id=f"prod_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            title=script.title, scenes=scenes, assets=assets, audio=audio,
            final_video=final, assembly_cmd=assembly,
            contains_synthetic_media=bool(provider != "slideshow"
                                          or audio.get("provider")),
            intentional_silence=intentional_silence,
            costs={"amount": 0.0, "currency": "USD", "complete": True,
                   "providers": [provider] if provider else [],
                   "notes": "Local slideshow + system TTS cost nothing."},
            provider=provider, rendered=rendered)
        project.save_production(bundle)
        project.mark(self.name, True,
                     f"{len(scenes)} scenes, rendered={rendered}")
        return {"stage": self.name, "ok": True, "bundle": bundle,
                "rendered": rendered,
                "note": (f"{len(scenes)} scenes; "
                         + ("real MP4 at " + final["path"] if rendered
                            else "plan written (simulated — cannot publish "
                                 "until a real render exists)")),
                "simulated": not rendered}


def _synth_call(tts, text: str, out: Path) -> bool:
    """Run an injected synthesizer ``tts(text, out_path) -> bool`` safely."""
    try:
        ok = tts(text, str(out))
        return bool(ok) and out.is_file() and out.stat().st_size > 100
    except Exception:
        return False


def _existing_file(value: str | None, project) -> Path | None:
    if not value:
        return None
    p = Path(str(value)).expanduser()
    if not p.is_absolute():
        p = project.root.parent / str(value)
    return p if p.is_file() and p.stat().st_size > 100 else None


def _existing_media_narration(project) -> Path | None:
    for name in ("narration.wav", "narration.mp3", "narration.m4a",
                 "narration.aac", "narration.ogg"):
        p = project.media_dir / name
        if p.is_file() and p.stat().st_size > 100:
            return p
    return None


def _synthesize(text: str, out: Path, voice: str = "") -> bool:
    exe = toolkit.which_tts()
    if not exe:
        return False
    import subprocess
    cmd = [exe, "-w", str(out)] if exe.startswith("espeak") else [exe, "-o", str(out)]
    if voice and exe.startswith("espeak"):
        cmd = [exe, "-v", voice, "-w", str(out)]
    cmd.append((text or "")[:4000])
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        return out.is_file() and out.stat().st_size > 100
    except Exception:
        return False


_last_cmd: list[str] = []


def _render_video(project, scenes, audio: dict, out: Path, total: float) -> bool:
    import subprocess
    global _last_cmd
    exe = toolkit.ffmpeg_path()
    if not exe:
        return False
    color = "0x0F172A"
    cmd = [exe, "-y", "-f", "lavfi",
           "-i", f"color=c={color}:s=1280x720:d={total:.2f}"]
    has_audio = bool(audio.get("path")) and not audio.get("simulated")
    if has_audio:
        cmd += ["-i", str(audio["path"]), "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "25", str(out)]
    _last_cmd = cmd
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        return out.is_file() and out.stat().st_size > 1000
    except Exception:
        return False


# ── REVIEW ───────────────────────────────────────────────────────────────────
class ReviewStep:
    name = "review"
    role = "quality checks, fact ledger and the approval gate"

    def run(self, project, llm, *, approved: bool = False,
            verified_sources: list | None = None,
            sources: list | None = None, claims: list | None = None,
            waive: list | None = None, waive_note: str = "",
            probes: dict | None = None, **_) -> dict:
        project.ensure()
        ch = project.load_channel()
        research = project.load_research()
        script = project.load_script()
        seo = project.load_seo()
        bundle = project.load_production()

        # ── provenance ───────────────────────────────────────────────────
        prov_sources = sources if sources is not None else _sources_from_research(
            research, verified_sources)
        prov_claims = (list(claims) if claims is not None
                       else _claims_from_script(script, prov_sources))
        waive_note = str(waive_note or "").strip()
        waived = _apply_waivers(prov_claims, waive, waive_note)
        prov = toolkit.build_provenance(
            prov_sources, prov_claims,
            contains_synthetic_media=bool(bundle.get("containsSyntheticMedia")))

        # ── readiness ────────────────────────────────────────────────────
        live_probes = probes or {
            "text_provider": (lambda: toolkit.probe_text(llm)),
            "voice_narration": (lambda: toolkit.probe_narration(bundle)),
            "video_assembly": (lambda: toolkit.probe_ffmpeg()),
            "youtube_access": (lambda: toolkit.probe_youtube()),
            "upload_metadata": (lambda: toolkit.probe_metadata(seo.to_metadata())),
        }
        ready = toolkit.run_readiness(live_probes)

        # ── discoverability (advisory) ───────────────────────────────────
        production = {"id": bundle.get("id", ""), "script": script.to_dict(),
                      "seo": seo.to_dict(), "provenance": prov}
        audit = toolkit.audit_discoverability(production, ch)

        gates = {
            "readiness_ok": ready.get("status") != "failed",
            "provenance_ok": prov.get("status") != "blocked",
            "metadata_ok": bool(toolkit.validate_metadata(seo).get("valid")),
            "real_video": policy.real_video(bundle),
            "narration": policy.narration_present(bundle),
        }
        findings: list[str] = []
        if not gates["real_video"]:
            findings.append("No real MP4 yet — render the video before "
                            "publishing (simulated output cannot ship).")
        if not gates["narration"]:
            findings.append("Narration is a placeholder — record/config TTS or "
                            "record intentional silence.")
        if not gates["provenance_ok"]:
            findings.append("Unresolved factual claims — verify their sources "
                            "or waive them with a note.")
        if prov.get("error"):
            findings.append(f"Fact ledger rejected the input: {prov['error']}")
        if waive and not waive_note and not waived:
            findings.append("Waiving a claim requires a reviewer note — "
                            "nothing was waived.")
        if ready.get("status") == "failed":
            findings.append("Readiness failed: "
                            + ", ".join(ready.get("blockingFailures") or []))

        review = schema.ReviewReport(
            readiness=ready, provenance=prov, discoverability=audit,
            gates=gates, approved=bool(approved), findings=findings)
        project.save_review(review, prov)
        project.write(project.studio_html, _studio_html(script, seo, prov, audit,
                                                        ready, gates))
        # The stage COMPLETED (the report exists); ``ready`` is the publish
        # decision. A run that needs the human is not a failed run.
        ready_to_ship = bool(gates) and all(gates.values())
        project.mark(self.name, ready_to_ship,
                     "gates: " + ", ".join(f"{k}={'ok' if v else 'x'}"
                                           for k, v in gates.items()))
        return {"stage": self.name, "ok": True, "ready": ready_to_ship,
                "review": review, "ready_for_approval": ready_to_ship,
                "note": ("review ready" if ready_to_ship
                         else "review done — blockers need a human")
                        + (f"; {len(findings)} item(s) to resolve"
                           if findings else ""),
                "gates": gates, "findings": findings,
                "readiness_status": ready.get("status"),
                "provenance_status": prov.get("status")}


def _sources_from_research(research: schema.Research,
                           verified_sources: list | None) -> list[dict]:
    verified = {str(u) for u in (verified_sources or [])}
    out: list[dict] = []
    for src in (research.source_catalog or []):
        url = src.get("url")
        if not url:
            continue
        out.append({"url": url, "title": src.get("title") or url,
                    "status": "verified" if url in verified else "pending"})
    return out


def _claims_from_script(script: schema.Script,
                        sources: list | None = None) -> list[dict]:
    """The script's claims, marked ``supported`` when the reviewer has already
    verified a source the claim cites. Nothing is ever auto-waived."""
    verified = {s.get("url") for s in (sources or [])
                if str(s.get("status")) == "verified"}
    out: list[dict] = []
    for c in script.claims:
        urls = [u for u in (c.source_urls or []) if u]
        status = ("supported" if urls and any(u in verified for u in urls)
                  else "pending")
        out.append({"text": c.text, "riskLevel": c.risk_level,
                    "sourceUrls": urls, "status": status})
    return out


def _apply_waivers(claims: list[dict], waive: list | None,
                   note: str) -> list[str]:
    """Waive the claims a reviewer names (exact text, a substring, or ``*``).

    A waiver is only valid with a note, so an empty note waives nothing — the
    claims stay open and the review surfaces why. Returns the waived texts.
    """
    wanted = [str(w).strip() for w in (waive or []) if str(w).strip()]
    if not wanted or not note:
        return []
    done: list[str] = []
    for c in claims:
        text = str(c.get("text") or "")
        if any(w == "*" or w.lower() in text.lower() for w in wanted):
            c["status"] = "waived"
            c["notes"] = note
            done.append(text)
    return done


def _studio_html(script: schema.Script, seo: schema.SEO, prov: dict,
                 audit: dict, ready: dict, gates: dict) -> str:
    """A static, self-contained Review Studio page (no scripts, no network)."""
    def esc(x) -> str:
        return (str(x or "").replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))
    gate_rows = "".join(
        f"<tr><td>{esc(k)}</td><td class='{'ok' if v else 'bad'}'>"
        f"{'PASS' if v else 'FAIL'}</td></tr>" for k, v in gates.items())
    sections = "".join(f"<h3>{esc(s.title)}</h3><ul>" +
                       "".join(f"<li>{esc(c)}</li>" for c in s.content) +
                       "</ul>" for s in script.sections)
    claims = "".join(f"<li>{esc(c.get('text'))} "
                     f"<em>[{esc(c.get('status'))}]</em></li>"
                     for c in (prov.get("claims") or [])) or "<li>none</li>"
    findings = "".join(f"<li>{esc(f)}</li>"
                       for f in (audit.get("findings") or [])) or "<li>none</li>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Review Studio — {esc(script.title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{{font:15px/1.55 system-ui,sans-serif;margin:0;background:#0F172A;color:#E2E8F0}}
 header,main{{max-width:900px;margin:0 auto;padding:20px}}
 h1{{font-size:22px}} h2{{font-size:17px;color:#F59E0B;margin-top:28px}}
 table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #334155;padding:6px}}
 .ok{{color:#34D399}} .bad{{color:#F87171}}
 code,em{{color:#94A3B8}}
</style></head><body>
<header><h1>Review Studio — {esc(script.title)}</h1>
<p>{esc(seo.description[:200])}</p></header>
<main>
 <h2>Gates</h2><table><tr><th>gate</th><th>result</th></tr>{gate_rows}</table>
 <h2>Script</h2>{sections}
 <h2>Fact ledger (claims)</h2><ul>{claims}</ul>
 <h2>Discoverability findings (advisory)</h2><ul>{findings}</ul>
 <h2>Readiness</h2>
 <p>status: <strong>{esc(ready.get('status'))}</strong></p>
 <p><em>Approval is yours. Nothing is uploaded from this page.</em></p>
</main></body></html>"""


# ── PUBLISH ──────────────────────────────────────────────────────────────────
class PublishStep:
    name = "publish"
    role = "schedule and (with approval) upload"

    def run(self, project, llm, *, approved: bool = False, publish: bool = False,
            publish_time: str = "", privacy: str = "", **_) -> dict:
        project.ensure()
        ch = project.load_channel()
        script = project.load_script()
        seo = project.load_seo()
        bundle = project.load_production()
        review = project.load_review()
        prov = review.get("provenance") or {}
        ready = review.get("readiness") or {}

        decision = policy.assert_publishable(
            bundle, prov, approved=approved, publish=publish, readiness=ready)

        privacy = (privacy or ch.privacy_status or "private").lower()
        entry = schema.PublishEntry(
            production_id=bundle.get("id", ""), title=seo.title or script.title,
            publish_time=publish_time or "", status="planned", privacy=privacy,
            notes=("Not uploaded. " + decision.reason) if not decision.allowed
                  else "Approved — attempting upload.")

        if decision.allowed:
            uploaded, detail = _try_upload(project, entry, seo, bundle)
            entry.uploaded = uploaded
            entry.scheduled = uploaded
            entry.status = "published" if uploaded else "scheduled"
            if uploaded:
                entry.url = detail.get("url", "")
                entry.youtube_id = detail.get("id", "")
                entry.notes = "Uploaded with your approval."
            else:
                entry.notes = ("Approved, but no upload was performed here: "
                               + detail.get("reason", "no YouTube client") +
                               ". The schedule entry is ready to publish from a "
                               "machine with credentials.")

        project.save_publish(entry)
        project.mark(self.name, decision.allowed,
                     entry.status + (" (uploaded)" if entry.uploaded else ""))
        return {"stage": self.name, "ok": True, "entry": entry,
                "allowed": decision.allowed, "violations": decision.violations,
                "note": (f"publish is {'ALLOWED' if decision.allowed else 'BLOCKED'}"
                         f" — status: {entry.status}"),
                "reason": decision.reason}


def _try_upload(project, entry: schema.PublishEntry, seo: schema.SEO,
                bundle: dict) -> tuple[bool, dict]:
    """Best-effort real upload. Never fakes success."""
    creds = toolkit.youtube_credentials()
    if not creds.get("configured"):
        return False, {"reason": f"no YouTube credentials ({creds.get('detail')})"}
    final = bundle.get("finalVideo") or {}
    path = final.get("path") or ""
    if not path or not Path(path).is_file():
        return False, {"reason": "no renderable MP4 on disk"}
    try:
        import googleapiclient.discovery  # noqa: F401
        import google.oauth2.credentials  # noqa: F401
    except Exception:
        return False, {"reason": "google-api-python-client is not installed"}
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        data = json.loads(Path(creds["path"]).read_text(encoding="utf-8"))
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            return False, {"reason": "credential file has no token yet "
                                     "(complete OAuth first)"}
        creds_obj = Credentials.from_authorized_user_info(data)
        service = build("youtube", "v3", credentials=creds_obj)
        meta = toolkit.validate_metadata(seo).get("value") or {}
        body = {"snippet": {"title": meta.get("title"),
                            "description": meta.get("description"),
                            "tags": meta.get("tags"),
                            "categoryId": meta.get("categoryId"),
                            "defaultLanguage": meta.get("defaultLanguage")},
                "status": {"privacyStatus": entry.privacy,
                           "selfDeclaredMadeForKids": False,
                           "containsSyntheticMedia":
                               bool(bundle.get("containsSyntheticMedia"))}}
        media = MediaFileUpload(path, chunksize=-1, resumable=True,
                                mimetype="video/mp4")
        req = service.videos().insert(part="snippet,status", body=body,
                                      media_body=media)
        resp = req.execute()
        vid = resp.get("id", "")
        return True, {"id": vid,
                      "url": f"https://www.youtube.com/watch?v={vid}"}
    except Exception as exc:
        return False, {"reason": policy.redact(f"{type(exc).__name__}: {exc}")[:300]}


# ── LEARN ────────────────────────────────────────────────────────────────────
class LearnStep:
    name = "learn"
    role = "measure the outcome and propose the next move"

    def run(self, project, llm, *, evidence: dict | None = None,
            retention_curves: dict | None = None, approve: list | None = None,
            reject: list | None = None, kpi: str = "", **_) -> dict:
        project.ensure()
        ch = project.load_channel()
        kpi = (kpi or ch.kpi or "views").lower()
        prior = project.load_learning().get("snapshots") or []
        script = project.load_script()
        research = project.load_research()
        strategy = research.topics[0].to_dict() if research.topics else {}

        snapshots: list[dict] = []
        used_evidence = bool(evidence)
        for window in ("24h", "7d"):
            report = (evidence or {}).get(window)
            if not isinstance(report, dict):
                continue
            result = toolkit.capture_learning(window, report, {
                "videoId": report.get("videoId", ""),
                "strategy": {**strategy, "contentPillar": strategy.get("pillar")},
                "script": script.to_dict(),
                "contentFormat": "long_form"}, prior + snapshots)
            if result.get("ok"):
                snap = result
                snap["window"] = window
                snap["summary"] = _window_summary(snap, kpi)
                snapshots.append(snap)

        for window in ("24h", "7d"):
            curve = (retention_curves or {}).get(window)
            if isinstance(curve, dict):
                snap = toolkit.capture_retention(curve, {
                    "productionId": project.load_production().get("id", ""),
                    "retentionScenes": [
                        {"id": f"scene_{i}", "label": s.get("title"),
                         "duration": s.get("duration")}
                        for i, s in enumerate(script.scene_plan or [])]})
                if snap:
                    rec = _retention_recommendation(snap)
                    if rec:
                        snapshots.append({"window": window,
                                          "recommendation": rec,
                                          "summary": snap.get("summary", {})})

        # ── proposals (pending until the human approves) ─────────────────
        existing = project.load_recommendations()
        existing = _apply_review(existing, approve, reject)
        proposals = _proposals(snapshots, ch, kpi, existing)
        merged = _merge_recommendations(existing, proposals)
        merged = _apply_review(merged, approve, reject)

        project.save_learning(snapshots, merged)
        project.mark(self.name, True,
                     f"{len(snapshots)} snapshot(s), "
                     f"{sum(1 for r in merged if r.get('status') == 'approved')} "
                     f"approved")
        return {"stage": self.name, "ok": True, "snapshots": snapshots,
                "recommendations": merged,
                "approved": [r for r in merged
                             if r.get("status") == "approved"],
                "pending": [r for r in merged
                            if r.get("status") == "pending"],
                "note": (f"{len(snapshots)} measured window(s); "
                         + (f"{len(merged)} proposal(s) "
                            f"({sum(1 for r in merged if r['status'] == 'pending')} "
                            f"pending your approval)" if merged else
                            "no evidence supplied — nothing proposed (never "
                            "fabricated)")),
                "used_evidence": used_evidence}


def _window_summary(snap: dict, kpi: str) -> str:
    from orchestrator.agents.youtube.tools import learning
    text = learning.summarize(snap)
    value = (snap.get("metrics") or {}).get(kpi)
    target = ""
    if value is not None:
        target = f"\n- {kpi} = {value}"
    return text + target


def _retention_recommendation(snap: dict) -> dict | None:
    from orchestrator.agents.youtube.tools import retention
    return retention.build_recommendation(snap)


def _proposals(snapshots: list[dict], ch: schema.Channel, kpi: str,
               existing: list[dict]) -> list[dict]:
    out: list[dict] = []
    for snap in snapshots:
        if snap.get("recommendation"):
            out.append(snap["recommendation"])
            continue
        metrics = snap.get("metrics") or {}
        deltas = snap.get("deltas") or {}
        confidence = snap.get("confidence") or "low"
        if not metrics:
            continue
        views_delta = deltas.get("views")
        if views_delta is not None and views_delta < -20:
            out.append(schema.Recommendation(
                category="growth",
                title=f"Fix the {kpi} shortfall",
                rationale=(f"{kpi} fell {abs(views_delta):.0f}% vs the channel "
                           f"baseline in the {snap.get('window')} window."),
                proposed_change=("Revisit the hook and thumbnail for this "
                                 "topic — test a sharper promise and a higher-"
                                 "contrast thumbnail before the next upload."),
                confidence=confidence, kpi=kpi,
                expected_effect="Recover toward the rolling baseline.").to_dict())
        elif views_delta is not None and views_delta > 20:
            out.append(schema.Recommendation(
                category="growth",
                title="Double down on this topic",
                rationale=(f"{kpi} beat the baseline by {views_delta:.0f}% in "
                           f"the {snap.get('window')} window."),
                proposed_change=("Plan a follow-up in the same pillar with the "
                                 "same format and length."),
                confidence=confidence, kpi=kpi,
                expected_effect="Repeat a demonstrated win.").to_dict())
        roi = metrics.get("roi")
        if roi is not None and roi < 0:
            out.append(schema.Recommendation(
                category="economics",
                title="Cut the unit cost",
                rationale=f"This video's ROI is {roi}% (revenue vs production cost).",
                proposed_change=("Move to the local slideshow/TTS path or "
                                 "shorten the format until ROI is positive."),
                confidence=confidence, kpi="revenue",
                expected_effect="Bring ROI above zero.").to_dict())
        if ch.target_value:
            key = _KPI_METRIC.get(kpi, kpi)
            value = metrics.get(key)
            if isinstance(value, (int, float)) and value < float(ch.target_value):
                out.append(schema.Recommendation(
                    category="growth",
                    title=f"Close the {kpi} gap to target",
                    rationale=(f"Measured {kpi} = {value} against your target of "
                               f"{ch.target_value} per {ch.target_window_days} "
                               f"days in the {snap.get('window')} window."),
                    proposed_change=("Plan a follow-up in the strongest pillar "
                                     "and reformat the hook/thumbnail toward the "
                                     "target audience."),
                    confidence=confidence, kpi=kpi,
                    expected_effect=f"Move {kpi} toward {ch.target_value}.").to_dict())
    return out


_KPI_METRIC = {
    "views": "views", "watch_time": "watchMinutes", "subscribers":
    "netSubscribers", "ctr": "ctr", "revenue": "estimatedRevenue",
    "retention": "retention",
}


def _merge_recommendations(existing: list[dict],
                           new: list[dict]) -> list[dict]:
    by_title = {str(r.get("title", "")).lower(): r for r in existing}
    for r in new:
        key = str(r.get("title", "")).lower()
        if key and key not in by_title:
            by_title[key] = r
    return list(by_title.values())


def _apply_review(recommendations: list[dict], approve: list | None,
                  reject: list | None) -> list[dict]:
    approve_keys = {str(x).lower() for x in (approve or [])}
    reject_keys = {str(x).lower() for x in (reject or [])}
    # Bare flags: approve=["*"] / reject=["*"]
    if "*" in approve_keys:
        for r in recommendations:
            if (r.get("status") or "pending") == "pending":
                r["status"] = "approved"
    if "*" in reject_keys:
        for r in recommendations:
            if (r.get("status") or "pending") == "pending":
                r["status"] = "rejected"
    for r in recommendations:
        title = str(r.get("title", "")).lower()
        if title in approve_keys:
            r["status"] = "approved"
        elif title in reject_keys:
            r["status"] = "rejected"
    return recommendations


# ── helpers ──────────────────────────────────────────────────────────────────
def _as_channel(value) -> schema.Channel | None:
    if value is None:
        return None
    if isinstance(value, schema.Channel):
        return value.normalised()
    if isinstance(value, dict):
        return schema.parse_channel(value)
    return None


def _clean_signals(signals) -> list[dict]:
    out = []
    for s in (signals or [])[:15]:
        if isinstance(s, dict) and s.get("topic"):
            out.append({"topic": str(s["topic"])[:120],
                        "score": s.get("score"),
                        "sources": s.get("sources") or [],
                        "evidence": s.get("evidence") or []})
    return out


def _clean_catalog(catalog) -> list[dict]:
    out = []
    for s in (catalog or [])[:30]:
        if isinstance(s, dict) and s.get("url"):
            out.append({"title": str(s.get("title") or s["url"])[:200],
                        "url": str(s["url"])})
    return out


def _recent_topics(project) -> list[str]:
    state = project.state.get("recent_topics") or []
    return list(state)[:50]


def _pick_topic(research: schema.Research, topic: str,
                index: int) -> schema.Topic:
    if topic:
        for t in research.topics:
            if t.topic.lower() == topic.strip().lower():
                return t
        return schema.Topic(topic=topic.strip())
    if research.topics:
        idx = max(0, min(int(index or 0), len(research.topics) - 1))
        return research.topics[idx]
    return schema.Topic(topic="your next video")


STEPS = (ResearchStep(), ScriptStep(), ProductionStep(), ReviewStep(),
         PublishStep(), LearnStep())
BY_NAME = {s.name: s for s in STEPS}
