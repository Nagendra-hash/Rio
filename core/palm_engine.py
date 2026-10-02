"""
core/palm_engine.py — deterministic palmistry (chirology) interpretation.

Same philosophy as core/astro_engine.py: every number and label here is looked
up, never invented by the model. The model's job is only to LOOK at the palm
(captured via screen_process angle='camera') and fill in a small observation
dict; this engine turns observations into a structured reading.

What it maps (classic Western + Vedic chirology conventions):
  • Mounts (7)     — Venus, Jupiter, Saturn, Apollo, Mercury, Mars, Luna
  • Major lines(4) — Life, Head, Heart, Fate
  • Hand shape (4) — Earth, Air, Fire, Water
  • Special marks  — star, cross, triangle, island, grille (per feature)
  • Two classic combos — head/heart line start, life/head line gap

Usage:
    reading = full_palm_reading(observations_dict)
Missing observations simply degrade that section — a partial dict still yields
a partial reading, so the model never has to guess to satisfy the schema.
"""
from __future__ import annotations

# ── Mounts ────────────────────────────────────────────────────────────────────
# score: 0 flat .. 2 dominant. Lord, domain, and what each score traditionally
# reads as. Kept deliberately compact — the conversational layer (the model)
# expands on it in the user's language.
_MOUNTS = {
    "venus": {
        "lord": "Venus", "domain": "love, vitality, warmth, appreciation of beauty",
        "tones": {
            0: "reserved in matters of love; affection shown through acts, not display",
            1: "balanced warmth — affectionate without excess",
            2: "magnetic warmth; strong vitality, love of pleasure, music and beauty",
        },
    },
    "jupiter": {
        "lord": "Jupiter", "domain": "ambition, leadership, self-drive, pride",
        "tones": {
            0: "prefers cooperation over command; ambition quiet",
            1: "healthy ambition; leads when asked, content either way",
            2: "commanding presence; strong ego-drive, natural leader, proud",
        },
    },
    "saturn": {
        "lord": "Saturn", "domain": "discipline, patience, responsibility, introspection",
        "tones": {
            0: "light-hearted; resists routine and heavy duty",
            1: "steady; takes duty in stride",
            2: "solemn, disciplined, carries duty heavily; prone to introspection",
        },
    },
    "apollo": {
        "lord": "Apollo (Sun)", "domain": "creativity, charisma, recognition, optimism",
        "tones": {
            0: "prefers the background; talents kept private",
            1: "creative spark with healthy optimism",
            2: "charisma and creative brilliance; drawn to recognition and the arts",
        },
    },
    "mercury": {
        "lord": "Mercury", "domain": "communication, wit, commerce, adaptability",
        "tones": {
            0: "economical with words; listens more than talks",
            1: "conversational ease; business sense present",
            2: "gift of the gab; quick wit, persuasive, trade-minded",
        },
    },
    "mars": {
        "lord": "Mars", "domain": "courage, resistance, stamina",
        "tones": {
            0: "avoids conflict; endurance quiet",
            1: "courage when needed, even temper",
            2: "fighting spirit; defends fiercely, competitive",
        },
    },
    "luna": {
        "lord": "Luna (Moon)", "domain": "imagination, intuition, travel, moods",
        "tones": {
            0: "practical-minded; imagination serves the task at hand",
            1: "good intuition balanced with realism",
            2: "vivid imagination and strong intuition; moods swing; wanderlust",
        },
    },
}

# ── Major lines ───────────────────────────────────────────────────────────────
# quality: 0 faint/broken → 2 deep/clear; plus shape codes the model picks.
_LINES = {
    "life_line": {
        "label": "Life line", "governs": "vitality, stamina, life pace (NOT lifespan)",
        "qualities": {
            0: "energy comes in waves; needs recovery periods",
            1: "steady vitality, no dramatic swings",
            2: "robust constitution; strong recovery from setbacks",
        },
        "variants": {
            "long_wide_sweep": "wide arc around Venus — generous energy, open approach to life",
            "hugging_venus": "line hugs the thumb — cautious, conserves energy",
            "sister_line": "a second line shadows it — strong recovery, guarded by luck",
        },
    },
    "head_line": {
        "label": "Head line", "governs": "thinking style, focus, decision pattern",
        "qualities": {
            0: "mind wanders; brilliance in bursts, focus costly",
            1: "balanced thinker; practical judgment",
            2: "powerful concentration; decisive, sometimes to a fault",
        },
        "variants": {
            "straight": "practical, logical, linear thinker",
            "sloping": "creative, imaginative; grounded enough to ship ideas",
            "deep_slope_luna": "slopes deep toward Luna — dreamer; guard against escapism",
        },
    },
    "heart_line": {
        "label": "Heart line", "governs": "emotional style, how affection is given/received",
        "qualities": {
            0: "keeps feelings private; trust builds slowly",
            1: "warm, steady emotional life",
            2: "feels deeply and openly; emotional intensity possible",
        },
        "variants": {
            "to_jupiter": "extends to Jupiter — idealistic in love, high standards",
            "to_saturn": "ends at Saturn — expresses love through acts of care and duty",
            "to_apollo": "ends at Apollo — warm, playful, romantic",
            "forked": "forked end — sees both sides; diplomatic heart",
            "chained": "chained appearance — emotional ups and downs early in life",
        },
    },
    "fate_line": {
        "label": "Fate line", "governs": "direction, career feel, external influence on path",
        "qualities": {
            0: "self-made path; direction chosen freely rather than felt as fate",
            1: "sense of direction; career builds steadily",
            2: "strong sense of destiny; career is a calling, felt as inevitable",
        },
        "variants": {
            "from_life": "rises from the life line — self-made; family support earned, not given",
            "from_luna": "rises from Luna — career involves the public, travel or others' support",
            "broken": "breaks along the way — career pivots, each self-chosen",
            "absent": "no clear fate line — freedom over destiny; path is self-authored",
        },
    },
}

# ── Hand shapes (classical 4-element) ────────────────────────────────────────
_HAND_SHAPES = {
    "earth": ("square palm + short fingers", "practical, reliable, hands-on; values results over talk"),
    "air":   ("square palm + long fingers",  "curious, communicative, mentally restless; needs stimulation"),
    "fire":  ("long palm + short fingers",   "energetic, impulsive, charismatic; acts first, refines later"),
    "water": ("long palm + long fingers",    "sensitive, intuitive, artistic; feels the room before entering it"),
}

# ── Special marks (per line/mount) ───────────────────────────────────────────
_MARKS = {
    "star":     "sudden brilliant event at that feature's domain (classic omen of fame on Apollo)",
    "cross":    "obstacle or turning point in that domain",
    "triangle": "talent; an aptitude that can be mastered or monetized",
    "island":   "a period of strain or lowered vitality within that line's span",
    "grille":   "scattered energy in that mount; enthusiasm without focus",
}

# ── Two classic combos the model can spot at a glance ────────────────────────
_HEAD_HEART_START = {
    "separated": "head and heart lines start apart — decisions are weighed, not just felt; classic balanced temperament",
    "joined":    "head and heart lines joined at the start — heart and mind rise together; passionate, sometimes torn",
    "simian":    "a single crease across the palm (simian line) — intensity; feeling and thinking channel through one focused line",
}

_LIFE_HEAD_GAP = {
    "wide":     "wide gap between life and head lines at the start — independent streak, early self-reliance",
    "narrow":   "narrow gap — family-close, cautious starter",
    "touching": "lines touch at the start — deeply shaped by family and upbringing",
}


def _clamp_quality(value) -> int:
    """Coerce a line quality to 0/1/2; junk and absence both read as 1."""
    try:
        q = int(value)
    except (TypeError, ValueError):
        return 1
    return max(0, min(2, q))


def full_palm_reading(obs: dict) -> dict:
    """Turn the model's palm observations into a deterministic reading.

    Expected obs keys (all optional — missing keys degrade gracefully):
      hand_shape:        'earth' | 'air' | 'fire' | 'water'
      mounts:            {mount_name: 0|1|2}
      lines:             {line_name: {"quality": 0|1|2,
                                      "variant": str,        # optional
                                      "marks": [str, ...]}}  # optional
      head_heart_start:  'separated' | 'joined' | 'simian'   (optional)
      life_head_gap:     'wide' | 'narrow' | 'touching'      (optional)
      active_hand:       'right' | 'left'  (which hand was read)
    """
    obs = obs or {}
    out: dict = {
        "active_hand": (str(obs.get("active_hand") or "right")).strip().lower(),
        "hand_shape": None,
        "dominant_mount": None,
        "mounts": [],
        "lines": [],
        "special_notes": [],
    }

    # ── hand shape ────────────────────────────────────────────────────────
    hs = str(obs.get("hand_shape") or "").strip().lower()
    if hs in _HAND_SHAPES:
        looks, temperament = _HAND_SHAPES[hs]
        out["hand_shape"] = {"type": hs, "looks_like": looks,
                             "temperament": temperament}

    # ── mounts ────────────────────────────────────────────────────────────
    mounts_in = obs.get("mounts") or {}
    for name, score in mounts_in.items():
        key = str(name).strip().lower()
        if key not in _MOUNTS:
            continue
        try:
            score = int(score)
        except (TypeError, ValueError):
            continue
        if score not in (0, 1, 2):
            continue
        m = _MOUNTS[key]
        entry = {"mount": m["lord"], "domain": m["domain"],
                 "score": score, "tone": m["tones"][score]}
        out["mounts"].append(entry)

    # Dominant mount = highest score; ties break in the classic order
    # (Venus, Jupiter, Saturn, Apollo, Mercury, Mars, Luna).
    if out["mounts"]:
        order = {m["lord"]: i for i, m in enumerate(
            _MOUNTS[k] for k in ("venus", "jupiter", "saturn",
                                 "apollo", "mercury", "mars", "luna"))}
        best = max(out["mounts"], key=lambda e: (e["score"], -order[e["mount"]]))
        if best["score"] > 0:
            out["dominant_mount"] = {"mount": best["mount"], "score": best["score"]}

    # ── lines ─────────────────────────────────────────────────────────────
    lines_in = obs.get("lines") or {}
    for name, spec in lines_in.items():
        key = str(name).strip().lower()
        if key not in _LINES:
            continue
        spec = spec if isinstance(spec, dict) else {}
        line_def = _LINES[key]
        q = _clamp_quality(spec.get("quality", 1))
        entry = {"line": line_def["label"], "governs": line_def["governs"],
                 "quality": q, "reads_as": line_def["qualities"][q]}

        variant = str(spec.get("variant") or "").strip().lower()
        if variant and variant in line_def.get("variants", {}):
            entry["variant"] = {"code": variant,
                                "reads_as": line_def["variants"][variant]}

        mark_entries = []
        for mk in (spec.get("marks") or []):
            mk_key = str(mk).strip().lower()
            if mk_key in _MARKS:
                mark_entries.append({"mark": mk_key, "meaning": _MARKS[mk_key]})
        if mark_entries:
            entry["marks"] = mark_entries

        out["lines"].append(entry)

    # ── classic combos ────────────────────────────────────────────────────
    hhs = str(obs.get("head_heart_start") or "").strip().lower()
    if hhs in _HEAD_HEART_START:
        out["special_notes"].append(_HEAD_HEART_START[hhs])

    lhg = str(obs.get("life_head_gap") or "").strip().lower()
    if lhg in _LIFE_HEAD_GAP:
        out["special_notes"].append(_LIFE_HEAD_GAP[lhg])

    return out
