"""
palm_reading.py — palmistry (chirology) skill for JARVIS.

Two-step flow, by design:
  1. The model captures the user's palm with screen_process(angle='camera')
     — only main.py's inline vision path can inject an image into the live
     session, so this tool deliberately does NOT try to capture anything.
  2. With the image in view, the model calls palm_reading with a small
     structured observation dict. core/palm_engine.py turns it into a
     deterministic reading (same "tool is the calculator" philosophy as
     core/astro_engine.py) — the model then narrates it warmly.

The observation schema is printed in the denial message below so the model
can recover on its own if it calls this first by mistake.
"""

import json

from core import palm_engine


_OBS_SCHEMA = (
    "hand_shape: 'earth'|'air'|'fire'|'water' (palm+finger proportions); "
    "mounts: {venus|jupiter|saturn|apollo|mercury|mars|luna: 0 flat|1 normal|2 dominant}; "
    "lines: {life_line|head_line|heart_line|fate_line: {quality: 0 faint|1 clear|2 deep, "
    "variant: <code from the capture instructions>, marks: ['star'|'cross'|'triangle'|'island'|'grille']}}; "
    "head_heart_start: 'separated'|'joined'|'simian'; "
    "life_head_gap: 'wide'|'narrow'|'touching'; active_hand: 'right'|'left'"
)


def palm_action(parameters: dict, player=None) -> str:
    p = parameters or {}

    obs = p.get("observations")
    if not isinstance(obs, dict) or not obs:
        msg = (
            "NO_PALM_OBSERVATIONS. Look at the user's palm first: call "
            "screen_process with angle='camera' (user shows their palm to the "
            "webcam), study the image, then call palm_reading again with "
            f"observations built from what you actually see. Schema: {_OBS_SCHEMA}. "
            "Report only what is genuinely visible; omit fields you cannot see."
        )
        _log("no observations — asked model to capture the palm first", player)
        return msg

    hand = (p.get("hand") or obs.get("active_hand") or "right").strip().lower()
    obs.setdefault("active_hand", hand)

    reading = palm_engine.full_palm_reading(obs)
    _log(
        f"palm reading ({reading['active_hand']} hand, "
        f"{len(reading['lines'])} lines, {len(reading['mounts'])} mounts)",
        player,
    )
    return json.dumps(reading, ensure_ascii=False)


def _log(message: str, player=None) -> None:
    print(f"[Palm] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass


TOOL = {
    "name": "palm_reading",
    "description": (
        "Palmistry / chirology expert tool (hast rekha). TWO-STEP flow: "
        "STEP 1 — call screen_process with angle='camera' so you can SEE the "
        "user's open palm (ask them to hold it up, fingers relaxed, good "
        "light). STEP 2 — with the image in front of you, call palm_reading "
        "with 'observations' describing what you ACTUALLY see: "
        "hand_shape ('earth' square palm+short fingers | 'air' square palm+"
        "long fingers | 'fire' long palm+short fingers | 'water' long palm+"
        "long fingers), mounts {venus,jupiter,saturn,apollo,mercury,mars,luna: "
        "0 flat|1 normal|2 dominant}, lines {life_line,head_line,heart_line,"
        "fate_line: {quality 0 faint|1 clear|2 deep, variant code, marks "
        "['star','cross','triangle','island','grille']}} — line variants: "
        "life: long_wide_sweep|hugging_venus|sister_line; head: straight|"
        "sloping|deep_slope_luna; heart: to_jupiter|to_saturn|to_apollo|forked|"
        "chained; fate: from_life|from_luna|broken|absent. Plus "
        "head_heart_start ('separated'|'joined'|'simian') and life_head_gap "
        "('wide'|'narrow'|'touching') if visible, and hand 'right'|'left' "
        "(active hand = present/past, other = inherited traits). "
        "ALL interpretation is computed by the engine from these observations "
        "— never invent meanings yourself, and never fabricate features you "
        "cannot see: omit instead. Route ANY palm/hast-rekha/chirology ask "
        "here. Afterwards present the JSON conversationally and warmly in the "
        "user's language — you are the palmist; this tool is your calculator. "
        "Keep it fun; add a light disclaimer that this is for entertainment, "
        "not medicine or fortune-guarantee."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "observations": {
                "type": "OBJECT",
                "description": "Dict of what you saw in the palm image — schema in the tool description. Omit (do not pass empty) if you have not captured the palm yet.",
            },
            "hand": {
                "type": "STRING",
                "description": "'right' (active, default) or 'left' — which hand the observations came from.",
            },
        },
        "required": [],
    },
    "handler": palm_action,
}
