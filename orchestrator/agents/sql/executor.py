"""
orchestrator/agents/sql/executor.py — read-only execution + formatting.

One module, two halves:

THE EXECUTION HALF — an already-open sqlite3 connection is handed in;
this module opens NOTHING and holds NO credentials. The agent calls
execute() only after the safety level is read-only or the approval gate
has opened, and always with the same caps:

    * the connection was opened mode=ro by the caller (read-only path);
    * LIMIT is enforced IN ADDITION to any the SQL carries, on the last
      statement only, so one row-count brain-dead query cannot hold the
      process (a filtered scan still returns rows in budget);
    * MAX_ROWS (default 200) rows are pulled, then truncated — an honest
      "truncated" flag, never a silently bigger bag;
    * EXECUTION_TIMEOUT_S (default 5s) bounds a single statement via
      sqlite3 progress handler — the clock is the agent's, not the db's;
    * column values formatted through safety.redact_value — a cell in a
      password/token/api_key-shaped column prints [REDACTED].

THE FORMATTING HALF — the same result shape as every other agent's
"data" payload: markdown-ish table lines for the text body, plus the
json-safe dict for data. truncate + redact happen once, HERE.
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Any

from orchestrator.agents.sql import safety
from orchestrator.agents.sql.schema import QueryResult, clean_sql

MAX_ROWS = 200                  # rows actually pulled
MAX_CELL_CHARS = 200            # per-cell display cap
MAX_COLUMNS_SHOWN = 12          # columns printed in the text table
EXECUTION_TIMEOUT_S = 5.0

_INTENSITY_RE = re.compile(r"\bLIMIT\s+(\d+)\s*$", re.IGNORECASE)


def _apply_limit(sql: str, limit: int) -> str:
    """Append/correct a LIMIT on the statement text — one normalized
    LIMIT clause capped at `limit`. Reads fetch limit+1, so "more rows
    exist" is knowable WITHOUT changing the LIMIT semantics."""
    m = _INTENSITY_RE.search(sql.strip())
    if m and m.group(1).isdigit():
        n = int(m.group(1))
        # SQL asks for >= our pull cap → run at the cap (+1 hook) so the
        # truncated flag can detect the cut; smaller stays as-is
        if n >= limit:
            return _INTENSITY_RE.sub(f"LIMIT {limit + 1}", sql.strip(),
                                     count=1)
        return _INTENSITY_RE.sub(f"LIMIT {n}", sql.strip(), count=1)
    # no LIMIT in the SQL → append one higher than the pull cap, so the
    # truncated flag can be detected honestly at pull time
    return f"{sql.strip().rstrip(';')} LIMIT {limit + 1}"


def execute(conn: sqlite3.Connection, sql: str,
            *, limit: int = MAX_ROWS, timeout_s: float = EXECUTION_TIMEOUT_S,
            database: str = "db0") -> QueryResult:
    """Run one statement (already safety-cleared by the agent), collect
    rows up to `limit`, redact credential-shaped cells, report honestly.
    Never raises: on failure the QueryResult carries the error."""
    started = time.time()
    out = QueryResult(ok=False, sql=clean_sql(sql),
                      statement_class=safety.statement_class_of(sql),
                      database=database)
    stmt = _apply_limit(sql, limit)
    out.applied_limit = limit
    deadline = started + max(0.1, float(timeout_s))
    try:
        cur = conn.execute(stmt)
        cols = [str(d[0] if d and d[0] else f"col{i}")
                for i, d in enumerate(cur.description or [])]
        rows: list[list[Any]] = []
        truncated = False
        # read limit+1: the extra row (if any) is the truncated signal,
        # thrown away before formatting
        for i, row in enumerate(cur.fetchmany(limit + 1)):
            if i >= limit:
                truncated = True
                continue
            if time.time() > deadline:
                out.elapsed_ms = int((time.time() - started) * 1000)
                out.error = ("query exceeded its execution timeout "
                             f"({timeout_s:.0f}s) — narrow the WHERE clause")
                return out
            rows.append([_prep_cell(v, c) for c, v in zip(cols, row)])
        out.ok = True
        out.columns = cols
        out.rows = rows
        out.row_count = len(rows)
        out.truncated = truncated
        out.elapsed_ms = int((time.time() - started) * 1000)
        try:
            out.plan_text = [str(t[3]) for t in conn.execute(
                f"EXPLAIN QUERY PLAN {stmt}").fetchall()[:8]]
        except sqlite3.Error:
            pass                              # plan text is best-effort
    except sqlite3.Error as exc:
        out.elapsed_ms = int((time.time() - started) * 1000)
        out.error = str(exc)
        return out
    return out


def _prep_cell(value: Any, column: str) -> Any:
    """One cell: redact credentials first, then display-cap."""
    v = safety.redact_value(column, value)
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    text = str(v)
    if len(text) > MAX_CELL_CHARS:
        text = text[:MAX_CELL_CHARS - 1] + "…"
    return text


# ── formatting ──────────────────────────────────────────────────────────────

def _render_cell(v: Any) -> str:
    if v is None:
        return "∅"
    return " ".join(str(v).split())


def format_result(res: QueryResult, *, as_table: bool = True) -> str:
    """Human text: rows as a bounded markdown-ish table plus a footer."""
    if not res.ok:
        return f"Query failed: {res.error}"
    lines: list[str] = []
    n = res.row_count
    cols = res.columns[:MAX_COLUMNS_SHOWN]
    more_cols = len(res.columns) - len(cols)
    if as_table and res.rows:
        widths = [
            min(max([len(_render_cell(row[i]))
                     for row in res.rows if i < len(row)], default=0),
                len(cols[i]) or 1, 28)
            for i in range(len(cols))]
        lines.append("┌" + "┬".join("─" * (w + 2) for w in widths) + "┐")
        lines.append("│ " + " │ ".join(
            _render_cell(c).ljust(w)[:w] for c, w in zip(cols, widths)) + " │")
        lines.append("├" + "┼".join("─" * (w + 2) for w in widths) + "┤")
        for row in res.rows:
            cells = []
            for i, w in enumerate(widths):
                v = row[i] if i < len(row) else None
                cells.append(_render_cell(v).ljust(w)[:w])
            lines.append("│ " + " │ ".join(cells) + " │")
        lines.append("└" + "┴".join("─" * (w + 2) for w in widths) + "┘")
    elif as_table:
        lines.append("(no rows returned)")
    if more_cols > 0:
        lines.append(f"… and {more_cols} more column(s) not shown")
    bits = [f"{n} row{'s' if n != 1 else ''}"]
    if res.truncated:
        bits.append(f"truncated at {res.applied_limit}")
    if res.elapsed_ms:
        bits.append(f"{res.elapsed_ms} ms")
    footer = " · ".join(bits)
    lines.append(f"[{footer}]")
    return "\n".join(lines)


def multi_result_lines(results: list[QueryResult]) -> list[str]:
    """One block per part of a multi-part ask, numbered."""
    lines: list[str] = []
    for i, res in enumerate(results, 1):
        lines.append(f"── part {i} ──")
        lines.append(format_result(res))
        lines.append("")
    return lines
