"""
orchestrator/agents/replica/project.py — the artifact store.

Everything a replica run produces lives under one ``replica/`` folder next to
the app it is building. The layout mirrors the upstream Replica pack so the
vendored tools' paths and the agents' outputs line up:

    replica/
      state.json            resumable pipeline state (never shipped)
      recon.md              the recon map            (scout)
      features.csv          the feature matrix       (study → enhance)
      architecture.md       Android stack + schema   (study)
      design/tokens.json    design tokens            (design)
      design/components.md  component specs          (design)
      screens/S01.png…      the original's public store screenshots (reference only)
      reviews.csv           the reviews you collected (critic in)
      fixes.md              what users hate/want      (critic out)
      positioning.md        the angle                (enhance)
      brand.json / brand.md rename + palette + checks (enhance)
      launch/listing.json   the new store listing    (enhance)
      diffs/S01.json        clone-vs-original diffs  (verify)
      parity.md             the parity report        (verify)
      build-log.md          what the builder did     (builder → verify)

Nothing in ``replica/`` is shipped inside the app, and the rebrand sweep
skips this folder by default.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.agents.replica import schema


@dataclass
class ReplicaProject:
    root: Path  # the replica/ folder
    app_name: str = ""
    listing_url: str = ""
    package: str = ""
    state: dict = field(default_factory=dict)

    # ── paths ────────────────────────────────────────────────────────────
    @property
    def state_json(self) -> Path:
        return self.root / "state.json"

    @property
    def recon_md(self) -> Path:
        return self.root / "recon.md"

    @property
    def recon_json(self) -> Path:
        return self.root / "recon.json"

    @property
    def features_csv(self) -> Path:
        return self.root / "features.csv"

    @property
    def source_listing_json(self) -> Path:
        return self.root / "source-listing.json"

    @property
    def architecture_md(self) -> Path:
        return self.root / "architecture.md"

    @property
    def design_dir(self) -> Path:
        return self.root / "design"

    @property
    def tokens_json(self) -> Path:
        return self.design_dir / "tokens.json"

    @property
    def components_md(self) -> Path:
        return self.design_dir / "components.md"

    @property
    def screens_dir(self) -> Path:
        return self.root / "screens"

    @property
    def diffs_dir(self) -> Path:
        return self.root / "diffs"

    @property
    def reviews_csv(self) -> Path:
        return self.root / "reviews.csv"

    @property
    def feedback_md(self) -> Path:
        return self.root / "feedback.md"

    @property
    def fixes_md(self) -> Path:
        return self.root / "fixes.md"

    @property
    def positioning_md(self) -> Path:
        return self.root / "positioning.md"

    @property
    def brand_json(self) -> Path:
        return self.root / "brand.json"

    @property
    def brand_md(self) -> Path:
        return self.root / "brand.md"

    @property
    def launch_dir(self) -> Path:
        return self.root / "launch"

    @property
    def listing_json(self) -> Path:
        return self.launch_dir / "listing.json"

    @property
    def parity_md(self) -> Path:
        return self.root / "parity.md"

    @property
    def build_log_md(self) -> Path:
        return self.root / "build-log.md"

    # ── io ───────────────────────────────────────────────────────────────
    def ensure(self) -> "ReplicaProject":
        for d in (
            self.root,
            self.design_dir,
            self.screens_dir,
            self.diffs_dir,
            self.launch_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""

    # ── state (resumable) ────────────────────────────────────────────────
    def save_state(self) -> None:
        self.ensure()
        self.state["app_name"] = self.app_name
        self.state["listing_url"] = self.listing_url
        self.state["package"] = self.package
        self.state_json.write_text(
            json.dumps(self.state, indent=1, default=str), encoding="utf-8"
        )

    def load_state(self) -> dict:
        try:
            self.state = json.loads(self.state_json.read_text(encoding="utf-8"))
        except Exception:
            self.state = {}
        if not self.app_name:
            self.app_name = str(self.state.get("app_name") or "")
        return self.state

    def done(self, stage: str) -> bool:
        return stage in (self.state.get("stages") or {})

    def mark(self, stage: str, ok: bool, note: str = "") -> None:
        self.state.setdefault("stages", {})[stage] = {
            "ok": bool(ok),
            "note": note[:200],
        }
        self.save_state()

    # ── recon ────────────────────────────────────────────────────────────
    def save_recon(self, recon: schema.ReconMap) -> None:
        self.write(self.recon_md, recon.render_recon_md())
        self.write(self.recon_json, recon.to_json())
        self.write(self.features_csv, recon.render_features_csv())

    def load_recon(self) -> schema.ReconMap:
        """Rebuild a ReconMap from the artifacts we have (recon.json first,
        else features.csv + recon.md). Never raises.

        ``features.csv`` is the live feature matrix — the build and enhance
        steps tick it as the app is built — so it always wins over the
        features recorded in the original recon map."""
        recon = None
        try:
            data = json.loads(self.recon_json.read_text(encoding="utf-8"))
            recon = _recon_from_dict(data)
        except Exception:
            pass
        if recon is None:
            recon = schema.ReconMap(
                app=self.app_name, package=self.package, listing_url=self.listing_url
            )
        live = schema.parse_features_csv(self.read(self.features_csv))
        if live:
            recon.features = live
        return recon


def _recon_from_dict(data: dict) -> schema.ReconMap:
    def mk(cls, rows):
        fields = {f for f in cls.__dataclass_fields__}
        return [
            cls(**{k: v for k, v in (r or {}).items() if k in fields})
            for r in (rows or [])
            if isinstance(r, dict)
        ]

    return schema.ReconMap(
        app=data.get("app", ""),
        platform=data.get("platform", "android"),
        package=data.get("package", ""),
        listing_url=data.get("listing_url", ""),
        scope=data.get("scope", "full app"),
        sources=mk(schema.Source, data.get("sources")),
        core_loop=data.get("core_loop", ""),
        screens=mk(schema.Screen, data.get("screens")),
        flows=mk(schema.Flow, data.get("flows")),
        entities=mk(schema.Entity, data.get("entities")),
        features=mk(schema.Feature, data.get("features")),
        out_of_scope=list(data.get("out_of_scope") or []),
        category=data.get("category", ""),
        rating=data.get("rating"),
        installs=data.get("installs", ""),
        whats_new=data.get("whats_new", ""),
        description=data.get("description", ""),
        screenshots=list(data.get("screenshots") or []),
        reviews=mk(schema.Review, data.get("reviews")),
    )
