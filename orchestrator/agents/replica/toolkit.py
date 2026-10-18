"""
orchestrator/agents/replica/toolkit.py — one import surface over the tools.

Wraps the vendored Replica tools (MIT, see ``tools/ATTRIBUTION.md``) in
structured, never-raising functions the agents call. Same logic as the CLIs,
so a number the agent reports is a number the user can reproduce by hand:

    python3 orchestrator/agents/replica/tools/parity.py replica/features.csv
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from orchestrator.agents.replica import schema


# ── feature matrix ───────────────────────────────────────────────────────────
def feature_stats(features: list[schema.Feature]) -> dict:
    """Counts + must-have totals, deterministic (no tool needed)."""
    norm = [f.normalised() for f in features]
    counted = [f for f in norm if f.original != "no" and f.clone != "skip"]
    musts = [f for f in counted if f.priority == "must"]
    done = sum(1 for f in musts if f.clone == "yes")
    extras = [f for f in norm if f.original == "no"]
    return {
        "total": len(norm),
        "must": sum(1 for f in norm if f.priority == "must"),
        "should": sum(1 for f in norm if f.priority == "should"),
        "could": sum(1 for f in norm if f.priority == "could"),
        "must_done": done,
        "must_total": len(musts),
        "extras": len(extras),
        "shippable": done == len(musts) and len(musts) > 0,
    }


def missing_in_build_order(features: list[schema.Feature]) -> list[schema.Feature]:
    """Features not fully built, must-haves first, then should, then could."""
    norm = [f.normalised() for f in features]
    missing = [f for f in norm
               if f.original != "no" and f.clone != "skip" and f.clone != "yes"]
    return sorted(missing, key=lambda f: (schema.PRIORITY_ORDER.get(f.priority, 9),
                                          f.area, f.feature))


def parity_score(features_csv: Path | str, visuals: list | None = None,
                 fail_under: float | None = None) -> dict:
    """The vendored parity score for a features.csv. Never raises."""
    try:
        from orchestrator.agents.replica.tools import parity as _parity
        rows = _parity.load(str(features_csv))
        result = _parity.combine(_parity.score(rows),
                                 _parity.visual_scores(visuals or []))
        result["rendered"] = _parity.render(result, markdown=True)
        if fail_under is not None:
            result["passed"] = result["overall"] >= fail_under
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ── screenshots ──────────────────────────────────────────────────────────────
def layout_diff(original: Path | str, clone: Path | str, mode: str = "layout",
                cols: int = 12, out: Path | str | None = None) -> dict:
    """Layout (colour-ignored) or pixel diff of two PNGs. Never raises."""
    try:
        from orchestrator.agents.replica.tools import imgdiff
        a = imgdiff.read_png(str(original))
        b = imgdiff.read_png(str(clone))
        report, diff = imgdiff.compare(a, b, mode=mode, cols=cols)
        report["files"] = {"original": str(original), "clone": str(clone)}
        if out:
            imgdiff.write_png(str(out), report["compared_at"]["width"],
                              report["compared_at"]["height"], diff)
            report["diff_image"] = str(out)
        return {"ok": True, **report}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ── reviews ──────────────────────────────────────────────────────────────────
def rank_reviews(reviews_csv: Path | str, months: int = 18,
                 today: str | None = None) -> dict:
    """Rank what users hate/want from a reviews.csv. Never raises."""
    try:
        from orchestrator.agents.replica.tools import reviews as _reviews
        themes, req = _reviews.load_themes()
        rows, dropped, dupes = _reviews.load_reviews(str(reviews_csv))
        if not rows:
            return {"ok": False,
                    "error": "no usable reviews (every row needs a url + text)",
                    "dropped": dropped, "duplicates": dupes}
        t = _reviews.parse_date(today) if today else None
        result = _reviews.analyse(rows, themes, req, today=t, months=months)
        result.update({"dropped": dropped, "duplicates": dupes})
        result["rendered"] = _reviews.render(result, dropped, dupes)
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def reviews_to_insights(result: dict) -> list[schema.ReviewInsight]:
    """Turn a rank_reviews() result into ReviewInsight objects."""
    out: list[schema.ReviewInsight] = []
    for t in (result.get("themes") or []):
        out.append(schema.ReviewInsight(
            label=t.get("label", ""),
            kind=t.get("kind", "complaint"),
            score=float(t.get("score") or 0.0),
            count=int(t.get("count") or 0),
            share_pct=float(t.get("share_pct") or 0.0),
            avg_rating=t.get("avg_rating"),
            thin=bool(t.get("thin")),
            quotes=t.get("quotes") or [],
        ))
    return out


# ── store listing ────────────────────────────────────────────────────────────
def lint_listing(listing: dict, avoid: list[str] | None = None) -> dict:
    """Play/App Store listing lint. Never raises."""
    try:
        from orchestrator.agents.replica.tools import listing as _listing
        issues = _listing.lint(listing or {}, avoid)
        errors = [i for i in issues if i["level"] == "error"]
        return {"ok": True, "issues": issues,
                "errors": len(errors), "warnings": len(issues) - len(errors),
                "rendered": _listing.render(listing or {}, issues),
                "passed": not errors}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ── contrast ─────────────────────────────────────────────────────────────────
def contrast_check(tokens: Path | str) -> dict:
    """WCAG AA check on a tokens.json. Never raises."""
    try:
        from orchestrator.agents.replica.tools import contrast
        data = json.loads(Path(tokens).read_text(encoding="utf-8"))
        cols = contrast.colours(data)
        pairs = data.get("pairs") or contrast.default_pairs(cols)
        rows = contrast.check(cols, pairs)
        fails = sum(1 for r in rows if "error" in r or not r["aa"])
        return {"ok": True, "rows": rows, "fails": fails,
                "passed": fails == 0 and bool(rows),
                "rendered": contrast.render(rows)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def render_tokens_css(tokens: dict) -> str:
    """Flatten a tokens.json into Android-friendly custom properties.

    Colours only (the ``color`` block), so the same roles map to
    ``res/values/colors.xml`` or a Compose theme.
    """
    out = [":root {"]
    color = tokens.get("color", tokens.get("colors", {}))
    if isinstance(color, dict):
        for role, value in color.items():
            if isinstance(value, str) and value.startswith("#"):
                out.append(f"  --{role}: {value};")
    out.append("}")
    return "\n".join(out) + "\n"


# ── rebrand sweep ────────────────────────────────────────────────────────────
def sweep_project(root: Path | str, avoid: list[str], domains: list[str],
                  colors: list[str], include_planning: bool = False) -> dict:
    """The vendored sweep over a project folder. Never raises."""
    try:
        from orchestrator.agents.replica.tools import sweep as _sweep
        hits = _sweep.sweep(str(root), avoid, domains, colors, include_planning)
        return {"ok": True, "hits": hits, "clean": not hits,
                "rendered": _sweep.render(hits)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
