"""
core/video_agent/characters.py — the permanent character registry.

The user's Psychology Character Design Sheet defines the DEFAULT cast for
every video request:

  * Psychology Expert — adult man, dark wavy hair, beard, round glasses,
    navy blazer + brown/tan waistcoat (calm, intelligent, friendly,
    patient, good listener)
  * Little Boy — expressive brown eyes, dark hair, yellow hoodie (curious,
    energetic, innocent, honest, kind)

Profiles live in `<JARVIS_HOME-agnostic cast dir>/character_reference/`
(default `~/Videos/JARVIS/character_reference/`): characters.json plus the
reference sheet image. The registry is DATA-driven — editing the JSON
changes the default cast everywhere without code changes.

Honesty rules encoded here:

  * the reference sheet is an ILLUSTRATION, not a 3D model — for Blender,
    `blender_model` is null for these characters and callers must not
    pretend otherwise (the only procedural 3D avatar is the original
    robot in build_avatar_scene.py)
  * image-capable generation tools (e.g. ViMax image-to-video) can receive
    the reference sheet via `image_reference()`; tools that only do text
    prompts get `prompt_fragment()` text
  * `image_reference()` never invents a file: it returns the sheet only if
    it exists on disk
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["cast_dir", "load", "defaults", "get", "image_reference",
           "prompt_fragment", "summary_text", "blender_status"]

_FALLBACK_DIR = Path.home() / "Videos" / "JARVIS" / "character_reference"


def cast_dir() -> Path:
    """The character asset directory. `JARVIS_CHARACTER_DIR` overrides;
    default `~/Videos/JARVIS/character_reference` (survives JARVIS_HOME
    relocations because cast media belongs with the video library)."""
    import os
    override = os.environ.get("JARVIS_CHARACTER_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return _FALLBACK_DIR


def load() -> dict[str, Any]:
    path = cast_dir() / "characters.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("characters"):
            return data
    except (OSError, ValueError):
        pass
    return {}


def defaults() -> list[dict[str, Any]]:
    """The default cast, in order (the two sheet characters)."""
    data = load()
    out = []
    for cid in data.get("defaults") or []:
        char = (data.get("characters") or {}).get(cid)
        if char:
            out.append({"id": cid, **char})
    return out


def get(character_id: str) -> dict[str, Any] | None:
    data = load()
    char = (data.get("characters") or {}).get(str(character_id or "").strip())
    if char:
        return {"id": str(character_id).strip(), **char}
    return None


def image_reference(character_id: str | None = None) -> Path | None:
    """The reference-sheet image for image-capable generation tools —
    returned ONLY when it actually exists on disk (never invented)."""
    data = load()
    candidates = [str(character_id or "").strip()] if character_id else []
    candidates += list(data.get("defaults") or [])
    for cid in candidates:
        char = (data.get("characters") or {}).get(cid) or {}
        rel = str(char.get("reference_image") or "")
        if not rel:
            continue
        path = cast_dir() / rel
        if path.is_file():
            return path
    sheet = cast_dir() / str(data.get("reference_sheet") or "")
    return sheet if sheet.is_file() else None


def prompt_fragment(character_ids: list[str] | None = None) -> str:
    """The text block for prompt-based generation tools: who the characters
    are and how they must stay consistent."""
    data = load()
    characters = data.get("characters") or {}
    ids = character_ids or list(data.get("defaults") or [])
    frags = []
    for cid in ids:
        char = characters.get(cid) or {}
        frag = str(char.get("prompt_fragment") or "")
        if frag:
            frags.append(frag)
    relationship = str((data.get("relationship") or {}).get("dynamic") or "")
    consistency = str((data.get("relationship") or {}).get(
        "consistency_rule") or "")
    out = "\n".join(frags)
    if relationship:
        out += f"\nRelationship: {relationship}"
    if consistency:
        out += f"\nConsistency: {consistency}"
    return out


def blender_status() -> dict[str, Any]:
    """Honest Blender capability for the default cast: is there a reusable
    3D model/rig? (Today: no — the sheet is an illustration.)"""
    out = []
    for char in defaults():
        out.append({
            "id": char.get("id"), "name": char.get("name"),
            "blender_model": char.get("blender_model"),
            "note": char.get("blender_note"),
        })
    return {"characters": out,
            "procedural_avatar_available": True,
            "procedural_avatar_note": ("the only existing 3D avatar is the "
                                       "ORIGINAL robot (build_avatar_scene."
                                       "py) — it is NOT these characters")}


def summary_text() -> str:
    data = load()
    if not data:
        return ("No character profiles found — expected "
                f"{cast_dir() / 'characters.json'}.")
    sheet = image_reference()
    lines = [f"Default cast ({cast_dir()}):"]
    for char in defaults():
        lines.append(f"• {char.get('name')} [{char.get('id')}] — "
                     f"{', '.join(char.get('personality') or [])}")
    rel = (data.get("relationship") or {}).get("dynamic") or ""
    if rel:
        lines.append(f"Relationship: {rel}")
    if sheet:
        lines.append(f"Reference sheet: {sheet}")
    else:
        lines.append(f"Reference sheet: MISSING — save the design sheet "
                     f"into {cast_dir()}")
    blender = blender_status()
    lines.append("Blender: " + "; ".join(
        f"{c['name']} → no 3D model yet" if not c["blender_model"]
        else f"{c['name']} → model available" for c in blender["characters"]))
    return "\n".join(lines)
