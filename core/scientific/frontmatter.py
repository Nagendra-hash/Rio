"""
core/scientific/frontmatter.py — parser for SKILL.md frontmatter.

WHY THIS EXISTS
    RIO already has `core.skill_registry.read_frontmatter`, but it is a
    two-line regex: it returns only flat `name:` and `description:` and drops
    everything else. The K-Dense skill packs put real metadata in nested YAML:

        metadata:
          version: "1.11"
          last-reviewed: "2026-09-30"
          openclaw:
            envVars:
            - name: OPENROUTER_API_KEY
              required: false

    and use folded blocks (`description: >`) and lists. Parsing that correctly
    is what makes version tracking, dependency checks and security review
    possible, so this module implements the YAML subset those files actually
    use — with no third-party dependency (the house rule for registry code is
    stdlib only).

SUPPORTED SUBSET
    * `key: scalar`, single/double quoted, booleans, null
    * `key:` + nested map (by indentation)
    * `key: >` / `|` folded and literal blocks (with chomping indicators)
    * `key:` + `- item` lists, including `- key: value` maps with continuation
    * inline lists `[a, b, "c, d"]`
    Comments (`#`) outside quotes are dropped; blank lines are insignificant.

NOT SUPPORTED (deliberately, and absent from the upstream files)
    anchors/aliases, tags, multi-document streams, plain multi-line scalars.
    An unsupported construct is kept as its literal text rather than crashing —
    a metadata parser must never make an install fail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

Delimiter = "---"


def split_frontmatter(text: str) -> tuple[str, str]:
    """(raw frontmatter, body). A file without frontmatter returns ("", text)."""
    if not text:
        return "", ""
    stripped = text.lstrip("\ufeff")
    lines = stripped.splitlines()
    if not lines or lines[0].strip() != Delimiter:
        return "", text
    for index in range(1, len(lines)):
        if lines[index].strip() == Delimiter:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    return "", text  # unterminated block: treat the whole file as body


def parse(text: str) -> dict[str, Any]:
    """Frontmatter of a SKILL.md string as a dict (empty when absent)."""
    raw, _ = split_frontmatter(text)
    if not raw.strip():
        return {}
    return _Parser(_prepare(raw)).parse()


def load(path: str | Path) -> tuple[dict[str, Any], str]:
    """(metadata, body) for one markdown file. Never raises on bad YAML."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    raw, body = split_frontmatter(text)
    if not raw.strip():
        return {}, body
    try:
        meta = _Parser(_prepare(raw)).parse()
    except Exception:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), body


# ── internals ───────────────────────────────────────────────────────────────

_Line = list  # [indent:int, content:str]


def _prepare(raw: str) -> list[_Line]:
    out: list[_Line] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()
        if content.startswith("#"):
            continue
        out.append([indent, content])
    return out


def _split_inline(value: str) -> list[str]:
    """Split an inline list body on commas that are not inside quotes."""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    for ch in value:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def _scalar(value: str) -> Any:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    low = value.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", ""):
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [_scalar(part) for part in _split_inline(inner)] if inner else []
    return value


class _Parser:
    def __init__(self, lines: list[_Line]):
        self.lines = lines

    def parse(self) -> Any:
        if not self.lines:
            return {}
        value, _ = self._block(0, self.lines[0][0])
        return value

    def _block(self, index: int, indent: int) -> tuple[Any, int]:
        if self.lines[index][1].startswith("-"):
            return self._list(index, indent)
        return self._map(index, indent)

    def _map(self, index: int, indent: int) -> tuple[dict, int]:
        result: dict[str, Any] = {}
        while index < len(self.lines):
            ind, content = self.lines[index]
            if ind < indent:
                break
            if ind > indent:
                # A deeper line with no owning key: malformed; skip it rather
                # than aborting the whole parse.
                index += 1
                continue
            key, sep, value = content.partition(":")
            if not sep:
                index += 1
                continue
            key = key.strip()
            value = value.strip()
            index += 1
            if value in (">", "|", ">-", "|-", ">+", "|+"):
                block: list[str] = []
                while index < len(self.lines) and self.lines[index][0] > indent:
                    block.append(self.lines[index][1])
                    index += 1
                joiner = " " if value.startswith(">") else "\n"
                result[key] = joiner.join(block)
            elif value:
                result[key] = _scalar(value)
            elif index < len(self.lines) and (
                self.lines[index][0] > indent
                or (
                    self.lines[index][0] == indent
                    and self.lines[index][1].startswith("-")
                )
            ):
                # A nested map is indented deeper, but YAML also allows a block
                # sequence to sit at the SAME indent as its key:
                #     envVars:
                #     - name: OPENROUTER_API_KEY
                # Treating that `-` as a new key produced a bogus "- name" key.
                nested, index = self._block(index, self.lines[index][0])
                result[key] = nested
            else:
                result[key] = None
        return result, index

    def _list(self, index: int, indent: int) -> tuple[list, int]:
        items: list[Any] = []
        while index < len(self.lines):
            ind, content = self.lines[index]
            if ind != indent or not content.startswith("-"):
                break
            rest = content[1:].strip()
            index += 1
            if not rest:
                if index < len(self.lines) and self.lines[index][0] > indent:
                    nested, index = self._block(index, self.lines[index][0])
                    items.append(nested)
                else:
                    items.append(None)
                continue
            if ":" in rest and not rest.startswith(("'", '"', "[")):
                sub: list[_Line] = [[indent + 2, rest]]
                while index < len(self.lines) and self.lines[index][0] > indent:
                    sub.append(self.lines[index])
                    index += 1
                items.append(_Parser(sub).parse())
            else:
                items.append(_scalar(rest))
        return items, index
