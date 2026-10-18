"""
orchestrator/agents/youtube/tools/learning.py — the closed loop, measured.

A Python port of the core of the upstream pack's
``utils/channel-learning-engine.js`` (MIT — see ``ATTRIBUTION.md``): given a
performance report it normalises the metrics, computes a median baseline from
prior snapshots of the same surface, and reports the deltas, a confidence, and
the economics (net revenue and ROI) — so "the next move" is a number-backed
recommendation, not a hunch.

Honesty rules carried over from upstream:
  * analytics marked ``simulated`` are excluded from learning (confidence
    ``unverified``) and never move the baseline;
  * revenue/ROI are only computed when the outcome data is *available* and the
    currency matches the production cost's currency.

    from orchestrator.agents.youtube.tools import learning
    snap = learning.capture("advancedMode", report, {"videoId": "abc",
                            "strategy": {...}, "productionCost": {...}})

Pure and stdlib-only.
"""
from __future__ import annotations

from statistics import median

METRIC_KEYS = (
    "views", "impressions", "ctr", "retention", "averageViewDuration",
    "watchMinutes", "watchHours", "engagementRate", "performanceScore",
    "netSubscribers", "subscribersPerThousandImpressions", "estimatedRevenue",
    "revenuePerThousandViews", "productionCost", "netRevenue", "roi",
)


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _opt(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value, nd: int = 3):
    return None if value is None else round(float(value), nd)


def normalize_metrics(report: dict | None, context: dict | None = None) -> dict:
    """Turn a raw performance report into the flat metric set we store."""
    report = report or {}
    context = context or {}
    analytics = report.get("analytics") or {}
    views = analytics.get("views") or {}
    watch = analytics.get("watchTime") or {}
    engagement = analytics.get("engagement") or {}
    outcomes = analytics.get("outcomes") or {}
    thumbnail = report.get("thumbnailMetrics") or {}

    impressions = _num(views.get("totalImpressions", thumbnail.get("impressions")))
    total_views = _num(views.get("totalViews"))
    watch_minutes = _num(watch.get("totalWatchTime"))

    subs_available = bool(outcomes.get("subscribersAvailable"))
    rev_available = bool(outcomes.get("revenueAvailable"))
    net_subs = _opt(outcomes.get("netSubscribers")) if subs_available else None
    revenue = _opt(outcomes.get("estimatedRevenue")) if rev_available else None

    cost = context.get("productionCost") or {}
    production_cost = _opt(cost.get("amount"))
    rev_currency = (outcomes.get("currency") or None)
    cost_currency = (cost.get("currency") or None)
    currency_ok = production_cost == 0 or (
        rev_currency and cost_currency
        and str(rev_currency).upper() == str(cost_currency).upper())
    cost_complete = cost.get("complete") is True

    net_revenue = roi = None
    if (revenue is not None and production_cost is not None
            and cost_complete and currency_ok):
        net_revenue = _round(revenue - production_cost)
        if production_cost > 0:
            roi = _round((revenue - production_cost) / production_cost * 100, 1)

    return {
        "views": total_views,
        "impressions": impressions,
        "ctr": _num(thumbnail.get("clickThroughRate", views.get("averageCTR"))),
        "retention": _num(watch.get("averageViewPercentage")),
        "averageViewDuration": _num(watch.get("averageViewDuration")),
        "watchMinutes": watch_minutes,
        "watchHours": _round(watch_minutes / 60),
        "engagementRate": _num(engagement.get("engagementRate")),
        "performanceScore": _num((report.get("performance") or {}).get("score")),
        "subscribersGained": _opt(outcomes.get("subscribersGained")) if subs_available else None,
        "subscribersLost": _opt(outcomes.get("subscribersLost")) if subs_available else None,
        "netSubscribers": net_subs,
        "estimatedRevenue": revenue,
        "revenueCurrency": rev_currency,
        "monetizedPlaybacks": _opt(outcomes.get("monetizedPlaybacks")) if rev_available else None,
        "playbackBasedCpm": _opt(outcomes.get("playbackBasedCpm")) if rev_available else None,
        "subscribersPerThousandImpressions": (
            _round(net_subs / impressions * 1000) if net_subs is not None
            and impressions > 0 else None),
        "watchHoursPerThousandViews": (
            _round(watch_minutes / 60 / total_views * 1000)
            if total_views > 0 else None),
        "revenuePerThousandViews": (
            _round(revenue / total_views * 1000)
            if revenue is not None and total_views > 0 else None),
        "productionCost": production_cost,
        "costCurrency": cost_currency,
        "costComplete": cost_complete,
        "netRevenue": net_revenue,
        "roi": roi,
    }


def extract_attributes(report: dict | None, context: dict | None) -> dict:
    report = report or {}
    context = context or {}
    strategy = context.get("strategy") or {}
    script = context.get("script") or {}
    thumbnail = context.get("thumbnail") or {}
    details = report.get("videoDetails") or {}
    title = details.get("title") or context.get("title") or script.get("title") or ""
    hook = _text(script.get("hook") or script.get("introduction"))
    fmt = "shorts" if context.get("contentFormat") == "short" else \
        _slug(strategy.get("requestedStyle") or strategy.get("contentType")
              or "unknown")
    return {
        "topic": strategy.get("topic") or "",
        "pillar": strategy.get("contentPillar") or strategy.get("pillar") or "unknown",
        "surface": "shorts" if context.get("contentFormat") == "short" else "long_form",
        "format": fmt,
        "length": _slug(strategy.get("requestedLengthKey")
                        or strategy.get("requestedLength") or "unknown"),
        "hookLength": ("concise" if hook and _word_count(hook) <= 40
                       else "extended" if hook else "unknown"),
        "titleLength": ("concise" if title and _word_count(title) <= 9
                        else "long" if title else "unknown"),
        "thumbnailStyle": _slug((thumbnail.get("concept") or {}).get("composition")
                                or (thumbnail.get("concept") or {}).get("style")
                                or thumbnail.get("style") or "unknown"),
        "source": "autonomous_operator" if strategy.get("planRationale") else "manual",
    }


def baseline(prior: list[dict]) -> dict:
    """Median of each metric across prior snapshots (None when unmeasured)."""
    out = {}
    for key in METRIC_KEYS:
        values = [_opt((s.get("metrics") or {}).get(key)) for s in prior]
        values = [v for v in values if v is not None]
        out[key] = _round(median(values)) if values else None
    return out


def deltas(metrics: dict, base: dict) -> dict:
    out = {}
    for key, value in (metrics or {}).items():
        num = _opt(value)
        ref = _opt((base or {}).get(key))
        out[key] = (_round((num - ref) / abs(ref) * 100, 1)
                    if num is not None and ref not in (None, 0) else None)
    return out


def confidence_for(metrics: dict) -> str:
    views = _num((metrics or {}).get("views"))
    if views >= 1000:
        return "high"
    if views >= 100:
        return "medium"
    if views > 0:
        return "low"
    return "insufficient"


def capture(measurement_window: str, report: dict | None,
            context: dict | None = None, prior: list[dict] | None = None) -> dict:
    """Build one performance snapshot (the unit the loop stores)."""
    context = context or {}
    metrics = normalize_metrics(report, context)
    simulated = bool((report or {}).get("analytics", {}).get("simulated"))
    attrs = extract_attributes(report, context)
    peers = [s for s in (prior or [])
             if (s.get("contentAttributes") or {}).get("surface", "long_form")
             == attrs["surface"]]
    base = baseline(peers)
    return {
        "videoId": (report or {}).get("videoId") or context.get("videoId") or "",
        "productionId": context.get("productionId"),
        "measurementWindow": measurement_window,
        "publishedAt": ((report or {}).get("videoDetails") or {}).get("publishedAt")
                       or context.get("publishedAt"),
        "metrics": metrics,
        "contentAttributes": attrs,
        "baseline": base,
        "deltas": deltas(metrics, base),
        "confidence": "unverified" if simulated else confidence_for(metrics),
        "simulated": simulated,
    }


def summarize(snapshot: dict | None) -> str:
    s = snapshot or {}
    m = s.get("metrics") or {}
    d = s.get("deltas") or {}
    lines = [f"Window {s.get('measurementWindow') or '—'} "
             f"(confidence: {s.get('confidence') or '—'})",
             f"- views {m.get('views')}  impressions {m.get('impressions')}  "
             f"ctr {m.get('ctr')}%",
             f"- retention {m.get('retention')}%  "
             f"avg view {m.get('averageViewDuration')}s",
             f"- net subs {m.get('netSubscribers')}  "
             f"revenue {m.get('estimatedRevenue')}",
             f"- net revenue {m.get('netRevenue')}  roi {m.get('roi')}%"]
    if d.get("views") is not None:
        lines.append(f"- views vs baseline: {d['views']:+.1f}%")
    if s.get("simulated"):
        lines.append("- SIMULATED analytics — excluded from learning")
    return "\n".join(lines)


def _text(value) -> str:
    return " ".join(str(value or "").split())


def _word_count(text: str) -> int:
    return len(text.split())


def _slug(value) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_") or "unknown"
