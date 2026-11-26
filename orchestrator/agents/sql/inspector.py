"""
orchestrator/agents/sql/inspector.py — the permitted-schema reader.

One job: capture what the AGENT may see of a permitted database, as a
SchemaSnapshot — tables, views, columns with types/PK, declared foreign
keys, and an honest row-count estimate. It is the ONLY module that
talks to the live connection for schema purposes, and it never sees a
credential: the caller (executor) hands it an already-open connection,
so the registry's source paths stay inside the store.

Read-only by construction:
    * every read uses `main` — nothing ATTACHes, nothing PRAGMAs out;
    * a missing/corrupt file is reported, never invented;
    * a view is marked kind=view and walked like a table.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from orchestrator.agents.sql.schema import (ColumnInfo, Relation,
                                            SchemaSnapshot, TableInfo,
                                            clean_identifier, validate_snapshot)

_MAX_TABLES = 500                 # absurd schema cap


def inspect(conn: sqlite3.Connection, database: str = "db0",
            readonly: bool = True) -> SchemaSnapshot:
    """Snapshot the whole schema of an open sqlite connection. A corrupt
    file surfaces as sqlite3.DatabaseError — the caller reports it."""
    snap = SchemaSnapshot(database=database, readonly=readonly)
    # a corrupt file fails HERE (connect is lazy): let it propagate
    rows = conn.execute(
        "SELECT name, type FROM sqlite_master "
        "WHERE type IN ('table','view') "
        "AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name").fetchall()
    for name, kind in rows[:_MAX_TABLES]:
        tname = clean_identifier(name)
        tinfo = TableInfo(name=tname,
                          kind="view" if str(kind).lower() == "view"
                          else "table")
        tinfo.columns = _columns_of(conn, tname)
        tinfo.row_count = _row_count(conn, tname)
        snap.tables.append(tinfo)
    snap.relations = _relations_of(conn)
    return snap


def inspect_path(db_path: str | Path, database: str = "db0") \
        -> tuple[SchemaSnapshot, str]:
    """Inspect a file directly — demo/test convenience. Returns
    (snapshot, error); error non-empty means it failed and the
    snapshot is empty. `mode=ro`Sqlite opens read-only here, so even
    the reader cannot write: immutable safety at the caller's own door."""
    p = Path(str(db_path)).expanduser()
    if not p.exists() or not p.is_file():
        return SchemaSnapshot(database=database), "no such file"
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            snap = inspect(conn, database=database)
        finally:
            conn.close()
        return snap, ""
    except sqlite3.Error as exc:
        return SchemaSnapshot(database=database), f"not a readable " \
            f"sqlite database ({exc})"


def _columns_of(conn: sqlite3.Connection, table: str) -> list[ColumnInfo]:
    cols: list[ColumnInfo] = []
    try:
        for cid, name, ctype, notnull, dflt, pk in conn.execute(
                f'PRAGMA table_info("{clean_identifier(table)}")'):
            col = ColumnInfo(
                name=clean_identifier(name), cid=int(cid or 0),
                affinity=str(ctype or "").upper()[:40],
                not_null=bool(notnull),
                default=clean_identifier(dflt or ""),
                pk=bool(int(pk or 0)))
            if col.name:
                cols.append(col)
    except sqlite3.Error:
        pass
    return cols


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(
            f'SELECT COUNT(*) FROM "{clean_identifier(table)}"'
        ).fetchone()[0])                    # noqa: E501 — sane budget
    except sqlite3.Error:
        return -1


def _relations_of(conn: sqlite3.Connection) -> list[Relation]:
    """Declared FK edges across every table (sqlite_master walk)."""
    rels: list[Relation] = []
    try:
        names = [clean_identifier(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        for tname in names:
            try:
                for row in conn.execute(
                        f'PRAGMA foreign_key_list("{tname}")'):
                    # (id, seq, table, from, to, on_update, on_delete, match)
                    child_col = clean_identifier(row[3])
                    parent_tbl = clean_identifier(row[2])
                    parent_col = clean_identifier(row[4] or row[3])
                    if tname and child_col and parent_tbl:
                        rels.append(Relation(
                            table=tname, column=child_col,
                            ref_table=parent_tbl, ref_column=parent_col))
            except sqlite3.Error:
                continue
    except sqlite3.Error:
        pass
    return rels


def snapshot_from_rows(raw: list[dict], database: str = "db0") \
        -> SchemaSnapshot:
    """Build a validated snapshot from a plain rows-of-dicts shape —
    the offline test/demo path (no connection needed at all)."""
    snap, _errs = validate_snapshot({"database": database,
                                     "tables": raw})
    return snap or SchemaSnapshot(database=database)


__all__ = ["inspect", "inspect_path", "snapshot_from_rows"]
