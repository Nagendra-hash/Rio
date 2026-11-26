"""
orchestrator/agents/sql/schema.py — the SQL agent's value shapes.

One value type per field so the agent, the schema reader, the validator
and the tests share exactly one vocabulary:

    TableInfo       one permitted table/view: name, kind, row count,
                    columns with affinity/PK info.
    ColumnInfo      one column: name, position, declared type, notnull,
                    default, pk flag.
    Relation        one foreign-key edge: child.column → parent.column.
    SchemaSnapshot  the WHOLE permitted schema of one database at one
                    moment: tables + relations. The nl2sql engine and the
                    validator both read this — never the live connection —
                    so "what may this query touch" is decided against a
                    fixed, testable shape.
    QueryPlan       what a natural-language ask compiled into: the SQL,
                    the strategy that produced it, its confidence and the
                    tables it uses.
    QueryResult     what execution returned: columns/rows (json-safe),
                    row_count + truncated flag, applied LIMIT, elapsed
                    time, statement class, optional plan text.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

MAX_IDENTIFIER = 120
MAX_SQL_CHARS = 20_000
MAX_COLUMNS = 120                  # columns tracked per table
MAX_TABLES = 500                   # tables tracked per snapshot
MAX_RELATIONS = 1_000
MAX_ROWS_ATTRIB = 1_000_000        # row_count sanity cap
READONLY_UNKNOWN = -1              # row count not measured

_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_ $]*")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_identifier(raw) -> str:
    """Fold a schema identifier to its readable name (unquoted, traced)."""
    t = str(raw or "").strip()
    for q in ('"', "'", "`", "[", "]"):
        t = t.replace(q, "")
    return t.strip()[:MAX_IDENTIFIER]


@dataclass
class ColumnInfo:
    name: str = ""
    cid: int = 0
    affinity: str = ""              # declared type, e.g. INTEGER / TEXT
    not_null: bool = False
    default: str = ""
    pk: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _clean_column(raw) -> Optional[ColumnInfo]:
    if not isinstance(raw, dict):
        return None
    name = clean_identifier(raw.get("name"))
    if not name:
        return None
    try:
        cid = max(0, int(raw.get("cid") or 0))
    except (TypeError, ValueError):
        cid = 0
    return ColumnInfo(
        name=name,
        cid=cid,
        affinity=str(raw.get("affinity") or "")[:60],
        not_null=bool(raw.get("not_null")),
        default=str(raw.get("default") or "")[:80],
        pk=bool(raw.get("pk")),
    )


@dataclass
class TableInfo:
    name: str = ""
    kind: str = "table"             # table | view
    row_count: int = READONLY_UNKNOWN
    columns: list[ColumnInfo] = field(default_factory=list)

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def has_column(self, name: str) -> bool:
        n = clean_identifier(name).lower()
        return any(c.name.lower() == n for c in self.columns)

    def is_numeric(self, name: str) -> bool:
        for c in self.columns:
            if c.name.lower() == clean_identifier(name).lower():
                affinity = c.affinity.upper()
                return any(k in affinity for k in
                           ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC"))
        return False

    def to_lines(self) -> list[str]:
        n = ("?" if self.row_count == READONLY_UNKNOWN else self.row_count)
        cols = ", ".join(f"{c.name}{'*' if c.pk else ''}"
                         f"{'' if not c.affinity else f' {c.affinity}'}"
                         for c in self.columns[:20])
        more = (f" … +{len(self.columns) - 20}" if len(self.columns) > 20
                else "")
        return [f"  • {self.name} [{self.kind}, {len(self.columns)} cols, "
                f"rows={n}]  {cols}{more}"]

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind,
                "row_count": self.row_count,
                "columns": [c.to_dict() for c in self.columns]}


def _clean_table(raw) -> Optional[TableInfo]:
    if not isinstance(raw, dict):
        return None
    name = clean_identifier(raw.get("name"))
    if not name:
        return None
    kind = str(raw.get("kind") or "table").strip().lower()
    if kind not in ("table", "view"):
        kind = "table"
    cols = [c for c in (_clean_column(r) for r in
                        (raw.get("columns")
                         if isinstance(raw.get("columns"), list) else []))
            if c is not None][:MAX_COLUMNS]
    try:
        rc = int(raw.get("row_count") or READONLY_UNKNOWN)
    except (TypeError, ValueError):
        rc = READONLY_UNKNOWN
    rc = min(MAX_ROWS_ATTRIB, rc)
    return TableInfo(name=name, kind=kind, row_count=rc, columns=cols)


@dataclass
class Relation:
    """One FK edge: child.column references parent.column."""
    table: str = ""                 # the child (FK holder)
    column: str = ""
    ref_table: str = ""             # the parent (referenced)
    ref_column: str = ""

    def label(self) -> str:
        return f"{self.table}.{self.column} → {self.ref_table}.{self.ref_column}"

    def to_dict(self) -> dict:
        return asdict(self)


def _clean_relation(raw) -> Optional[Relation]:
    if not isinstance(raw, dict):
        return None
    rel = Relation(
        table=clean_identifier(raw.get("table")),
        column=clean_identifier(raw.get("column")),
        ref_table=clean_identifier(raw.get("ref_table")),
        ref_column=clean_identifier(raw.get("ref_column")),
    )
    if not (rel.table and rel.column and rel.ref_table):
        return None
    return rel


@dataclass
class SchemaSnapshot:
    """The permitted database schema, frozen at capture time."""
    database: str = ""
    tables: list[TableInfo] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    readonly: bool = True
    captured_at: str = field(default_factory=now_iso)

    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]

    def get_table(self, name: str) -> Optional[TableInfo]:
        """Case-insensitive table lookup (sqlite identifiers fold)."""
        n = clean_identifier(name).lower()
        for t in self.tables:
            if t.name.lower() == n:
                return t
        return None

    def has_table(self, name: str) -> bool:
        return self.get_table(name) is not None

    def relations_of(self, table: str) -> list[Relation]:
        """FK edges touching a table, either side."""
        t = clean_identifier(table).lower()
        return [r for r in self.relations
                if r.table.lower() == t or r.ref_table.lower() == t]

    def relation_labels(self) -> list[str]:
        return [r.label() for r in self.relations]

    def to_lines(self) -> list[str]:
        out = [f"Schema of {self.database!r} "
               f"({'read-only' if self.readonly else 'writes need approval'})"
               f" — {len(self.tables)} tables/views captured "
               f"{self.captured_at}:"]
        if not self.tables:
            out.append("  (empty database — no tables to inspect)")
        for t in self.tables:
            out += t.to_lines()
        if self.relations:
            out.append("")
            out.append(f"FK relations ({len(self.relations)} declared)")
            for r in self.relations[:30]:
                out.append(f"  • {r.label()}")
            if len(self.relations) > 30:
                out.append(f"  … and {len(self.relations) - 30} more")
        return out

    def to_prompt(self, max_chars: int = 6_000) -> str:
        """Compact schema text for an LLM prompt (tables + columns only;
        FK edges listed briefly). Column count is capped so the prompt
        budget holds for real databases."""
        parts: list[str] = []
        for t in self.tables:
            cols = ", ".join(
                f"{c.name}{'*' if c.pk else ''}"
                f"{f':{c.affinity}' if c.affinity else ''}"
                for c in t.columns[:40])
            more = (f" +{len(t.columns) - 40}" if len(t.columns) > 40 else "")
            parts.append(f"{t.name} ({t.kind}): {cols}{more}")
        if self.relations:
            parts.append("FK: " + "; ".join(r.label()
                                            for r in self.relations[:50]))
        text = "\n".join(parts)
        return text if len(text) <= max_chars else text[:max_chars - 1] + "…"

    def to_dict(self) -> dict:
        return {"database": self.database, "readonly": self.readonly,
                "captured_at": self.captured_at,
                "tables": [t.to_dict() for t in self.tables],
                "relations": [r.to_dict() for r in self.relations]}


def validate_snapshot(raw) -> tuple[Optional[SchemaSnapshot], list[str]]:
    """Validate/normalize a SchemaSnapshot or dict. Never invents tables."""
    errors: list[str] = []
    if isinstance(raw, SchemaSnapshot):
        data = asdict(raw)
    elif isinstance(raw, dict):
        data = dict(raw)
    else:
        return None, ["snapshot must be a SchemaSnapshot or a dict"]
    db = str(data.get("database") or "").strip()
    if not db:
        errors.append("snapshot database name missing")
    if errors:
        return None, errors
    tables = [t for t in (_clean_table(r) for r in
                          (data.get("tables")
                           if isinstance(data.get("tables"), list) else []))
              if t is not None][:MAX_TABLES]
    rels = [r for r in (_clean_relation(x) for x in
                        (data.get("relations")
                         if isinstance(data.get("relations"), list) else []))
            if r is not None][:MAX_RELATIONS]
    return SchemaSnapshot(
        database=db,
        tables=tables, relations=rels,
        readonly=bool(data.get("readonly", True)),
        captured_at=str(data.get("captured_at") or now_iso()),
    ), []


@dataclass
class QueryPlan:
    """What a natural-language ask compiled into."""
    sql: str = ""
    strategy: str = ""              # template | llm | direct
    confidence: float = 0.0
    rationale: str = ""
    tables: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def clean_sql(raw) -> str:
    """Whitespace-normalize + cap a SQL string."""
    return re.sub(r"[ \t]+", " ", str(raw or "")).strip()[:MAX_SQL_CHARS]


@dataclass
class QueryResult:
    """What execution returned — json-safe throughout."""
    ok: bool
    sql: str = ""
    statement_class: str = ""
    database: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    applied_limit: int = 0
    elapsed_ms: int = 0
    error: str = ""
    plan_text: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
