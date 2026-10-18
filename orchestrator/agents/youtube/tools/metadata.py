"""
orchestrator/agents/youtube/tools/metadata.py — YouTube upload metadata rules.

A faithful Python port of the upstream pack's
``utils/youtube-metadata-validator.js`` (MIT — see ``ATTRIBUTION.md``): the
same limits YouTube itself enforces, applied *before* anything is queued for
upload, so an invalid title/tag set can never reach the publish gate.

    from orchestrator.agents.youtube.tools import metadata as meta
    result = meta.validate(seo)
    result["valid"], result["errors"], result["value"]["tags"]

Pure and stdlib-only, so the checks are testable offline and cannot be
"worked around" by a model drafting the metadata.
"""
from __future__ import annotations

MAX_TITLE_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 5000
MAX_TAGS_LENGTH = 450        # YouTube's total tag budget, measured with commas
MAX_TAGS = 30


def _strip_control(value: object, replacement: str = "") -> str:
    """Drop C0/C1 control characters (mirrors the JS removeControlCharacters)."""
    out = []
    for ch in str(value if value is not None else ""):
        code = ord(ch)
        out.append(replacement if (code <= 31 or code == 127) else ch)
    return "".join(out)


def clean_text(value: object) -> str:
    """Collapse control characters and runs of whitespace into one line."""
    text = _strip_control(value, " ")
    return " ".join(text.split()).strip()


def normalize_tags(raw: object) -> list[str]:
    """Dedupe, strip '#'/quotes, cap at 30 tags and 450 total characters.

    The budget counts the commas between tags, exactly as the upstream tool
    does, so a tag list that "fits" here fits on upload.
    """
    if isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        values = str(raw or "").split(",")

    unique: list[str] = []
    seen: set[str] = set()
    total = 0
    for value in values:
        tag = clean_text(value).lstrip("#").replace('"', "")[:100].strip()
        key = tag.lower()
        if not tag or key in seen:
            continue
        nxt = total + len(tag) + (1 if unique else 0)
        if nxt > MAX_TAGS_LENGTH or len(unique) >= MAX_TAGS:
            break
        unique.append(tag)
        seen.add(key)
        total = nxt
    return unique


def normalize(metadata: dict | None) -> dict:
    """Coerce any of {snippet, seo, flat} shapes into the upload shape."""
    md = metadata or {}
    snippet = md.get("snippet") or md.get("seo") or md
    inner = snippet.get("metadata") if isinstance(snippet, dict) else {}
    inner = inner if isinstance(inner, dict) else {}
    return {
        "title": clean_text(snippet.get("title"))[:MAX_TITLE_LENGTH],
        "description": _strip_control(snippet.get("description")).strip()[
            :MAX_DESCRIPTION_LENGTH],
        "tags": normalize_tags(snippet.get("tags")),
        "categoryId": str(snippet.get("categoryId")
                          if snippet.get("categoryId") is not None
                          else inner.get("category", "22")).strip(),
        "defaultLanguage": str(snippet.get("defaultLanguage")
                               if snippet.get("defaultLanguage") is not None
                               else inner.get("language", "en")).strip(),
        "defaultAudioLanguage": str(
            snippet.get("defaultAudioLanguage")
            if snippet.get("defaultAudioLanguage") is not None
            else inner.get("language", "en")).strip(),
    }


def validate(metadata: dict | None) -> dict:
    """Return {"valid", "errors", "warnings", "value"} — never raises."""
    value = normalize(metadata)
    errors: list[str] = []
    warnings: list[str] = []

    if not value["title"]:
        errors.append("A video title is required.")
    if len(value["title"]) > MAX_TITLE_LENGTH:
        errors.append(f"Title exceeds {MAX_TITLE_LENGTH} characters.")
    if not value["description"]:
        warnings.append("The video description is empty.")
    if len(value["description"]) > MAX_DESCRIPTION_LENGTH:
        errors.append(f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters.")
    if not _is_category(value["categoryId"]):
        errors.append("YouTube category must be a numeric category ID.")
    if not _is_language(value["defaultLanguage"]):
        errors.append("Default language must be a valid language code.")
    if len(value["tags"]) < 3:
        warnings.append("Fewer than three usable tags remain after validation.")

    return {"valid": not errors, "errors": errors, "warnings": warnings,
            "value": value}


def render(result: dict) -> str:
    value = result.get("value") or {}
    out = ["# Upload metadata", "",
           f"**{'VALID' if result.get('valid') else 'INVALID'}**", "",
           f"- title ({len(value.get('title') or '')}/{MAX_TITLE_LENGTH}): "
           f"{value.get('title') or '(none)'}",
           f"- description: {len(value.get('description') or '')} chars",
           f"- tags ({len(value.get('tags') or [])}, "
           f"{sum(len(t) for t in (value.get('tags') or []))} chars): "
           f"{', '.join(value.get('tags') or []) or '(none)'}",
           f"- categoryId: {value.get('categoryId') or '(none)'}",
           f"- language: {value.get('defaultLanguage') or '(none)'}", ""]
    if result.get("errors"):
        out += ["## Errors", ""] + [f"- {e}" for e in result["errors"]] + [""]
    if result.get("warnings"):
        out += ["## Warnings", ""] + [f"- {w}" for w in result["warnings"]] + [""]
    return "\n".join(out)


def _is_category(value: str) -> bool:
    return bool(value) and value.isdigit() and len(value) <= 3


def _is_language(value: str) -> bool:
    import re
    return bool(re.match(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$", value or ""))
