"""
orchestrator/agents/replica/agents.py — the small agents of the pack.

Seven focused steps, each independently callable and each honest when a model
is unavailable (it degrades to a deterministic artifact, never a fake one):

    scout    public Play listing  → recon map (screens, flows, features)
    study    recon map            → Android architecture + feature matrix
    design   screenshots/roles    → Material 3 tokens + component specs
    critic   real reviews         → what users hate/want, in their words
    enhance  the fixes            → enhancements + brand + store listing
    builder  the plan             → an Android APK (via the coding agent)
    verify   everything           → parity + rebrand + listing gates

Every step returns ``{"stage", "ok", "note", **extras}`` and never raises.
The clean-room rules live in ``policy`` and ride in every prompt.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from orchestrator.agents.replica import policy, schema, store, toolkit
from orchestrator.agents.replica.llm import extract_json
from orchestrator.agents.replica.project import ReplicaProject

PRIORITY_HINT = {
    "must": "must",
    "should": "should",
    "could": "could",
    "p0": "must",
    "p1": "should",
    "p2": "could",
}
ANDROID_DEPS = [
    "androidx.appcompat:appcompat",
    "com.google.android.material:material",
    "androidx.constraintlayout:constraintlayout",
]


# ── small shared helpers ─────────────────────────────────────────────────────
def _norm_feature(
    row: dict, *, default_area: str = "core", original: str = "yes"
) -> schema.Feature:
    prio = str(row.get("priority") or "must").strip().lower()
    return schema.Feature(
        feature=str(row.get("feature") or row.get("name") or "").strip(),
        area=str(row.get("area") or default_area).strip(),
        priority=PRIORITY_HINT.get(prio, "could"),
        original=original,
        clone=str(row.get("clone") or "no").strip().lower() or "no",
        notes=str(row.get("notes") or "").strip(),
    ).normalised()


def _features_from_text(text: str, *, limit: int = 14) -> list[schema.Feature]:
    """Deterministic features from a Play description's bullet lines.

    Used when no model is available. Play descriptions separate feature
    bullets with newlines, '•', '·' or '-'; each surviving line becomes a
    must/should feature. Never invents features that are not in the text.
    """
    rows: list[schema.Feature] = []
    seen: set[str] = set()
    cleaned = re.sub(r"[\u2022\u00b7\u25cf\u25aa]", "\n", text or "")
    for raw in re.split(r"[\n\r]+", cleaned):
        line = raw.strip(" \t-–—*·•")
        line = re.sub(r"\s+", " ", line)
        words = line.split()
        if not (3 <= len(words) <= 18) or len(line) > 120:
            continue
        if line.endswith(":"):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        priority = "must" if len(rows) < 6 else "should"
        rows.append(
            schema.Feature(
                feature=line[:100],
                area="core",
                priority=priority,
                original="yes",
                clone="no",
            )
        )
        if len(rows) >= limit:
            break
    return rows


def measure_palette(png_paths: list[str | Path], *, limit: int = 8) -> list[dict]:
    """The dominant colours of the original's screenshots, measured.

    Deterministic and stdlib-only (reuses the vendored PNG reader). Recorded
    as *observed* so the design step can avoid the original's brand colour and
    the brand step can sweep for it — these are never copied into the app.
    """
    try:
        from orchestrator.agents.replica.tools import imgdiff
    except Exception:
        return []
    counts: dict[tuple, int] = {}
    total = 0
    for path in png_paths:
        try:
            _w, _h, rows = imgdiff.read_png(str(path))
        except Exception:
            continue
        step = max(1, len(rows) // 200)  # sample ~200 rows per shot
        for row in rows[::step]:
            for r, g, b in row[:: max(1, len(row) // 200)]:
                q = (r // 16 * 16, g // 16 * 16, b // 16 * 16)
                counts[q] = counts.get(q, 0) + 1
                total += 1
    if not total:
        return []
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    out: list[dict] = []
    for (r, g, b), n in ranked:
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        if lum > 245 or lum < 12:  # skip pure white/black chrome
            continue
        out.append(
            {"hex": "#%02X%02X%02X" % (r, g, b), "share": round(100.0 * n / total, 1)}
        )
        if len(out) >= limit:
            break
    return out


def _write_bundle(project: ReplicaProject, recon: schema.ReconMap) -> None:
    project.save_recon(recon)


# ── SCOUT ────────────────────────────────────────────────────────────────────
SCOUT_SYSTEM = (
    "You are the JARVIS replica pack's recon (scout) agent. You describe what "
    "an app DOES from its public Play Store listing, so a clean-room rebuild "
    "can match the behaviour. You never claim to have decompiled anything. "
    "Answer with ONE JSON object only.\n" + policy.clean_room_banner()
)
SCOUT_PROMPT = """Reverse-engineer this Android app from its PUBLIC Play Store
listing into a recon map a clean-room rebuild can follow.

LISTING:
{listing}

Return ONE JSON object, nothing else:
{{
  "core_loop": "one sentence: the thing users come back for",
  "screens": [{{"id":"S01","name":"Home","reach":"app open","purpose":"...","components":"...","states":"empty, filled, error"}}],
  "flows": [{{"id":"F01","goal":"...","screens":["S01","S02"],"happy_clicks":3,"edge":"..."}}],
  "entities": [{{"name":"Item","fields":"id, title, done:bool","evidence":"S01","confidence":"medium"}}],
  "features": [{{"feature":"add an item","area":"core","priority":"must"}}],
  "out_of_scope": ["licensed content we should not rebuild"]
}}
Rules: infer ONLY what the listing supports; mark guesses with confidence
"guess". 4-10 screens, 2-6 flows, 5-15 features. Priorities must be
must/should/could. Do not describe copied assets or code — behaviour only.
"""


class ScoutStep:
    name = "recon"
    role = "reverse-engineer the app from its public Play listing"

    def run(
        self,
        project: ReplicaProject,
        llm,
        *,
        listing: dict | None = None,
        listing_url: str = "",
        source_text: str = "",
        **_,
    ) -> dict:
        project.ensure()
        url = listing_url or project.listing_url or ""
        app_name = project.app_name

        if listing is None and url:
            check = policy.assert_source_allowed(url)
            if not check.allowed:
                return {"stage": self.name, "ok": False, "note": check.reason}
            listing = store.fetch_listing(url)

        if listing is None:
            saved = project.read(project.source_listing_json)
            if saved:
                try:
                    listing = json.loads(saved)
                except Exception:
                    listing = None

        listing = listing or {}
        if listing.get("url"):
            url = listing["url"]
        if listing.get("name"):
            app_name = listing["name"]
        project.app_name = app_name or project.app_name
        project.listing_url = url or project.listing_url
        if listing.get("package"):
            project.package = listing["package"]

        # Persist the raw listing so a run is reproducible offline.
        if listing.get("ok") or listing.get("name"):
            project.write(
                project.source_listing_json, json.dumps(listing, indent=1, default=str)
            )

        desc = store.listing_to_description(listing)
        if source_text:
            desc = (desc + "\n\n" + source_text).strip()

        recon = schema.ReconMap(
            app=app_name or "unknown app",
            platform="android",
            package=project.package or listing.get("package", ""),
            listing_url=url,
            category=listing.get("category", ""),
            rating=listing.get("rating"),
            installs=listing.get("installs", ""),
            description=(listing.get("description") or "")[:8000],
            whats_new=(listing.get("whats_new") or "")[:2000],
            screenshots=list(listing.get("screenshots") or []),
        )
        recon.sources = [
            schema.Source(
                "Google Play listing", url or "—", "public listing; no APK used"
            )
        ]

        # Reference screenshots (never shipped) — best effort, network only.
        if listing.get("screenshots") and not any(project.screens_dir.glob("*.png")):
            store.download_screenshots(
                listing.get("screenshots"), project.screens_dir, limit=6
            )

        # ── deterministic baseline ───────────────────────────────────────
        recon.features = _features_from_text(listing.get("description", ""))
        if not recon.features:
            recon.features = [
                schema.Feature("Use the app's main function", "core", "must"),
                schema.Feature("View and manage saved items", "core", "must"),
                schema.Feature("Change basic settings", "settings", "should"),
            ]
        recon.screens = [
            schema.Screen(
                "S01",
                "Home",
                "app open",
                "the app's main function",
                "list, actions",
                "empty, filled, error",
            ),
            schema.Screen(
                "S02",
                "Settings",
                "menu",
                "preferences",
                "switches, fields",
                "default, changed",
            ),
        ]
        recon.flows = [
            schema.Flow(
                "F01",
                "the app's core loop",
                ["S01", "S01"],
                2,
                "empty state on first run",
            )
        ]
        recon.core_loop = "use the app's main function, then return to it"

        # ── model enrichment (optional) ──────────────────────────────────
        used_llm = False
        if desc:
            raw = (
                llm.text(SCOUT_PROMPT.format(listing=desc[:6000]), system=SCOUT_SYSTEM)
                if llm
                else None
            )
            data = extract_json(raw or "")
            if data:
                used_llm = True
                if data.get("core_loop"):
                    recon.core_loop = str(data["core_loop"])[:300]
                screens = [
                    s
                    for s in (data.get("screens") or [])
                    if isinstance(s, dict) and s.get("name")
                ]
                if screens:
                    recon.screens = [
                        schema.Screen(
                            id=str(s.get("id") or f"S{i:02d}"),
                            name=str(s["name"]),
                            reach=str(s.get("reach") or ""),
                            purpose=str(s.get("purpose") or ""),
                            components=str(s.get("components") or ""),
                            states=str(s.get("states") or "empty, filled, error"),
                        )
                        for i, s in enumerate(screens, 1)
                    ]
                flows = [
                    f
                    for f in (data.get("flows") or [])
                    if isinstance(f, dict) and f.get("goal")
                ]
                if flows:
                    recon.flows = [
                        schema.Flow(
                            id=str(f.get("id") or f"F{i:02d}"),
                            goal=str(f["goal"]),
                            screens=[str(x) for x in (f.get("screens") or [])],
                            happy_clicks=int(f.get("happy_clicks") or 0),
                            edge=str(f.get("edge") or ""),
                        )
                        for i, f in enumerate(flows, 1)
                    ]
                ents = [
                    e
                    for e in (data.get("entities") or [])
                    if isinstance(e, dict) and e.get("name")
                ]
                if ents:
                    recon.entities = [
                        schema.Entity(
                            name=str(e["name"]),
                            fields=str(e.get("fields") or ""),
                            evidence=str(e.get("evidence") or ""),
                            confidence=str(e.get("confidence") or "guess"),
                        )
                        for e in ents
                    ]
                feats = [
                    f
                    for f in (data.get("features") or [])
                    if isinstance(f, dict) and (f.get("feature") or f.get("name"))
                ]
                if feats:
                    recon.features = [_norm_feature(f) for f in feats]
                out = [
                    str(x) for x in (data.get("out_of_scope") or []) if str(x).strip()
                ]
                if out:
                    recon.out_of_scope = out

        recon.features = [f.normalised() for f in recon.features]
        _write_bundle(project, recon)
        project.mark(
            self.name,
            True,
            f"{len(recon.screens)} screens, {len(recon.features)} features"
            + ("" if used_llm else " (deterministic baseline)"),
        )
        return {
            "stage": self.name,
            "ok": True,
            "recon": recon,
            "note": f"recon map written ({len(recon.screens)} screens, "
            f"{len(recon.features)} features"
            + ("" if used_llm else ", no model — baseline only")
            + ")",
            "listing_ok": bool(listing.get("ok")),
            "listing_error": listing.get("error", ""),
        }


# ── STUDY ────────────────────────────────────────────────────────────────────
STUDY_SYSTEM = (
    "You are the JARVIS replica pack's architect. You plan boring, managed "
    "Android technology and a real data model from a recon map. Answer with "
    "ONE JSON object only.\n" + policy.clean_room_banner()
)
STUDY_PROMPT = """Plan the Android architecture for a clean-room rebuild of
this app. Recon map:

{recon}

Return ONE JSON object:
{{
  "dependencies": ["androidx.room:room-runtime"],
  "entities": [{{"name":"Item","fields":"id(Int, PK), title(String), done(Boolean)","confidence":"medium"}}],
  "notes": "one or two sentences on the hard parts"
}}
Rules: Kotlin; prefer androidx + Jetpack; no third-party SDK that would need
the original's keys. Only the app's own data model — no partner APIs.
"""


class StudyStep:
    name = "architecture"
    role = "plan the Android stack, data model and build order"

    def run(self, project: ReplicaProject, llm, **_) -> dict:
        project.ensure()
        recon = project.load_recon()
        stats = toolkit.feature_stats(recon.features)
        missing = toolkit.missing_in_build_order(recon.features)

        deps = list(ANDROID_DEPS)
        used_llm = False
        note = ""
        if recon.screens or recon.entities:
            raw = (
                llm.text(
                    STUDY_PROMPT.format(recon=recon.render_recon_md()[:6000]),
                    system=STUDY_SYSTEM,
                )
                if llm
                else None
            )
            data = extract_json(raw or "")
            if data:
                used_llm = True
                for d in data.get("dependencies") or []:
                    d = str(d).strip()
                    if d and d not in deps:
                        deps.append(d)
                ents = [
                    e
                    for e in (data.get("entities") or [])
                    if isinstance(e, dict) and e.get("name")
                ]
                if ents:
                    recon.entities = [
                        schema.Entity(
                            name=str(e["name"]),
                            fields=str(e.get("fields") or ""),
                            evidence=str(e.get("evidence") or "planned"),
                            confidence=str(e.get("confidence") or "medium"),
                        )
                        for e in ents
                    ]
                    project.save_recon(recon)
                note = str(data.get("notes") or "")[:400]

        md = _render_architecture(recon, deps, missing, stats, note)
        project.write(project.architecture_md, md)
        project.write(project.features_csv, recon.render_features_csv())
        # The app spec the builder will hand to the coding agent.
        project.state["app_spec"] = _render_app_spec(recon)
        project.save_state()
        project.mark(
            self.name,
            True,
            f"{len(deps)} deps, {stats['must_total']} must-haves"
            + ("" if used_llm else " (deterministic)"),
        )
        return {
            "stage": self.name,
            "ok": True,
            "note": note or f"architecture written; {len(missing)} feature(s) to build",
            "dependencies": deps,
            "stats": stats,
            "missing": [f.feature for f in missing],
        }


def _render_architecture(
    recon: schema.ReconMap,
    deps: list[str],
    missing: list[schema.Feature],
    stats: dict,
    note: str,
) -> str:
    out = [
        f"# Architecture — a clean-room rebuild of {recon.app}",
        "",
        f"Platform: Android (Kotlin). Scope: {recon.scope}. " f"Size: {recon.size()}.",
        "",
    ]
    out += [
        "## Stack",
        "",
        "| layer | choice | why |",
        "| --- | --- | --- |",
        "| UI | Kotlin + XML views (androidx) | builds with the existing "
        "Gradle scaffold; Compose is a drop-in later |",
        "| state | ViewModel + LiveData | no extra deps, lifecycle-safe |",
        "| storage | Room (local) | offline-first, matches most store apps |",
        "| background | WorkManager | reminders/sync without a server |",
        "| payments | Google Play Billing | if the original charges in-app |",
        "| prefs | DataStore / SharedPreferences | settings screen |",
        "",
    ]
    out += ["## Dependencies", ""]
    out += [f"- `{d}`" for d in deps] or ["- (none)"]
    out.append("")
    if note:
        out += ["## Notes", "", note, ""]
    out += ["## Data model (Room)", "", "```"]
    if recon.entities:
        for e in recon.entities:
            out.append(f"{e.name}  {e.fields}   [confidence: {e.confidence}]")
    else:
        out.append("(inferred from the listing; refine as screens are built)")
    out.append("```")
    out.append("")
    out += ["## Screens → activities/fragments", ""]
    for s in recon.screens:
        out.append(
            f"- {s.id} `{schema.slug(s.name, fallback='screen')}` — "
            f"{s.purpose or s.name}"
        )
    out.append("")
    out += ["## Build order (thin vertical slice first)", ""]
    if missing:
        for i, f in enumerate(missing, 1):
            out.append(f"{i}. [{f.priority}] {f.area}: {f.feature}")
    else:
        out.append("- every counted feature is already marked done")
    out.append("")
    out += [
        "## Feature matrix",
        "",
        f"Must-haves: {stats['must_done']}/{stats['must_total']} done. "
        f"Enhancements beyond the original: {stats['extras']}.",
        "",
    ]
    out += ["## Out of scope", ""]
    for item in recon.out_of_scope or ["licensed content and the app's branded assets"]:
        out.append(f"- {item}")
    out.append("")
    return "\n".join(out)


def _render_app_spec(recon: schema.ReconMap) -> str:
    """A buildable description the coding agent turns into an APK."""
    lines = [
        f"Build an Android app called '{recon.app}' (clean-room rebuild).",
        f"It does: {recon.core_loop or recon.description[:200]}",
        "",
        "Screens:",
    ]
    for s in recon.screens:
        lines.append(f"- {s.name}: {s.purpose} ({s.components})")
    lines.append("")
    lines.append("Must-have features:")
    for f in recon.features:
        if f.priority == "must":
            lines.append(f"- {f.feature}")
    extras = [f for f in recon.features if f.original == "no"]
    if extras:
        lines.append("")
        lines.append("Improvements over the original (do these too):")
        for f in extras:
            lines.append(f"- {f.feature}")
    lines.append("")
    lines.append(policy.clean_room_banner())
    return "\n".join(lines)


# ── DESIGN ───────────────────────────────────────────────────────────────────
DEFAULT_TOKENS = {
    "_about": "Design tokens for a clean-room rebuild. Role names mirror the "
    "upstream Replica pack; values are neutral placeholders. The "
    "original's brand colour is recorded under 'measured' but never "
    "used — the enhance step picks your own palette.",
    "color": {
        "bg": "#FFFFFF",
        "surface": "#F5F5F7",
        "border": "#D1D1D6",
        "text": "#1C1C1E",
        "text-muted": "#6E6E73",
        "accent": "#4F46E5",
        "on-accent": "#FFFFFF",
        "danger": "#C81E1E",
        "success": "#15803D",
        "warning": "#9A3412",
    },
    "radius": {"sm": "4dp", "md": "8dp", "lg": "16dp"},
    "spacing": {"xs": "4dp", "sm": "8dp", "md": "16dp", "lg": "24dp"},
    "pairs": [
        ["text", "bg", "normal"],
        ["text", "surface", "normal"],
        ["text-muted", "bg", "normal"],
        ["text-muted", "surface", "normal"],
        ["on-accent", "accent", "normal"],
        ["text", "bg", "large"],
        ["danger", "bg", "large"],
        ["success", "bg", "large"],
        ["warning", "bg", "large"],
    ],
}


class DesignStep:
    name = "design"
    role = "rebuild the design system as tokens + component specs"

    def run(self, project: ReplicaProject, llm, **_) -> dict:
        project.ensure()
        recon = project.load_recon()

        measured = measure_palette(sorted(project.screens_dir.glob("*.png")))
        tokens = json.loads(json.dumps(DEFAULT_TOKENS))
        if measured:
            tokens["measured"] = {
                "_about": "The original's dominant screenshot colours — "
                "observed only. Do NOT ship these; the enhance step "
                "replaces the palette and sweeps for these values.",
                "colors": measured,
            }
        project.write(project.tokens_json, json.dumps(tokens, indent=2))

        project.write(project.components_md, _render_components(recon, measured))
        check = toolkit.contrast_check(project.tokens_json)
        project.mark(
            self.name,
            check.get("passed", False),
            f"contrast {'ok' if check.get('passed') else 'FAILING'}"
            + (f", {len(measured)} measured colours" if measured else ""),
        )
        return {
            "stage": self.name,
            "ok": True,
            "note": (
                f"tokens + component specs written; contrast "
                f"{'passes AA' if check.get('passed') else 'FAILS AA'}"
            ),
            "measured": measured,
            "contrast": check,
        }


def _render_components(recon: schema.ReconMap, measured: list[dict]) -> str:
    out = [
        f"# Component specs — {recon.app} (rebuild)",
        "",
        "Material 3 roles. Every label is written fresh; no original "
        "artwork, icon or copy is used. Build these primitives before any "
        "screen.",
        "",
    ]
    specs = [
        (
            "Button",
            "filled (primary), tonal (secondary), outlined, text",
            "40dp min height, radius md",
            "default, pressed, focus, disabled, loading",
        ),
        (
            "TextField",
            "filled, outlined",
            "56dp, supporting text below",
            "empty, focused, error, disabled",
        ),
        (
            "ListItem",
            "one-line, two-line, with trailing action",
            "72dp, 16dp horizontal padding",
            "default, pressed, selected",
        ),
        ("Card", "elevated, outlined", "radius lg, 1dp elevation", "default, pressed"),
        (
            "BottomNavigation",
            "3-5 destinations",
            "80dp, active/inactive",
            "active, inactive",
        ),
        (
            "Dialog",
            "alert, confirmation",
            "radius lg, max width 560dp",
            "default, destructive action",
        ),
        ("Snackbar", "message, action", "bottom, above nav", "default, action"),
        ("Progress", "linear, circular", "accent colour", "determinate, indeterminate"),
    ]
    for name, variants, size, states in specs:
        out += [
            f"## {name}",
            "",
            f"- variants: {variants}",
            f"- size: {size}",
            f"- states: {states}",
            "- tokens: bg surface, text text, accent accent, radius md",
            "- a11y: real semantic control, visible focus, content "
            "descriptions on every icon",
            "",
        ]
    if measured:
        out += [
            "## Observed colours (reference only)",
            "",
            "Measured from the original's public screenshots. These are "
            "shown so you can make your palette clearly different — they "
            "are never shipped.",
            "",
        ]
        for m in measured:
            out.append(f"- `{m['hex']}`  ({m['share']}% of sampled pixels)")
        out.append("")
    return "\n".join(out)


# ── CRITIC ───────────────────────────────────────────────────────────────────
CRITIC_SYSTEM = (
    "You are the JARVIS replica pack's critic. You turn REAL user reviews into "
    "a ranked fix plan for the rebuild. You never invent a review, quote, "
    "rating or source. Answer with ONE JSON object only.\n" + policy.clean_room_banner()
)
CRITIC_PROMPT = """Here is the ranked feedback from real reviews of the app
being rebuilt. For each theme, write a concrete fix for OUR rebuild.

FEEDBACK (ranked):
{feedback}

Return ONE JSON object:
{{"fixes": [{{"theme": "<theme label exactly as above>",
             "plan": "what our rebuild does differently, 1-2 sentences"}}]}}
"""


class CriticStep:
    name = "critic"
    role = "read what real users hate and want, rank it"

    def run(self, project: ReplicaProject, llm, **_) -> dict:
        project.ensure()
        # Build reviews.csv from the listing if the user has not supplied one.
        if not project.reviews_csv.exists():
            # The user may drop reviews.csv next to the project folder too.
            beside = project.root.parent / "reviews.csv"
            if beside.is_file():
                import shutil

                project.root.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(beside, project.reviews_csv)
        if not project.reviews_csv.exists():
            recon = project.load_recon()
            if recon.reviews:
                _write_reviews_csv(project.reviews_csv, recon.reviews)

        if not project.reviews_csv.exists():
            project.write(
                project.feedback_md,
                "# What users hate and want\n\n"
                "No reviews collected yet. Save real reviews to "
                "`replica/reviews.csv` (columns: source,url,date,"
                "rating,text) and re-run the critic. Nothing is "
                "ranked without real quotes.\n",
            )
            project.write(
                project.fixes_md,
                "# Fix plan\n\n_(no reviews yet — "
                "the enhancement pass has nothing "
                "to fold in)_\n",
            )
            project.mark(self.name, True, "skipped: no reviews.csv")
            return {
                "stage": self.name,
                "ok": True,
                "ranked": False,
                "note": "no reviews.csv — skipped (never invents reviews)",
                "insights": [],
                "fixes": [],
            }

        result = toolkit.rank_reviews(project.reviews_csv)
        if not result.get("ok"):
            project.mark(self.name, False, result.get("error", "rank failed"))
            return {
                "stage": self.name,
                "ok": False,
                "note": result.get("error", "review ranking failed"),
                "insights": [],
                "fixes": [],
            }

        project.write(project.feedback_md, result.get("rendered", ""))
        insights = toolkit.reviews_to_insights(result)

        # Model writes the fix prose; deterministic fallback keeps the plan.
        plans: dict[str, str] = {}
        if insights:
            raw = (
                llm.text(
                    CRITIC_PROMPT.format(feedback=result.get("rendered", "")[:4000]),
                    system=CRITIC_SYSTEM,
                )
                if llm
                else None
            )
            data = extract_json(raw or "")
            for fx in (data or {}).get("fixes") or []:
                if isinstance(fx, dict) and fx.get("theme"):
                    plans[str(fx["theme"])] = str(fx.get("plan") or "")

        fixes: list[schema.Fix] = []
        for ins in insights[:8]:
            plan = plans.get(ins.label) or _default_fix_plan(ins)
            urls = [q.get("url", "") for q in ins.quotes if q.get("url")]
            fixes.append(
                schema.Fix(
                    title=ins.label,
                    from_theme=ins.label,
                    kind=ins.kind,
                    evidence_urls=urls,
                    plan=plan,
                )
            )
        project.write(project.fixes_md, _render_fixes(fixes, insights))
        project.mark(
            self.name,
            True,
            f"{len(insights)} themes, {result.get('reviews', 0)} reviews",
        )
        return {
            "stage": self.name,
            "ok": True,
            "note": f"ranked {result.get('reviews', 0)} reviews into "
            f"{len(insights)} themes",
            "insights": insights,
            "fixes": fixes,
        }


def _default_fix_plan(ins: schema.ReviewInsight) -> str:
    if ins.kind == "request":
        return (
            f"Our rebuild ships {ins.label.lower()} natively, so nobody "
            f"has to ask for it."
        )
    return (
        f"Our rebuild fixes {ins.label.lower()} by design — the flow is "
        f"simplified and the failure state is handled explicitly."
    )


def _render_fixes(fixes: list[schema.Fix], insights: list[schema.ReviewInsight]) -> str:
    out = [
        "# Fix plan — what the original's users hate and want",
        "",
        "Every line below points at a real review. Our rebuild does these "
        "better; that is the reason it exists.",
        "",
    ]
    if not insights:
        out.append("_(nothing ranked yet)_")
        return "\n".join(out) + "\n"
    complaints = [f for f in fixes if f.kind == "complaint"]
    requests = [f for f in fixes if f.kind != "complaint"]
    for title, group in (
        ("What they hate — and how ours fixes it", complaints),
        ("What they ask for — and ours ships it", requests),
    ):
        out += [f"## {title}", ""]
        if not group:
            out.append("_(none)_")
            out.append("")
            continue
        for f in group:
            out.append(f"### {f.title}")
            out.append(f"- Plan: {f.plan}")
            if f.evidence_urls:
                out.append(f"- Evidence: {', '.join(f.evidence_urls[:3])}")
            out.append("")
    return "\n".join(out) + "\n"


def _write_reviews_csv(path: Path, reviews: list[schema.Review]) -> None:
    import csv as _csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(["source", "url", "date", "rating", "text"])
        for r in reviews:
            w.writerow([r.source, r.url, r.date, r.rating, r.text])


# ── ENHANCE ──────────────────────────────────────────────────────────────────
ENHANCE_SYSTEM = (
    "You are the JARVIS replica pack's entrepreneur. You name the rebuilt app "
    "and write its store listing so it is the user's own product — never the "
    "original's identity. Answer with ONE JSON object only.\n"
    + policy.clean_room_banner()
)
ENHANCE_PROMPT = """Name a clean-room rebuild of an Android app and write its
Google Play listing. The rebuild's edge: it fixes what the original's users
hate.

APP: {app}
WHAT IT DOES: {core_loop}
THE EDGE (fixes over the original): {fixes}

Return ONE JSON object:
{{
  "names": ["5 candidate names, none similar to the original's in sound, "
            "spelling or meaning"],
  "title": "store title (<=30 chars, no ranking claims, no emoji)",
  "short_description": "<=80 chars, no ranking claims",
  "full_description": "<=4000 chars, describes the app and its improvements, "
                      "no mention of the original's name"
}}
"""


class EnhanceStep:
    name = "enhance"
    role = "fold the fixes in, name it, brand it, write the listing"

    def run(
        self, project: ReplicaProject, llm, *, fixes: list | None = None, **_
    ) -> dict:
        project.ensure()
        recon = project.load_recon()
        if fixes is None:
            fixes = _fixes_from_reviews(project)

        # 1. enhancements become feature-matrix extras (original="no").
        extra_feats = []
        for f in fixes:
            extra_feats.append(
                schema.Feature(
                    feature=f"Improve: {f.title}",
                    area="enhancement",
                    priority="should",
                    original="no",
                    clone="yes",
                    notes=f.plan[:160],
                )
            )
        existing = {fe.feature.lower() for fe in recon.features}
        for ef in extra_feats:
            if ef.feature.lower() not in existing:
                recon.features.append(ef)
        project.write(project.features_csv, recon.render_features_csv())
        project.save_recon(recon)

        # 2. brand: avoid-list = the original's identity; palette = measured.
        measured = []
        if project.tokens_json.exists():
            try:
                measured = [
                    c["hex"]
                    for c in (
                        json.loads(project.tokens_json.read_text(encoding="utf-8"))
                        .get("measured", {})
                        .get("colors", [])
                    )
                ]
            except Exception:
                measured = []
        avoid = [recon.app] if recon.app else []
        brand = {"avoid": avoid, "domains": [], "colors": measured}
        project.write(project.brand_json, json.dumps(brand, indent=2))

        # 3. name + listing (model, else deterministic).
        edge = "; ".join(f.title for f in fixes[:5]) or "a cleaner, faster rebuild"
        used_llm = False
        names, title, short, full = _fallback_listing(recon, edge, avoid)
        raw = None
        if llm:
            raw = llm.text(
                ENHANCE_PROMPT.format(
                    app=recon.app,
                    core_loop=recon.core_loop or recon.description[:200],
                    fixes=edge[:800],
                ),
                system=ENHANCE_SYSTEM,
            )
        data = extract_json(raw or "")
        if data:
            used_llm = True
            names = [str(n) for n in (data.get("names") or names) if str(n).strip()][
                :5
            ] or names
            title = str(data.get("title") or title)[:30]
            short = str(data.get("short_description") or short)[:80]
            full = str(data.get("full_description") or full)[:4000]
        # Whatever the source, never let the original's name through.
        for term in avoid:
            if not term:
                continue
            title = re.sub(re.escape(term), "", title, flags=re.I).strip()
            short = re.sub(re.escape(term), "", short, flags=re.I)
            full = re.sub(re.escape(term), "the app", full, flags=re.I)
            names = [re.sub(re.escape(term), "", n, flags=re.I).strip() for n in names]
        names = [n for n in names if n] or ["Everyday Companion"]
        title = title or names[0][:30]

        project.write(project.brand_md, _render_brand(recon, names, brand, measured))
        listing = {
            "avoid": avoid,
            "google_play": {
                "title": title,
                "short_description": short,
                "full_description": full,
            },
        }
        project.write(project.listing_json, json.dumps(listing, indent=2))
        lint = toolkit.lint_listing(listing, avoid=avoid)

        project.write(
            project.positioning_md, _render_positioning(recon, fixes, names, edge)
        )
        project.mark(
            self.name, True, f"{len(extra_feats)} enhancements, {len(names)} names"
        )
        return {
            "stage": self.name,
            "ok": True,
            "note": (
                f"{len(extra_feats)} enhancement(s) folded in; "
                f"{len(names)} name candidate(s); listing "
                f"{'lints clean' if lint.get('passed') else 'has issues'}"
            ),
            "enhancements": [f.feature for f in extra_feats],
            "names": names,
            "brand": brand,
            "listing": listing,
            "lint": lint,
            "name_title": title,
        }


def _fixes_from_reviews(project: ReplicaProject) -> list[schema.Fix]:
    if not project.reviews_csv.exists():
        return []
    result = toolkit.rank_reviews(project.reviews_csv)
    if not result.get("ok"):
        return []
    return [
        schema.Fix(
            title=i.label,
            from_theme=i.label,
            kind=i.kind,
            evidence_urls=[q.get("url") for q in i.quotes if q.get("url")],
            plan=_default_fix_plan(i),
        )
        for i in toolkit.reviews_to_insights(result)[:8]
    ]


def _fallback_listing(recon: schema.ReconMap, edge: str, avoid: list[str]) -> tuple:
    """A deterministic name + listing that carries none of the original's
    identity (the lint would reject it otherwise)."""
    blocked = {w.lower() for a in avoid for w in re.findall(r"[A-Za-z]{4,}", a)}
    blocked |= {re.sub(r"[^A-Za-z]", "", a).lower() for a in avoid if a}

    def safe(text: str) -> str:
        out = text
        for a in avoid:
            if a:
                out = re.sub(re.escape(a), "the app", out, flags=re.I)
        return out

    stems = [
        w
        for w in re.findall(r"[A-Za-z]{4,}", recon.app or "")
        if w.lower() not in blocked and not any(w.lower() in b for b in blocked)
    ]
    cat = [
        w
        for w in re.findall(r"[A-Za-z]{4,}", recon.category or "")
        if w.lower() not in blocked
    ]
    primary = (stems or cat or ["Everyday"])[0]
    names = [
        f"{primary} Companion",
        f"{primary} Plus",
        f"Everyday {primary}",
        f"{primary} Studio",
        f"{primary} Rebuilt",
    ]
    names = [n for n in names if safe(n) == n][:5] or ["Everyday Companion"]
    title = safe(names[0])[:30]
    short = ("The app you wanted: simpler, cleaner, and it keeps its promises.")[:80]
    improvements = [x for x in edge.split("; ") if x]
    full = safe(
        f"{title} — an independent clean-room rebuild, improved.\n\n"
        f"{recon.core_loop or 'The app you know, done better.'}\n\n"
        "What is better:\n" + "\n".join(f"- {x}" for x in improvements) + "\n\n"
        "Rebuilt from scratch: no original code, assets or branding."
    )
    return names, title, short, full[:4000]


def _render_brand(
    recon: schema.ReconMap, names: list[str], brand: dict, measured: list[str]
) -> str:
    out = [
        f"# Brand — your own app, not {recon.app}",
        "",
        "Nothing launches under the original's identity. Legal checks "
        "below are screening checks, not clearance.",
        "",
        "## Name candidates",
        "",
    ]
    for i, n in enumerate(names, 1):
        out.append(f"{i}. **{n}**")
    out += [
        "",
        "## Checks to run (mark each, never assume 'available')",
        "",
        "| check | where |",
        "| --- | --- |",
        "| US trademark | tmsearch.uspto.gov (classes 9 and 42) |",
        "| EU trademark | euipo.europa.eu eSearch / TMview |",
        "| domain | `whois <name>.com` or a registrar search |",
        "| Play / App Store | search the exact name |",
        "| handles | X, Instagram, TikTok, GitHub |",
        '| the web | search "<name> + <category>" |',
        "",
        "## Palette",
        "",
        "The rebuild uses the neutral token palette with its own accent "
        "(see `replica/design/tokens.json`). Set the original's colours in "
        "`replica/brand.json` so the sweep finds any that survive:",
        "",
    ]
    for c in measured or ["(none measured — add the original's brand colour)"]:
        out.append(f"- `{c}`")
    out += [
        "",
        "## Sweep",
        "",
        "Before shipping:",
        "",
        "```bash",
        "python3 orchestrator/agents/replica/tools/sweep.py . "
        "--config replica/brand.json",
        "```",
        "",
        "Exit 0 means clean. The verify step runs it for you.",
        "",
    ]
    return "\n".join(out)


def _render_positioning(
    recon: schema.ReconMap, fixes: list[schema.Fix], names: list[str], edge: str
) -> str:
    out = [
        "# Positioning — why this rebuild exists",
        "",
        f"A straight copy of {recon.app} would have no reason to be. This "
        f"one fixes what its users complain about.",
        "",
        "## The angle",
        "",
        edge,
        "",
        "## The fixes that make it better",
        "",
    ]
    for f in fixes:
        out.append(f"- **{f.title}** — {f.plan}")
    if not fixes:
        out.append("- (collect reviews to fill this in)")
    out += ["", "## Candidate names", "", ", ".join(names) or "(none)"]
    return "\n".join(out) + "\n"


# ── BUILDER ──────────────────────────────────────────────────────────────────
class BuilderStep:
    name = "build"
    role = "hand the plan to the coding agent and get an APK"

    def run(self, project: ReplicaProject, llm, *, build: bool = True, **_) -> dict:
        project.ensure()
        recon = project.load_recon()
        spec = project.state.get("app_spec") or _render_app_spec(recon)
        app_name = _app_folder_name(recon)

        if not build:
            project.write(
                project.build_log_md,
                "# Build log\n\nPlan only (build=False). The app spec "
                "the builder would use is in `state.json:app_spec`.\n",
            )
            project.mark(self.name, True, "planned (build disabled)")
            return {
                "stage": self.name,
                "ok": True,
                "built": False,
                "note": "build disabled — spec ready",
                "spec": spec,
            }

        try:
            import importlib

            dev = importlib.import_module("actions.dev_agent")
        except Exception as exc:
            project.write(
                project.build_log_md,
                f"# Build log\n\nThe coding agent is unavailable: "
                f"{exc}\n\nSpec saved in `state.json:app_spec`.\n",
            )
            project.mark(self.name, False, f"dev_agent unavailable: {exc}")
            return {
                "stage": self.name,
                "ok": False,
                "built": False,
                "note": f"coding agent unavailable: {exc}",
            }

        out = dev.dev_agent(
            {"description": spec, "language": "android", "project_name": app_name}
        )
        built = bool(re.search(r"APK", out or "", re.I)) and not re.search(
            r"(failed|error|could not)", out or "", re.I
        )
        marked = 0
        if built:
            # The build ticked the feature matrix: the features in the spec are
            # now IN the app. Marked "partial" (built, not yet compared against
            # the original) — never "yes", which only a parity check earns.
            for f in recon.features:
                if (
                    f.original != "no"
                    and f.clone == "no"
                    and f.priority in ("must", "should")
                ):
                    f.clone = "partial"
                    marked += 1
            project.write(project.features_csv, recon.render_features_csv())
            project.save_recon(recon)
        project.write(
            project.build_log_md, f"# Build log\n\nApp: {app_name}\n\n{out}\n"
        )
        # Prefer the real project dir the coding agent reported.
        m = re.search(r"(/[^\s\"']*(?:Desktop|JarvisProjects)/[^\s\"']+)", out or "")
        if m:
            project.state["project_dir"] = m.group(1)
            project.save_state()
        project.mark(self.name, built, (out or "")[:160])
        return {
            "stage": self.name,
            "ok": built,
            "built": built,
            "features_marked_partial": marked,
            "note": (out or "")[:400],
            "project_dir": project.state.get("project_dir", ""),
        }


def _app_folder_name(recon: schema.ReconMap) -> str:
    base = re.sub(r"[^\w\-]", "_", (recon.app or "replica_app")).strip("_")
    return f"{base}_Rebuild"[:40] or "replica_app"


# ── VERIFY ───────────────────────────────────────────────────────────────────
class VerifyStep:
    name = "verify"
    role = "parity + rebrand sweep + listing lint + contrast"

    def run(
        self,
        project: ReplicaProject,
        llm,
        *,
        project_dir: str = "",
        fail_under: float = 80.0,
        **_,
    ) -> dict:
        project.ensure()
        recon = project.load_recon()

        parity = toolkit.parity_score(project.features_csv, fail_under=fail_under)

        # Sweep the built app (if any), else the replica folder's parent.
        sweep_root = (
            project_dir or project.state.get("project_dir") or str(project.root.parent)
        )
        brand = {}
        try:
            brand = json.loads(project.brand_json.read_text(encoding="utf-8"))
        except Exception:
            brand = {"avoid": [recon.app], "domains": [], "colors": []}
        sweep = toolkit.sweep_project(
            sweep_root,
            brand.get("avoid") or [recon.app],
            brand.get("domains") or [],
            brand.get("colors") or [],
            include_planning=False,
        )

        listing = {}
        try:
            listing = json.loads(project.listing_json.read_text(encoding="utf-8"))
        except Exception:
            listing = {}
        lint = toolkit.lint_listing(listing, avoid=brand.get("avoid"))
        contrast = toolkit.contrast_check(project.tokens_json)

        # Optional visual parity
        visuals = []
        for orig in sorted(project.screens_dir.glob("*.png")):
            clone = project.diffs_dir.parent / "clone-screens" / orig.name
            if clone.exists():
                d = toolkit.layout_diff(orig, clone, out=project.diffs_dir / orig.name)
                if d.get("ok"):
                    visuals.append(project.diffs_dir / orig.name)
        if visuals:
            parity = toolkit.parity_score(
                project.features_csv,
                visuals=[str(v) for v in visuals],
                fail_under=fail_under,
            )

        stats = toolkit.feature_stats(recon.features)
        gates = {
            "must_haves_done": stats["shippable"],
            "parity": bool(parity.get("ok"))
            and float(parity.get("overall", 0)) >= fail_under,
            "rebrand_clean": bool(sweep.get("clean")),
            "listing_ok": bool(lint.get("passed")),
            "contrast_ok": bool(contrast.get("passed")),
        }
        ready = all(gates.values())
        project.write(
            project.parity_md,
            _render_parity(recon, parity, sweep, lint, contrast, gates, ready),
        )
        project.mark(self.name, ready, "shippable" if ready else "not shippable")
        return {
            "stage": self.name,
            "ok": ready,
            "ready": ready,
            "note": (
                "shippable"
                if ready
                else "not shippable yet: "
                + ", ".join(k for k, v in gates.items() if not v)
            ),
            "gates": gates,
            "parity": parity,
            "sweep": sweep,
            "lint": lint,
            "contrast": contrast,
            "stats": stats,
        }


def _render_parity(recon, parity, sweep, lint, contrast, gates, ready) -> str:
    out = [
        f"# Verify — {recon.app} rebuild",
        "",
        f"**{'SHIPPABLE' if ready else 'NOT shippable yet'}**",
        "",
        "## Gates",
        "",
        "| gate | result |",
        "| --- | --- |",
    ]
    for k, v in gates.items():
        out.append(f"| {k} | {'PASS' if v else 'FAIL'} |")
    out += ["", "## Parity", ""]
    out.append(parity.get("rendered", parity.get("error", "no parity report")))
    out += ["", "## Rebrand sweep", "", sweep.get("rendered", sweep.get("error", ""))]
    if not sweep.get("ok"):
        out.append("")

    out += [
        "",
        "## Store listing lint",
        "",
        lint.get("rendered", lint.get("error", "")),
    ]
    out += ["", "## Contrast", "", contrast.get("rendered", contrast.get("error", ""))]
    return "\n".join(out) + "\n"


# ── the pack, in order ───────────────────────────────────────────────────────
STEPS = (
    ScoutStep(),
    StudyStep(),
    DesignStep(),
    CriticStep(),
    EnhanceStep(),
    BuilderStep(),
    VerifyStep(),
)
BY_NAME = {s.name: s for s in STEPS}
