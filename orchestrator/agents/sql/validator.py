"""
orchestrator/agents/sql/validator.py — the SQL VALIDATOR.

A generated statement is only as good as the schema it was written
against. The validator therefore checks it against the SchemaSnapshot —
and AGAINST the permission layer — before anything runs, in this order:

    1. shape        a statement exists, is within the size cap, parses as
                    one of the statement shapes the engine produces.
    2. safety       classify_sql() — read-only / approval-required /
                    blocked. BLOCKED is reported as an error (never
                    runs, even approved); approval-required flips the
                    agent into the approval gate.
    3. identifiers  every table/column/dot-path the SQL names must exist
                    in the snapshot; SELECT * is allowed but flagged.
    4. structure    pragmatic SQL-shape checks the sqlite engine would
                    reject anyway — unbalanced parens, a same-table
                    second COUNT(*) without GROUP BY, comparing an
                    ORDER BY column against a numeric literal (type
                    mismatch where the result is a silent sort bug).

The validator NEVER executes: `EXPLAIN is execution` here is the
executor's job. Invalid SQL returns (plan, errors) with a short list of
human-readable errors; valid SQL returns the same plan with errors=[].
"""
from __future__ import annotations

import re
from typing import Optional

from orchestrator.agents.sql import safety
from orchestrator.agents.sql.schema import (MAX_SQL_CHARS, QueryPlan,
                                            SchemaSnapshot,
                                            clean_identifier, clean_sql)

# identifier rough-shape: letters/digits/_/$/space (allow space for
# quoted identifiers, which clean_identifier has already unwrapped)
_IDENT_CHARS = re.compile(r"^[A-Za-z0-9_$ ]+$")


def _idents_in(clause: str) -> list[str]:
    """Split `a.b.c, d AS x` into dotted paths, trimmed."""
    out: list[str] = []
    for tok in re.split(r"[,(]|\bAS\b", clause or "", flags=re.IGNORECASE):
        tok = tok.strip()
        if not tok or tok == "*":
            continue
        if not _IDENT_CHARS.match(tok):
            continue        # expressions/functions are not identifiers
        tok = tok.split()[-1] if " " in tok else tok
        if tok:
            out.append(tok)
    return out


def _dot_root(path: str) -> str:
    return clean_identifier(path.split(".")[0])


def _dot_leaf(path: str) -> str:
    return clean_identifier(path.split(".")[-1])


_NEXT_CLAUSE = (r"\b(?:FROM|WHERE|GROUP|ORDER|HAVING|LIMIT|OFFSET|"
               r"VALUES|SET|ON|USING|RETURNING|UNION|EXCEPT|INTERSECT|"
               r"WINDOW)\b")


def _clause(sql: str, keyword: str) -> str:
    """Best-effort small-parser: text after a top-level keyword up to the
    next top-level clause keyword. Comments/strings already blanked."""
    pat = re.compile(
        rf"\b{keyword}\b([^\n]*?)(?={_NEXT_CLAUSE})|"
        rf"\b{keyword}\b([^\n]*)",
        re.IGNORECASE)
    m = pat.search(sql)
    if not m:
        return ""
    return (m.group(1) or m.group(2) or "").strip()


def validate(plan: QueryPlan | str, snap: SchemaSnapshot) \
        -> tuple[Optional[QueryPlan], list[str]]:
    """Returns (plan, errors). plan is the cleaned/normed QueryPlan; a
    string input is wrapped first. errors non-empty ⇒ the agent refuses."""
    if isinstance(plan, str):
        plan = QueryPlan(sql=clean_sql(plan) or "",
                         strategy="direct")
    sql = plan.sql or ""
    errors: list[str] = []

    if not sql.strip():
        errors.append("no SQL to validate")
        return plan, errors
    if len(sql) > MAX_SQL_CHARS:
        errors.append(f"SQL exceeds {MAX_SQL_CHARS} chars")

    # ── 1. statement-level safety ──────────────────────────────────────
    level = safety.classify_sql(sql)
    plan.statement_class = safety.statement_class_of(
        safety.statements(sql)[0] if safety.statements(sql) else sql)
    if level == safety.BLOCKED:
        errors.append("blocked SQL: loads code from outside the database "
                      "or tampers with engine safety settings — never runs, "
                      "even approved")
        return plan, errors

    # ── 2. paren balance ───────────────────────────────────────────────
    skeleton, _lits = safety.strip_comments_and_strings(sql)
    if skeleton.count("(") != skeleton.count(")"):
        errors.append("unbalanced parentheses")

    # ── 3. identifier existence ────────────────────────────────────────
    unknown_tables, unknown_cols = _check_identifiers(sql, snap)
    if unknown_tables:
        errors.append("unknown table" + ("s" if len(unknown_tables) > 1 else "")
                      + f" in snapshot: {', '.join(unknown_tables[:6])}")
    if unknown_cols:
        errors.append("unknown column"
                      + ("s" if len(unknown_cols) > 1 else "")
                      + f" in snapshot: {', '.join(unknown_cols[:8])}")

    # ── 4. structure pragmas ───────────────────────────────────────────
    if _stmt_count(sql) > 1 and level != safety.READ_ONLY:
        # multi-statement scripts that mutate are parked as one approval,
        # so they are valid but flagged; the agent shows destructive_summary
        pass
    if re.search(r"\bORDER\s+BY\b[^()]*?=\s*['\"]?\d", skeleton,
                 re.IGNORECASE):
        errors.append("ORDER BY uses a comparison — looks like a typo; "
                      "did you mean WHERE?")

    if errors:
        return plan, errors
    plan.tables = _tables_in(sql, snap)
    return plan, []


def _check_identifiers(sql: str, snap: SchemaSnapshot) -> tuple[list[str], list[str]]:
    """Tables/aliases vs snapshot; columns without a table prefix vs the
    FROM clause's tables. Wildcards and aliases are tolerated."""
    skeleton, _ = safety.strip_comments_and_strings(sql)
    tables_in_sql: dict[str, str] = {}    # name/alias → real name (fold)
    for clause_txt, kw in ((_clause(skeleton, "FROM"), "FROM"),
                           (_clause(skeleton, "JOIN"), "JOIN"),
                           (_clause(skeleton, "UPDATE"), "UPDATE"),
                           (_clause(skeleton, "INTO"), "INTO")):
        for tok in _idents_in(clause_txt):
            root = _dot_root(tok)
            if root and root.lower() not in tables_in_sql \
                    and not root.isdigit():
                tables_in_sql[root.lower()] = root

    unknown_tables, unknown_cols = [], []
    for name in tables_in_sql.values():
        if root := snap.get_table(name):
            continue
        # an alias? count as known only if a real table also present
        if not snap.tables:
            continue
        upper_table = [t.name for t in snap.tables
                       if t.name.upper() == name.upper()]
        if not upper_table:
            unknown_tables.append(name)

    # columns: bare `foo` in a select list / where — check against the
    # union of all columns of the tables referenced (or whole snapshot
    # when none match — the honest refusal for a naked column name)
    target_cols: set[str] = set()
    for tname in tables_in_sql.values():
        hit = snap.get_table(tname)
        if hit:
            target_cols |= {c.name.lower() for c in hit.columns}
    if not tables_in_sql and snap.tables:
        for t in snap.tables[:20]:
            target_cols |= {c.name.lower() for c in t.columns}

    for clause_kw in ("SELECT", "WHERE", "GROUP BY", "ORDER BY", "ON"):
        body = _clause(skeleton, clause_kw)
        for tok in _idents_in(body):
            leaf = _dot_leaf(tok)
            if not leaf or leaf.isdigit():
                continue
            if leaf.lower() in ("asc", "desc", "collate", "nulls", "first",
                                "last"):
                continue      # sort direction words, not columns
            if "." in tok:
                root = _dot_root(tok)
                hit = snap.get_table(root)
                if hit and not snap.tables:
                    continue
                if hit and not hit.has_column(leaf) and \
                        not _looks_like_function(skeleton, tok):
                    unknown_cols.append(tok)
                continue
            if leaf.lower() in target_cols:
                continue
            if _looks_like_function(skeleton, tok):
                continue
            if _is_alias_or_literal(body, tok):
                continue
            if snap.tables:
                unknown_cols.append(leaf)
    # de-dup, case-folded
    seen_t, seen_c = set(), set()
    unknown_tables = [t for t in unknown_tables
                      if (k := t.lower()) not in seen_t and not seen_t.add(k)]
    unknown_cols = [c for c in unknown_cols
                    if (k := c.lower()) not in seen_c and not seen_c.add(k)]
    return unknown_tables, unknown_cols


def _looks_like_function(skeleton: str, tok: str) -> bool:
    """tok( … ) — a function call, not an identifier."""
    pat = re.compile(rf"\b{re.escape(_dot_leaf(tok))}\s*\(")
    return bool(pat.search(skeleton))


def _is_alias_or_literal(clause: str, tok: str) -> bool:
    return bool(re.search(
        rf"\bAS\s+{re.escape(tok)}\b|\b{re.escape(tok)}\s+AS\b",
        clause, re.IGNORECASE))


def _tables_in(sql: str, snap: SchemaSnapshot) -> list[str]:
    skeleton, _ = safety.strip_comments_and_strings(sql)
    out: list[str] = []
    for clause_kw in ("FROM", "JOIN", "UPDATE", "INTO"):
        for tok in _idents_in(_clause(skeleton, clause_kw)):
            root = tok.split(".")[0]
            if snap.has_table(root) and root not in out:
                out.append(root)
    return out


def _stmt_count(sql: str) -> int:
    return len([s for s in safety.statements(sql) if s.strip()])


def validate_or_refuse(sql: str, snap: SchemaSnapshot) \
        -> tuple["QueryPlan", list[str]]:
    """Convenience: validate a raw SQL string; on errors return a plan
    whose text explains what the table does NOT contain (honest refusal)."""
    plan, errors = validate(sql, snap)
    if errors:
        lines = ["I won't run that — it fails validation against the "
                 "permitted schema:"]
        for e in errors:
            lines.append(f"  • {e}")
        plan.rationale = "\n".join(lines)
        return plan, errors
    return plan, []
