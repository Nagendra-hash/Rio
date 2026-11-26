"""
orchestrator/agents/youtube/schema.py — the shapes the youtube agents speak.

Plain dataclasses (no pydantic), stdlib-only, JSON round-trippable, with small
markdown renderers so every artifact the pipeline writes is produced from one
definition the writers and the tests share.

    Channel          the operator's configuration (niche, audience, blocked
                     topics, voice, format, KPI, budget)
    Topic/Research   the editorial plan and the evidence behind it
    Script/Section   hook, narrative, CTA and the factual claims to verify
    SEO              title / description / tags / chapters (+ category, lang)
    Thumbnail        the concept (text overlaid, composition, palette)
    Scene/Asset      the production timeline and each asset's right of use
    ProductionBundle the assembled result (audio, final MP4, costs, synthetic?)
    Provenance       sources + claims → status (from tools.provenance)
    ReviewReport     readiness + provenance + discoverability → gates
    PublishEntry     the schedule entry (privacy, time, status, youtube id)
    LearningSnapshot the measured window (24h / 7d / rolling)
    Recommendation   a proposed next move (pending until approved)
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

FORMATS = ("explainer", "tutorial", "list", "review", "story")
LENGTHS = ("short", "medium", "long")
PRIORITIES = ("must", "should", "could")
KPIS = ("views", "watch_time", "subscribers", "ctr", "revenue", "retention")


def slug(text: str, *, fallback: str = "video") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or fallback


@dataclass
class Channel:
    """The operator's configuration — everything the human controls."""

    name: str = "My Channel"
    objective: str = ""
    audience: str = ""
    value_proposition: str = ""
    niche: str = ""
    pillars: list[str] = field(default_factory=list)
    default_format: str = "explainer"
    default_length: str = "medium"
    length_hint: str = ""  # e.g. "8-12 minutes"
    voice: str = "clear, credible, and engaging"
    brand_direction: str = ""
    cta: str = "invite the viewer to subscribe"
    kpi: str = "views"
    target_value: float = 0.0
    target_window_days: int = 28
    monthly_budget: float | None = None
    currency: str = "USD"
    blocked_topics: list[str] = field(default_factory=list)
    constraints: str = ""
    privacy_status: str = "private"

    def normalised(self) -> "Channel":
        fmt = (self.default_format or "explainer").lower()
        if fmt not in FORMATS:
            fmt = "explainer"
        length = (self.default_length or "medium").lower()
        if length not in LENGTHS:
            length = "medium"
        kpi = (self.kpi or "views").lower()
        if kpi not in KPIS:
            kpi = "views"
        return Channel(
            name=self.name or "My Channel",
            objective=self.objective,
            audience=self.audience,
            value_proposition=self.value_proposition,
            niche=self.niche,
            pillars=[p for p in (self.pillars or []) if p],
            default_format=fmt,
            default_length=length,
            length_hint=self.length_hint,
            voice=self.voice,
            brand_direction=self.brand_direction,
            cta=self.cta,
            kpi=kpi,
            target_value=self.target_value,
            target_window_days=self.target_window_days,
            monthly_budget=self.monthly_budget,
            currency=self.currency or "USD",
            blocked_topics=[t for t in (self.blocked_topics or []) if t],
            constraints=self.constraints,
            privacy_status=(self.privacy_status or "private").lower(),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Topic:
    topic: str
    pillar: str = ""
    angle: str = ""
    rationale: str = ""
    format: str = "explainer"
    length: str = "medium"
    source_urls: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Research:
    generated_at: str = ""
    sources: list[str] = field(default_factory=list)
    source_catalog: list[dict] = field(default_factory=list)
    notes: str = ""
    approved_learnings: list[dict] = field(default_factory=list)
    topics: list[Topic] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def render(self, channel: Channel) -> str:
        out = [
            f"# Research & editorial plan — {channel.name}",
            "",
            f"Objective: {channel.objective or '—'}",
            f"Audience: {channel.audience or '—'}",
            f"Value proposition: {channel.value_proposition or '—'}",
            f"Pillars: {', '.join(channel.pillars) or '—'}",
            f"KPI: {channel.kpi} target {channel.target_value or '—'} per "
            f"{channel.target_window_days} days",
            "",
        ]
        if channel.blocked_topics:
            out += ["## Blocked topics (never planned)", ""]
            out += [f"- {t}" for t in channel.blocked_topics]
            out.append("")
        out += ["## Topics", ""]
        out += [
            "| # | topic | pillar | format | length | rationale | source |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for i, t in enumerate(self.topics, 1):
            out.append(
                f"| {i} | {t.topic} | {t.pillar} | {t.format} | "
                f"{t.length} | {t.rationale} | "
                f"{', '.join(t.source_urls) or '—'} |"
            )
        out.append("")
        out += ["## Evidence sources", ""]
        out += [f"- {s}" for s in self.sources] or ["- (none)"]
        out.append("")
        if self.source_catalog:
            out += [
                "## Source catalog (only these URLs may be cited)",
                "",
                "| title | url |",
                "| --- | --- |",
            ]
            for s in self.source_catalog:
                out.append(f"| {s.get('title', '')} | {s.get('url', '')} |")
            out.append("")
        if self.approved_learnings:
            out += ["## Approved learnings applied (human-approved only)", ""]
            for l in self.approved_learnings:
                out.append(f"- {l.get('title', '')} — {l.get('rationale', '')}")
            out.append("")
        out += [
            "_Rule: no invented trends, statistics, sources or URLs. Only "
            "exact URLs from the source catalog may be cited._",
            "",
        ]
        return "\n".join(out)


@dataclass
class Claim:
    text: str
    risk_level: str = "standard"
    source_urls: list[str] = field(default_factory=list)
    status: str = "pending"
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Section:
    title: str
    content: list[str] = field(default_factory=list)
    duration: int = 60

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Script:
    title: str = ""
    hook: str = ""
    sections: list[Section] = field(default_factory=list)
    cta: str = ""
    tone: str = ""
    pacing: str = ""
    keywords: list[str] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    duration_estimate: int = 0
    scene_plan: list[dict] = field(default_factory=list)

    def full_text(self) -> str:
        parts = (
            [self.hook]
            + [f"{s.title}: " + " ".join(s.content) for s in self.sections]
            + [self.cta]
        )
        return "\n\n".join(p for p in parts if p).strip()

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "hook": self.hook,
            "sections": [s.to_dict() for s in self.sections],
            "cta": self.cta,
            "tone": self.tone,
            "pacing": self.pacing,
            "keywords": self.keywords,
            "claims": [c.to_dict() for c in self.claims],
            "duration_estimate": self.duration_estimate,
            "scene_plan": self.scene_plan,
            "fullScript": self.full_text(),
        }

    def render(self) -> str:
        out = [f"# Script — {self.title}", "", f"**Hook:** {self.hook}", ""]
        for i, s in enumerate(self.sections, 1):
            out.append(f"## {i}. {s.title}  ({s.duration}s)")
            out.append("")
            out += [f"- {line}" for line in s.content]
            out.append("")
        out += ["## Call to action", "", self.cta, ""]
        out += [
            f"_Tone:_ {self.tone}  _Pacing:_ {self.pacing}  "
            f"_Est. duration:_ ~{self.duration_estimate}s",
            "",
        ]
        if self.keywords:
            out += ["Keywords: " + ", ".join(self.keywords), ""]
        if self.claims:
            out += [
                "## Claims a reviewer must verify",
                "",
                "| claim | risk | status | sources |",
                "| --- | --- | --- | --- |",
            ]
            for c in self.claims:
                out.append(
                    f"| {c.text} | {c.risk_level} | {c.status} | "
                    f"{', '.join(c.source_urls) or '—'} |"
                )
            out.append("")
        return "\n".join(out)


@dataclass
class SEO:
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    chapters: list[dict] = field(default_factory=list)
    category_id: str = "22"
    language: str = "en"
    score: int = 0

    def to_metadata(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "categoryId": self.category_id,
            "defaultLanguage": self.language,
            "defaultAudioLanguage": self.language,
        }

    def to_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        out = [
            "# Metadata (SEO)",
            "",
            f"**Title ({len(self.title)}/100):** {self.title}",
            "",
            f"**Description ({len(self.description)}/5000):**",
            "",
            self.description,
            "",
            f"**Tags ({len(self.tags)}):** {', '.join(self.tags) or '—'}",
            "",
        ]
        if self.chapters:
            out += ["## Chapters", ""]
            for c in self.chapters:
                out.append(f"- {c.get('time', '')} {c.get('title', '')}")
            out.append("")
        out += [
            f"Category: {self.category_id}  Language: {self.language}  "
            f"SEO score: {self.score}/100",
            "",
        ]
        return "\n".join(out)


@dataclass
class Thumbnail:
    concept: str = ""
    composition: str = ""
    primary_text: str = ""
    secondary_text: str = ""
    palette: list[str] = field(default_factory=list)
    prompt: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        return "\n".join(
            [
                "# Thumbnail concept",
                "",
                f"**Primary text:** {self.primary_text}",
                f"**Secondary text:** {self.secondary_text}",
                "",
                f"Composition: {self.composition}",
                f"Palette: {', '.join(self.palette) or '—'}",
                "",
                "## Image prompt",
                "",
                self.prompt,
                "",
                "_Rendered by the configured image provider, or the local slideshow "
                "fallback. Never thumbnail art lifted from another channel._",
                "",
            ]
        )


@dataclass
class Asset:
    kind: str = "visual"  # visual | audio | thumbnail | video
    path: str = ""
    provider: str = ""
    license: str = ""  # right of use (owned/licensed/generated/…)
    generated: bool = False
    simulated: bool = False
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Scene:
    id: str
    index: int
    label: str
    narration: str = ""
    duration: float = 5.0
    visual: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProductionBundle:
    id: str = ""
    title: str = ""
    scenes: list[Scene] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    audio: dict = field(default_factory=dict)
    final_video: dict = field(default_factory=dict)
    assembly_cmd: list[str] = field(default_factory=list)
    contains_synthetic_media: bool = False
    intentional_silence: bool = False
    costs: dict = field(default_factory=dict)
    provider: str = ""
    rendered: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "scenes": [s.to_dict() for s in self.scenes],
            "assets": [a.to_dict() for a in self.assets],
            "audio": self.audio,
            "finalVideo": self.final_video,
            "assemblyCmd": self.assembly_cmd,
            "containsSyntheticMedia": self.contains_synthetic_media,
            "intentionalSilence": self.intentional_silence,
            "costs": self.costs,
            "provider": self.provider,
            "rendered": self.rendered,
        }

    def render(self) -> str:
        out = [
            f"# Production — {self.title}",
            "",
            f"Provider: {self.provider or '—'}  "
            f"Rendered: {'yes' if self.rendered else 'no (plan only)'}",
            "",
            f"Final video: {self.final_video.get('path') or '—'}"
            + (
                "  (SIMULATED — cannot be published)"
                if self.final_video.get("simulated")
                else ""
            ),
            "",
            f"Audio: {self.audio.get('path') or '—'}"
            + ("  (simulated)" if self.audio.get("simulated") else ""),
            "",
            f"Contains synthetic media: "
            f"{'yes' if self.contains_synthetic_media else 'no'}",
            "",
            "## Scenes",
            "",
            "| # | scene | duration | narration (head) | visual | right |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for s in self.scenes:
            v = s.visual or {}
            out.append(
                f"| {s.index + 1} | {s.label} | {s.duration}s | "
                f"{s.narration[:60]} | {v.get('provider') or v.get('kind') or '—'} | "
                f"{v.get('license') or '—'} |"
            )
        out.append("")
        if self.assembly_cmd:
            out += [
                "## Assembly (ffmpeg)",
                "",
                "```bash",
                " ".join(self.assembly_cmd),
                "```",
                "",
            ]
        return "\n".join(out)


@dataclass
class PublishEntry:
    production_id: str = ""
    title: str = ""
    publish_time: str = ""
    status: str = "planned"  # planned | scheduled | published | failed
    privacy: str = "private"
    youtube_id: str = ""
    url: str = ""
    scheduled: bool = False
    uploaded: bool = False
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        out = [
            f"# Publish — {self.title}",
            "",
            f"Status: **{self.status}**",
            "",
            f"- privacy: {self.privacy}",
            f"- publish time: {self.publish_time or '—'}",
            f"- scheduled on YouTube: {'yes' if self.scheduled else 'no'}",
            f"- uploaded: {'yes' if self.uploaded else 'no'}",
        ]
        if self.url:
            out.append(f"- url: {self.url}")
        if self.notes:
            out += ["", self.notes]
        out.append("")
        return "\n".join(out)


@dataclass
class LearningSnapshot:
    video_id: str = ""
    window: str = "24h"
    metrics: dict = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)
    deltas: dict = field(default_factory=dict)
    confidence: str = "insufficient"
    simulated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Recommendation:
    category: str = "growth"
    title: str = ""
    rationale: str = ""
    proposed_change: str = ""
    confidence: str = "low"
    status: str = "pending"  # pending | approved | rejected
    kpi: str = ""
    expected_effect: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReviewReport:
    readiness: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    discoverability: dict = field(default_factory=dict)
    gates: dict = field(default_factory=dict)
    approved: bool = False
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        out = [
            "# Review",
            "",
            f"**{'READY for approval' if all(self.gates.values()) and self.gates else 'NOT ready'}**",
            "",
            "## Gates",
            "",
            "| gate | result |",
            "| --- | --- |",
        ]
        for k, v in (self.gates or {}).items():
            out.append(f"| {k} | {'PASS' if v else 'FAIL'} |")
        out.append("")
        if self.findings:
            out += ["## What needs a human", ""]
            out += [f"- {f}" for f in self.findings]
            out.append("")
        return "\n".join(out)


def parse_channel(data: dict | None) -> Channel:
    """Build a Channel from loose input, ignoring unknown keys."""
    data = data or {}
    fields = {f for f in Channel.__dataclass_fields__}
    clean = {k: v for k, v in data.items() if k in fields}
    for listkey in ("pillars", "blocked_topics"):
        if isinstance(clean.get(listkey), str):
            clean[listkey] = [x.strip() for x in clean[listkey].split(",") if x.strip()]
    return Channel(**clean).normalised()


def to_json(obj: Any) -> str:
    return json.dumps(
        obj.to_dict() if hasattr(obj, "to_dict") else obj, indent=2, default=str
    )
