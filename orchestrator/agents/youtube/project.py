"""
orchestrator/agents/youtube/project.py — the artifact store.

Everything a run produces lives under one ``youtube/`` folder next to the work
it is producing (by default under the channel workspace). Nothing here is
uploaded anywhere; it is the human-readable record of what the agent decided and
why. The layout mirrors the pipeline stages:

    youtube/
      state.json            resumable pipeline state
      channel.json          the operator's configuration (as used)
      research.md           the editorial plan + evidence      (research)
      script.md/.json       hook, sections, CTA, claims        (script)
      seo.json / seo.md     title, description, tags, chapters (script)
      thumbnail.json        the concept                        (script)
      production.md/.json   the timeline + assets + right-of-use (production)
      media/                rendered narration / video (if any)
      review.md / review.json  readiness + provenance + seo    (review)
      provenance.md         the fact ledger                    (review)
      review-studio.html    a local, static review page        (review)
      publish.md/.json      the schedule entry                 (publish)
      learn.json / learning.md  metric snapshots + proposals   (learn)
      recommendations.json  proposals (pending until approved) (learn)

``state.json`` carries the resumable stage marks and the last bundle, so a run
can be continued after a restart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.agents.youtube import schema


@dataclass
class YouTubeProject:
    root: Path
    channel_name: str = ""
    state: dict = field(default_factory=dict)

    # ── paths ────────────────────────────────────────────────────────────
    @property
    def state_json(self) -> Path:
        return self.root / "state.json"

    @property
    def channel_json(self) -> Path:
        return self.root / "channel.json"

    @property
    def research_md(self) -> Path:
        return self.root / "research.md"

    @property
    def research_json(self) -> Path:
        return self.root / "research.json"

    @property
    def script_md(self) -> Path:
        return self.root / "script.md"

    @property
    def script_json(self) -> Path:
        return self.root / "script.json"

    @property
    def seo_json(self) -> Path:
        return self.root / "seo.json"

    @property
    def seo_md(self) -> Path:
        return self.root / "seo.md"

    @property
    def thumbnail_json(self) -> Path:
        return self.root / "thumbnail.json"

    @property
    def production_md(self) -> Path:
        return self.root / "production.md"

    @property
    def production_json(self) -> Path:
        return self.root / "production.json"

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def review_md(self) -> Path:
        return self.root / "review.md"

    @property
    def review_json(self) -> Path:
        return self.root / "review.json"

    @property
    def provenance_md(self) -> Path:
        return self.root / "provenance.md"

    @property
    def studio_html(self) -> Path:
        return self.root / "review-studio.html"

    @property
    def publish_md(self) -> Path:
        return self.root / "publish.md"

    @property
    def publish_json(self) -> Path:
        return self.root / "publish.json"

    @property
    def learn_json(self) -> Path:
        return self.root / "learn.json"

    @property
    def learning_md(self) -> Path:
        return self.root / "learning.md"

    @property
    def recommendations_json(self) -> Path:
        return self.root / "recommendations.json"

    # ── io ───────────────────────────────────────────────────────────────
    def ensure(self) -> "YouTubeProject":
        for d in (self.root, self.media_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")

    def write_json(self, path: Path, data) -> None:
        self.write(path, json.dumps(data, indent=2, default=str))

    def read(self, path: Path) -> str:
        try:
            return Path(path).read_text(encoding="utf-8")
        except Exception:
            return ""

    def read_json(self, path: Path, default):
        try:
            return json.loads(self.read(path))
        except Exception:
            return default

    # ── state ────────────────────────────────────────────────────────────
    def save_state(self) -> None:
        self.ensure()
        self.state["channel_name"] = self.channel_name
        self.write(self.state_json, json.dumps(self.state, indent=1, default=str))

    def load_state(self) -> dict:
        self.state = self.read_json(self.state_json, {})
        if not self.channel_name:
            self.channel_name = str(self.state.get("channel_name") or "")
        return self.state

    def done(self, stage: str) -> bool:
        return stage in (self.state.get("stages") or {})

    def mark(self, stage: str, ok: bool, note: str = "") -> None:
        self.state.setdefault("stages", {})[stage] = {
            "ok": bool(ok),
            "note": note[:200],
        }
        self.save_state()

    def save_channel(self, channel: schema.Channel) -> None:
        self.channel_name = channel.name
        self.write(self.channel_json, schema.to_json(channel))
        self.state["channel"] = channel.to_dict()
        self.save_state()

    def load_channel(self) -> schema.Channel:
        data = (
            self.read_json(self.channel_json, None) or self.state.get("channel") or {}
        )
        return schema.parse_channel(data)

    # ── artifacts ────────────────────────────────────────────────────────
    def save_research(self, research: schema.Research, channel: schema.Channel) -> None:
        self.write(self.research_md, research.render(channel))
        self.write_json(self.research_json, research.to_dict())

    def load_research(self) -> schema.Research:
        data = self.read_json(self.research_json, {}) or {}
        topics = [
            schema.Topic(
                **{k: v for k, v in t.items() if k in schema.Topic.__dataclass_fields__}
            )
            for t in (data.get("topics") or [])
            if isinstance(t, dict)
        ]
        return schema.Research(
            generated_at=data.get("generated_at", ""),
            sources=data.get("sources", []),
            source_catalog=data.get("source_catalog", []),
            notes=data.get("notes", ""),
            approved_learnings=data.get("approved_learnings", []),
            topics=topics,
        )

    def save_script(
        self, script: schema.Script, seo: schema.SEO, thumbnail: schema.Thumbnail
    ) -> None:
        self.write(self.script_md, script.render())
        self.write_json(self.script_json, script.to_dict())
        self.write_json(self.seo_json, seo.to_dict())
        self.write(self.seo_md, seo.render())
        self.write_json(self.thumbnail_json, thumbnail.to_dict())

    def load_script(self) -> schema.Script:
        data = self.read_json(self.script_json, {}) or {}
        sections = [
            schema.Section(
                title=s.get("title", ""),
                content=s.get("content", []) or [],
                duration=int(s.get("duration") or 60),
            )
            for s in (data.get("sections") or [])
            if isinstance(s, dict)
        ]
        if not sections and (data.get("mainContent") or {}).get("sections"):
            sections = [
                schema.Section(
                    title=s.get("title", ""),
                    content=s.get("content", []) or [],
                    duration=int(s.get("duration") or 60),
                )
                for s in data["mainContent"]["sections"]
            ]
        claims = [
            schema.Claim(
                **{k: v for k, v in c.items() if k in schema.Claim.__dataclass_fields__}
            )
            for c in (data.get("claims") or [])
            if isinstance(c, dict)
        ]
        script = schema.Script(
            title=data.get("title", ""),
            hook=data.get("hook", ""),
            sections=sections,
            cta=data.get("cta", ""),
            tone=data.get("tone", ""),
            pacing=data.get("pacing", ""),
            keywords=data.get("keywords", []),
            claims=claims,
            duration_estimate=int(data.get("duration_estimate") or 0),
            scene_plan=data.get("scene_plan", []),
        )
        if not script.duration_estimate and sections:
            script.duration_estimate = sum(s.duration for s in sections) + 20
        return script

    def load_seo(self) -> schema.SEO:
        data = self.read_json(self.seo_json, {}) or {}
        return schema.SEO(
            **{k: v for k, v in data.items() if k in schema.SEO.__dataclass_fields__}
        )

    def save_production(self, bundle: schema.ProductionBundle) -> None:
        self.write(self.production_md, bundle.render())
        self.write_json(self.production_json, bundle.to_dict())

    def load_production(self) -> dict:
        return self.read_json(self.production_json, {}) or {}

    def save_review(
        self, review: schema.ReviewReport, provenance_dict: dict | None = None
    ) -> None:
        self.write(self.review_md, review.render())
        self.write_json(self.review_json, review.to_dict())
        if provenance_dict is not None:
            from orchestrator.agents.youtube.tools import provenance as _prov

            self.write(self.provenance_md, _prov.render(provenance_dict))

    def load_review(self) -> dict:
        return self.read_json(self.review_json, {}) or {}

    def save_publish(self, entry: schema.PublishEntry) -> None:
        self.write(self.publish_md, entry.render())
        self.write_json(self.publish_json, entry.to_dict())

    def load_publish(self) -> schema.PublishEntry:
        data = self.read_json(self.publish_json, {}) or {}
        return schema.PublishEntry(
            **{
                k: v
                for k, v in data.items()
                if k in schema.PublishEntry.__dataclass_fields__
            }
        )

    def save_learning(self, snapshots: list[dict], recommendations: list[dict]) -> None:
        self.write_json(self.learn_json, {"snapshots": snapshots})
        self.write_json(self.recommendations_json, {"recommendations": recommendations})
        lines = ["# Learn — what the evidence says", ""]
        for snap in snapshots:
            lines.append(f"## {snap.get('window', '?')} window")
            lines.append("")
            lines.append(snap.get("summary", "_(no summary)_"))
            lines.append("")
        lines.append("## Proposed next moves (pending your approval)")
        lines.append("")
        if recommendations:
            for r in recommendations:
                lines.append(
                    f"- **{r.get('title')}** "
                    f"[{r.get('status', 'pending')}] — "
                    f"{r.get('rationale', '')}"
                )
        else:
            lines.append("_(nothing to propose — no usable evidence yet)_")
        lines.append("")
        self.write(self.learning_md, "\n".join(lines))

    def load_learning(self) -> dict:
        return self.read_json(self.learn_json, {"snapshots": []})

    def load_recommendations(self) -> list[dict]:
        data = self.read_json(self.recommendations_json, {"recommendations": []})
        return data.get("recommendations", []) if isinstance(data, dict) else []
