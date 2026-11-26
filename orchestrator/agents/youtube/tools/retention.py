"""
orchestrator/agents/youtube/tools/retention.py — which scene lost the viewers.

A Python port of the core of the upstream pack's
``utils/scene-retention-engine.js`` (MIT — see ``ATTRIBUTION.md``): it takes a
real YouTube retention curve (elapsed-ratio / audience-watch-ratio points) and
the scene timeline the script was produced from, maps the curve onto the
scenes, and names the biggest drop and the biggest lift per scene — the
evidence behind "find the exact scene that lost viewers".

Upstream's own rule is kept: a curve too thin to be evidence (fewer than 10
points, or no scenes) is *not stored* — the engine returns ``None`` rather than
inventing a finding.

    from orchestrator.agents.youtube.tools import retention
    report = retention.capture(curve, {"retentionScenes": scenes,
                                       "contentFormat": "long_form"})

Pure and stdlib-only.
"""

from __future__ import annotations

MIN_POINTS = 10


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clamp(value, low, high):
    return max(low, min(high, value))


def _round(value, nd: int = 3):
    return round(float(value), nd)


def normalize_points(points: list | None) -> list[dict]:
    out = []
    for p in points or []:
        p = p if isinstance(p, dict) else {}
        out.append(
            {
                "elapsedRatio": _clamp(_num(p.get("elapsedRatio")), 0.0, 1.0),
                "audienceWatchRatio": max(0.0, _num(p.get("audienceWatchRatio"))),
                "relativeRetentionPerformance": _clamp(
                    _num(p.get("relativeRetentionPerformance")), 0.0, 1.0
                ),
                "startedWatching": max(0.0, _num(p.get("startedWatching"))),
                "stoppedWatching": max(0.0, _num(p.get("stoppedWatching"))),
                "totalSegmentImpressions": max(
                    0.0, _num(p.get("totalSegmentImpressions"))
                ),
            }
        )
    out = [p for p in out if p["elapsedRatio"] > 0]
    out.sort(key=lambda p: p["elapsedRatio"])
    deduped: list[dict] = []
    for p in out:
        if not deduped or p["elapsedRatio"] != deduped[-1]["elapsedRatio"]:
            deduped.append(p)
    return deduped


def scale_timeline(scenes: list | None, duration_seconds: float) -> list[dict]:
    scenes = scenes or []
    total = sum(
        max(0.0, _num(s.get("duration"))) for s in scenes if isinstance(s, dict)
    )
    if not total:
        return []
    scale = max(1.0, duration_seconds) / total
    cursor = 0.0
    out: list[dict] = []
    for index, scene in enumerate(scenes):
        scene = scene if isinstance(scene, dict) else {}
        duration = max(0.01, _num(scene.get("duration")) * scale)
        item = {
            "id": scene.get("id") or f"scene_{index}",
            "position": int(scene["position"])
            if str(scene.get("position", "")).lstrip("-").isdigit()
            else index,
            "label": str(scene.get("label") or f"Scene {index + 1}"),
            "startSeconds": _round(cursor, 2),
            "endSeconds": _round(min(duration_seconds, cursor + duration), 2),
            "duration": _round(duration, 2),
        }
        cursor += duration
        if index == len(scenes) - 1:
            item["endSeconds"] = _round(duration_seconds, 2)
        out.append(item)
    return out


def map_points_to_scenes(
    points: list[dict], timeline: list[dict], duration_seconds: float
) -> list[dict]:
    out: list[dict] = []
    for scene_index, scene in enumerate(timeline):
        chosen = []
        for p in points:
            second = p["elapsedRatio"] * duration_seconds
            in_range = second > scene["startSeconds"] and (
                second <= scene["endSeconds"] or scene_index == len(timeline) - 1
            )
            if in_range:
                chosen.append(p)
        if not chosen:
            continue
        first_index = points.index(chosen[0])
        preceding = points[first_index - 1] if first_index > 0 else chosen[0]
        largest_drop = max(
            0.0, preceding["audienceWatchRatio"] - chosen[0]["audienceWatchRatio"]
        )
        largest_lift = 0.0
        for i in range(1, len(chosen)):
            largest_drop = max(
                largest_drop,
                chosen[i - 1]["audienceWatchRatio"] - chosen[i]["audienceWatchRatio"],
            )
            largest_lift = max(
                largest_lift,
                chosen[i]["audienceWatchRatio"] - chosen[i - 1]["audienceWatchRatio"],
            )
        start = chosen[0]["audienceWatchRatio"]
        end = chosen[-1]["audienceWatchRatio"]
        stops = sum(p["stoppedWatching"] for p in chosen)
        impressions = sum(p["totalSegmentImpressions"] for p in chosen)
        metric = {
            **scene,
            "pointCount": len(chosen),
            "startWatchRatio": _round(start),
            "endWatchRatio": _round(end),
            "averageWatchRatio": _round(
                sum(p["audienceWatchRatio"] for p in chosen) / len(chosen)
            ),
            "averageRelativeRetention": _round(
                sum(p["relativeRetentionPerformance"] for p in chosen) / len(chosen)
            ),
            "changePoints": _round((end - start) * 100, 1),
            "largestDrop": _round(largest_drop),
            "largestLift": _round(largest_lift),
            "stoppedWatching": int(stops),
            "totalSegmentImpressions": int(impressions),
        }
        out.append(metric)
    return out


def summarize(scene_metrics: list[dict], points: list[dict]) -> dict:
    if not scene_metrics:
        return {}
    worst = min(scene_metrics, key=lambda s: s.get("changePoints", 0))
    best = max(scene_metrics, key=lambda s: s.get("changePoints", 0))
    return {
        "sceneCount": len(scene_metrics),
        "biggestDropScene": worst.get("label"),
        "biggestDropPoints": worst.get("changePoints"),
        "biggestLiftScene": best.get("label"),
        "biggestLiftPoints": best.get("changePoints"),
        "averageWatchRatio": _round(
            sum(p["audienceWatchRatio"] for p in points) / len(points)
        ),
    }


def confidence_for(points: list[dict], exposure: dict | None = None) -> str:
    impressions = sum(p["totalSegmentImpressions"] for p in points)
    if impressions >= 1000 and len(points) >= 25:
        return "high"
    if impressions >= 200 and len(points) >= 15:
        return "medium"
    if len(points) >= MIN_POINTS:
        return "low"
    return "insufficient"


def build_recommendation(snapshot: dict) -> dict | None:
    """A pending recommendation from the worst scene (never auto-applied)."""
    summary = snapshot.get("summary") or {}
    scene = summary.get("biggestDropScene")
    if not scene or (summary.get("biggestDropPoints") or 0) >= -5:
        return None
    return {
        "category": "retention",
        "title": f"Rework the scene '{scene}'",
        "rationale": (
            f"Viewers dropped {abs(summary['biggestDropPoints']):.0f} "
            f"percentage points across '{scene}' — the largest "
            f"single-scene drop in this video."
        ),
        "proposedChange": (
            f"Tighten or restructure '{scene}': shorten the "
            f"intro, front-load the payoff, and cut the segment "
            f"that loses attention."
        ),
        "confidence": snapshot.get("confidence") or "low",
        "status": "pending",
    }


def capture(
    retention_report: dict | None,
    context: dict | None = None,
    measurement_window: str = "rolling",
) -> dict | None:
    """Build the snapshot, or ``None`` when the evidence is too thin."""
    retention_report = retention_report or {}
    context = context or {}
    if retention_report.get("simulated") or retention_report.get("available") is False:
        return None
    points = normalize_points(retention_report.get("points") or [])
    scenes = context.get("retentionScenes")
    scenes = scenes if isinstance(scenes, list) else []
    if len(points) < MIN_POINTS or not scenes:
        return None
    duration = max(
        1.0,
        _num(retention_report.get("durationSeconds"))
        or _num(context.get("retentionDuration"))
        or sum(_num(s.get("duration")) for s in scenes),
    )
    timeline = scale_timeline(scenes, duration)
    scene_metrics = map_points_to_scenes(points, timeline, duration)
    if not scene_metrics:
        return None
    return {
        "videoId": retention_report.get("videoId") or "",
        "productionId": context.get("productionId"),
        "title": retention_report.get("title") or context.get("title") or "",
        "surface": "shorts" if context.get("contentFormat") == "short" else "long_form",
        "measurementWindow": measurement_window,
        "durationSeconds": duration,
        "points": [
            {**p, "elapsedSeconds": _round(p["elapsedRatio"] * duration, 2)}
            for p in points
        ],
        "sceneMetrics": scene_metrics,
        "summary": summarize(scene_metrics, points),
        "confidence": confidence_for(points),
    }


def render(snapshot: dict | None) -> str:
    if not snapshot:
        return (
            "# Scene retention\n\nNo usable retention curve (needs >= 10 "
            "real points and the scene timeline). Nothing was inferred.\n"
        )
    out = [
        "# Scene retention — where viewers leave",
        "",
        f"Video: {snapshot.get('videoId') or '—'}  "
        f"window: {snapshot.get('measurementWindow')}  "
        f"confidence: {snapshot.get('confidence')}",
        "",
        "| scene | window (s) | change (pts) | largest drop | stops |",
        "| --- | --- | --- | --- | --- |",
    ]
    for s in snapshot.get("sceneMetrics") or []:
        out.append(
            f"| {s.get('label')} | {s.get('startSeconds')}–"
            f"{s.get('endSeconds')} | {s.get('changePoints'):+.1f} | "
            f"{s.get('largestDrop')} | {s.get('stoppedWatching')} |"
        )
    summary = snapshot.get("summary") or {}
    out += [
        "",
        f"Biggest drop: **{summary.get('biggestDropScene')}** "
        f"({summary.get('biggestDropPoints')} pts). "
        f"Biggest lift: **{summary.get('biggestLiftScene')}** "
        f"({summary.get('biggestLiftPoints')} pts).",
        "",
    ]
    return "\n".join(out)
