"""
orchestrator/agents/replica/schema.py — the shapes the replica agents speak.

Plain dataclasses (no pydantic), stdlib-only, JSON round-trippable. Every
artifact the pipeline writes (`replica/*.md`, `replica/*.csv`,
`replica/*.json`) is produced from one of these, so the writers and the tests
share a single definition.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

# Feature-matrix priorities, exactly as the vendored parity tool reads them.
PRIORITIES = ("must", "should", "could")
# The "clone" column values the parity tool credits.
CLONE_STATES = ("yes", "partial", "no", "skip")

PRIORITY_ORDER = {"must": 0, "should": 1, "could": 2}


def slug(text: str, *, fallback: str = "app") -> str:
    """A safe folder/screen id fragment."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or fallback


@dataclass
class Source:
    label: str
    url: str = ""
    notes: str = ""


@dataclass
class Screen:
    id: str                      # S01...
    name: str
    reach: str = ""              # route / how to reach it
    purpose: str = ""
    components: str = ""
    states: str = "empty, filled, error"


@dataclass
class Flow:
    id: str                      # F01...
    goal: str
    screens: list[str] = field(default_factory=list)
    happy_clicks: int = 0
    edge: str = ""


@dataclass
class Feature:
    feature: str
    area: str = ""
    priority: str = "must"
    original: str = "yes"        # yes | no (no = an enhancement you added)
    clone: str = "no"            # yes | partial | no | skip
    notes: str = ""

    def normalised(self) -> "Feature":
        p = (self.priority or "must").lower()
        if p in ("p0", "3"):
            p = "must"
        elif p in ("p1", "2"):
            p = "should"
        elif p in ("p2", "1"):
            p = "could"
        if p not in PRIORITIES:
            p = "could"
        o = (self.original or "yes").lower()
        original = "no" if o in ("no", "n", "false") else "yes"
        c = (self.clone or "no").lower()
        if c not in CLONE_STATES:
            c = "no"
        return Feature(self.feature, self.area, p, original, c, self.notes)


@dataclass
class Entity:
    name: str
    fields: str = ""
    evidence: str = ""
    confidence: str = "guess"    # high | medium | guess


@dataclass
class Review:
    source: str
    url: str
    date: str = ""
    rating: Any = ""             # 1..5 or ""
    text: str = ""


@dataclass
class ReviewInsight:
    """One ranked theme from the critic agent (from the vendored tool)."""
    label: str
    kind: str                    # complaint | request
    score: float = 0.0
    count: int = 0
    share_pct: float = 0.0
    avg_rating: Any = None
    thin: bool = False
    quotes: list[dict] = field(default_factory=list)


@dataclass
class Fix:
    """An improvement folded into the clone — the 'enhanced version'."""
    title: str
    from_theme: str = ""
    kind: str = "complaint"
    evidence_urls: list[str] = field(default_factory=list)
    plan: str = ""


@dataclass
class ReconMap:
    app: str = ""
    platform: str = "android"
    package: str = ""
    listing_url: str = ""
    scope: str = "full app"
    sources: list[Source] = field(default_factory=list)
    core_loop: str = ""
    screens: list[Screen] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    features: list[Feature] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    category: str = ""
    rating: Any = None
    installs: str = ""
    whats_new: str = ""
    description: str = ""
    screenshots: list[str] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)

    # ── derived ──────────────────────────────────────────────────────────
    def must_features(self) -> list[Feature]:
        return [f for f in self.features if f.priority == "must"]

    def size(self) -> str:
        n = len(self.screens)
        if n <= 3:
            return "S"
        if n <= 6:
            return "M"
        if n <= 12:
            return "L"
        return "XL"

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        import json
        return json.dumps(self.to_dict(), indent=2, default=str)

    # ── markdown ─────────────────────────────────────────────────────────
    def render_recon_md(self) -> str:
        out = [f"# Recon map: {self.app or 'unknown app'} ({self.platform})",
               "",
               f"Scope: {self.scope}",
               f"Listing: {self.listing_url or '—'}",
               f"Category: {self.category or '—'}  Rating: "
               f"{self.rating if self.rating is not None else '—'}  "
               f"Installs: {self.installs or '—'}",
               ""]
        out.append("## Sources")
        out.append("")
        out.append("| # | source | URL | notes |")
        out.append("| --- | --- | --- | --- |")
        if self.sources:
            for i, s in enumerate(self.sources, 1):
                out.append(f"| {i} | {s.label} | {s.url} | {s.notes} |")
        else:
            out.append("| 1 | (none recorded) | | add the listing and any docs |")
        out.append("")
        out.append("## Core loop")
        out.append("")
        out.append(self.core_loop or "_(not yet determined)_")
        out.append("")
        out.append("## Screens")
        out.append("")
        out.append("| ID | screen | reach | purpose | key components | states |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for s in self.screens:
            out.append(f"| {s.id} | {s.name} | {s.reach} | {s.purpose} | "
                       f"{s.components} | {s.states} |")
        if not self.screens:
            out.append("| S01 | | | | | |")
        out.append("")
        out.append("## Flows")
        out.append("")
        out.append("```")
        for f in self.flows:
            out.append(f"{f.id} {f.goal}")
            out.append(f"    {' -> '.join(f.screens) or '(screens tbd)'}")
            out.append(f"    happy path taps: {f.happy_clicks}")
            if f.edge:
                out.append(f"    edge: {f.edge}")
        if not self.flows:
            out.append("F01 (main flow)  S01 -> ...")
        out.append("```")
        out.append("")
        out.append("## Inferred data model")
        out.append("")
        out.append("```")
        for e in self.entities:
            out.append(f"{e.name}  {e.fields}")
            out.append(f"            evidence: {e.evidence}")
            out.append(f"            confidence: {e.confidence}")
        if not self.entities:
            out.append("(inferred from screens and listing text)")
        out.append("```")
        out.append("")
        out.append("## Feature matrix")
        out.append("")
        must = sum(1 for f in self.features if f.priority == "must")
        should = sum(1 for f in self.features if f.priority == "should")
        could = sum(1 for f in self.features if f.priority == "could")
        skip = sum(1 for f in self.features if f.clone == "skip")
        out.append(f"See `features.csv`. Must: {must}, should: {should}, "
                   f"could: {could}, skip: {skip}.")
        out.append("")
        out.append("## Out of scope (cannot or should not be cloned)")
        out.append("")
        if self.out_of_scope:
            for item in self.out_of_scope:
                out.append(f"- {item}")
        else:
            out.append("- licensed content, partner networks, and the app's own "
                       "branded assets")
        out.append("")
        out.append("## Size")
        out.append("")
        out.append(f"Screens {len(self.screens)}, flows {len(self.flows)}, "
                   f"entities {len(self.entities)}. Size: {self.size()}.")
        out.append("")
        return "\n".join(out)

    def render_features_csv(self) -> str:
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["feature", "area", "priority", "original", "clone", "notes"])
        for f in self.features:
            f = f.normalised()
            w.writerow([f.feature, f.area, f.priority, f.original, f.clone,
                        f.notes])
        return buf.getvalue()


def parse_features_csv(text: str) -> list[Feature]:
    """Read a features.csv back into Feature objects (tolerant)."""
    import csv
    import io
    rows: list[Feature] = []
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return rows
    cols = {(c or "").strip().lower() for c in reader.fieldnames}
    if "feature" not in cols:
        return rows
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip()
               for k, v in raw.items()}
        if not row.get("feature"):
            continue
        rows.append(Feature(
            feature=row.get("feature", ""),
            area=row.get("area", ""),
            priority=row.get("priority", "must"),
            original=row.get("original", "yes") or "yes",
            clone=row.get("clone", "no") or "no",
            notes=row.get("notes", ""),
        ).normalised())
    return rows
