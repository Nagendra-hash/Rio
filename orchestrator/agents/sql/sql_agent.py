"""
orchestrator/agents/sql/sql_agent.py — the real SQL agent (Phase 17) on
the BaseAgent contract under the canonical name `sql_agent`, enabled
with JARVIS_SQL_AGENT=on (shadow-mode default).

ACTIONS

    inspect (default)   snapshot the permitted schema: tables/views,
                        columns with types+PK, declared FKs, row counts.
    query               natural language → SQL (template engine or
                        metadata.sql) → validate → execute read-only →
                        formatted results. Multi-part asks run each
                        clause and report them side by side.
    explain             what a statement does, what it touches, its
                        safety level, its plan — NOTHING runs.
    run                 metadata.sql execution with the FULL gate read
                        first: read-only runs, mutating parks behind
                        approval, blocked refuses.
    permit              approval-gated ADD of a database file to the
                        permitted-database registry.
    databases           what's permitted right now (internal names only).
    forget_db           approval-gated REMOVE of a database permission.
    remember            RAG integration: export the LAST query's rows
                        as a text note into the sql_reports collection
                        (caller sql_agent, can_write — read-only ACL
                        for other agents), so Research and other RAG
                        callers can cite the numbers later.

SAFETY (all in code — never a prompt hope):
    * safety.classify_sql decides the level from the SQL itself.
    * exec-only-with-approval: mutating SQL runs ONLY through the
      checkpointed approval path: park the EXACT SQL at NEEDS_APPROVAL，
      resume executes the parked string, never a fresh parse; a second
      resume is DECLINED.
    * blocked SQL never runs, even approved.
    * credentials never leave memory/sql/: agent replies carry the
      db0/db1 internal names; connection strings are refused on ask.
    * every read line of every formatted result runs through the
      credential guard, and result cells redact credential-shaped
      columns/values.

handle() NEVER raises. Every reply ends with the read-only disclaimer.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

from orchestrator.agents.sql import (executor as sql_executor,
                                     inspector as sql_inspector,
                                     nl2sql, safety, validator)
from orchestrator.agents.sql.checkpoint import SqlCheckpoint
from orchestrator.agents.sql.perms import SqlStore, new_db_id
from orchestrator.agents.sql.schema import (QueryPlan, QueryResult,
                                            SchemaSnapshot, clean_sql)
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)

log = logging.getLogger("jarvis.sql")

MAX_SQL_CHARS = 20_000
RAG_COLLECTION = "sql_reports"        # exported results live here
REMEMBER_MAX_ROWS = 60


class SqlAgent(BaseAgent):
    name = "sql_agent"
    description = (
        "SQL agent: inspects permitted database schemas, turns natural "
        "language into SQL (deterministic templates, optional validated "
        "LLM rung), validates every statement against the snapshot and "
        "the guardrails, executes SELECT/read-only queries by default, "
        "formats results, and parks every mutating statement behind "
        "approval. Never exposes credentials; blocked SQL (LOAD_"
        "EXTENSION-class) never runs, even approved.")
    task_types = (TaskType.SQL,)

    def __init__(self, store: SqlStore | None = None,
                 checkpoint: SqlCheckpoint | None = None) -> None:
        self.store = store or SqlStore()
        self.checkpoint = checkpoint or SqlCheckpoint()

        self._last: QueryResult | None = None
        self._last_db_title: str = ""

    # ── entry ────────────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            meta = request.metadata or {}
            if meta.get("approved") and meta.get("resume"):
                return self._resume_approved(request, str(meta["resume"]),
                                             str(meta.get("by") or "user"))
            if safety.is_exposure_request(request.text):
                return self._decline_exposure(request)
            if safety.is_advice_request(request.text):
                return self._decline_advice(request)
            action = str(meta.get("sql_action")
                         or meta.get("sql_query_action") or "").strip().lower()
            if not action:
                action = self._intent(request.text)
            handler = self._HANDLERS.get(action, SqlAgent._query)
            return handler(self, request)
        except Exception as exc:                  # never raise outward
            log.warning("sql agent error: %s", type(exc).__name__)
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               error="sql action failed",
                               agent=self.name)

    @staticmethod
    def _intent(text: str) -> str:
        import re as _re
        t = " ".join((text or "").lower().split())
        if _re.search(r"\b(?:forget|revoke|remove)\s+(?:the\s+)?(?:db|database)\b", t) \
                or "forget_db" in t:
            return "forget_db"
        if _re.search(r"\b(?:permit|grant|allow|add)\b.{0,40}\b(?:database|db|sqlite|"
                      r"\.db)\b", t) or t.startswith("permit"):
            return "permit"
        if _re.search(r"\bexplain\b", t) or "explain" in t and _re.search(
                r"\b(sql|query|statement|select)\b", t):
            return "explain"
        if _re.search(r"\b(?:databases?|permitted|acl)\b", t) and \
                _re.search(r"\b(?:list|show|which|what)\b", t):
            return "databases"
        if _re.search(r"\b(?:schema|tables?|columns?|structure|inspect)\b", t):
            return "inspect"
        return "query"

    # ── helpers ──────────────────────────────────────────────────────────
    def _reply(self, request, text, *, action: str,
               status: TaskStatus = TaskStatus.COMPLETED,
               extra: dict | None = None) -> AgentResult:
        from orchestrator.agents.productivity import security as prod_sec
        body = prod_sec.redact(safety.credential_guard(str(text or "")))
        if status is TaskStatus.COMPLETED:
            body = (body.rstrip() + ("\n\n" if body else "")
                    + safety.DISCLAIMER_TEXT)
        return AgentResult(
            task_id=request.task_id, status=status, text=body,
            data={"sql": True, "action": action, **(extra or {})},
            agent=self.name)

    def _decline_exposure(self, request) -> AgentResult:
        self.store.audit("exposure_declined")
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.DECLINED,
            text=safety.EXPOSURE_REFUSAL,
            data={"sql": True, "action": "exposure_refused",
                  "refusal": True},
            agent=self.name)

    def _decline_advice(self, request) -> AgentResult:
        self.store.audit("advice_declined")
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.DECLINED,
            text=safety.ADVICE_REFUSAL,
            data={"sql": True, "action": "advice_refused",
                  "refusal": True},
            agent=self.name)

    def _park(self, request, action: str, payload: dict,
              detail: str) -> AgentResult:
        cp = {"task_id": request.task_id, "agent": self.name,
              "action": action, "payload": payload,
              "status": "awaiting_approval"}
        self.checkpoint.save_for(request.task_id, cp)
        self.store.audit(f"parked_{action}")
        return safety.approval_result(request, self.name, action, detail)

    def _pick_db(self, meta: dict) -> tuple[Optional[dict], str]:
        """Resolve the database a request points at: metadata.db_id wins,
        else the most recently permitted one.
        Returns (db_row, error)."""
        db_id = str(meta.get("db_id") or "").strip()
        if db_id:
            hit = self.store.get_database(db_id)
            if not hit:
                return None, f"database {db_id!r} is not permitted"
            return hit, ""
        dbs = self.store.databases()
        if not dbs:
            return None, ("no databases permitted yet — metadata "
                          "{sql_action: 'permit', path: '/path/to/file.db'} "
                          "adds one behind approval")
        newest = max(dbs, key=lambda r: str(r.get("added_at") or ""))
        return newest, ""

    def _open_reader(self, db_row: dict) -> tuple[Optional[sqlite3.Connection],
                                                  str]:
        """A mode=ro connection for a permitted sqlite database.
        Nothing else opens anything."""
        path = str(db_row.get("source_path") or "").strip()
        if db_row.get("kind") != "sqlite":
            return None, (f"unsupported database kind "
                          f"{db_row.get('kind')!r} — sqlite only for now")
        p = Path(path).expanduser()
        if not p.exists() or not p.is_file():
            return None, f"database file {p.name!r} is gone"
        try:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            return conn, ""
        except sqlite3.Error as exc:
            return None, f"cannot open {p.name!r} read-only: {exc}"

    def _snapshot(self, db_row: dict) -> tuple[Optional[SchemaSnapshot], str]:
        conn, err = self._open_reader(db_row)
        if err:
            return None, err
        try:
            snap = sql_inspector.inspect(conn, database="db0",
                                         readonly=bool(
                                             db_row.get("readonly", True)))
            return snap, ""
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _fmt(self, res: QueryResult) -> str:
        return sql_executor.format_result(res)

    # ── inspect ──────────────────────────────────────────────────────────
    def _inspect(self, request) -> AgentResult:
        meta = request.metadata or {}
        db_row, err = self._pick_db(meta)
        if err:
            return self._reply(request, err, action="inspect")
        snap, err = self._snapshot(db_row)
        if err:
            return self._reply(request, f"Cannot inspect: {err}",
                               action="inspect")
        self.store.audit("inspected", db_id=db_row["db_id"],
                         tables=len(snap.tables))
        lines = snap.to_lines()
        return self._reply(request, "\n".join(lines), action="inspect",
                           extra={"db_id": db_row["db_id"],
                                  "tables": len(snap.tables),
                                  "relations": len(snap.relations)})

    # ── databases / permit / forget_db ───────────────────────────────────
    def _databases(self, request) -> AgentResult:
        rows = self.store.databases()
        lines = [f"Permitted databases: {len(rows)} (files stay in the "
                 "private store — replied as internal names only)."]
        if not rows:
            lines.append("  (none — metadata {sql_action: 'permit', "
                         "path: …} adds one, with your approval)")
        for r in rows:
            ro = "read-only" if r.get("readonly", True) else "approval for writes"
            lines.append(f"  • {r['db_id']}  {r.get('title')} "
                         f"[{r.get('kind')} · {ro}]")
        self.store.audit("databases_listed", n=len(rows))
        return self._reply(request, "\n".join(lines), action="databases",
                           extra={"databases": len(rows)})

    def _permit(self, request) -> AgentResult:
        meta = request.metadata or {}
        path = str(meta.get("path") or "").strip()
        if not path:
            att = [a for a in (request.attachments or []) if a]
            if att:
                path = att[0]
        if not path:
            return self._reply(
                request,
                "Which database? metadata {sql_action: 'permit', path: "
                "'/path/to/data.db', title?: '…'} — or attach the file.",
                action="permit", status=TaskStatus.NEEDS_INPUT)
        p = Path(path).expanduser()
        if not p.exists() or not p.is_file():
            return self._reply(request, f"No database file at {path}.",
                               action="permit")
        snap, snap_err = sql_inspector.inspect_path(str(p), database="db0")
        if snap_err:
            return self._reply(request,
                               f"Not a readable sqlite database: {snap_err}",
                               action="permit")
        title = str(meta.get("title") or "").strip() or p.stem[:160]
        detail = (f"permit database file '{p.name}' ({len(snap.tables)} "
                  "tables/views) for the SQL agent — read-only queries "
                  "always allowed, writes parked behind approval. "
                  "Nothing is recorded until you approve.")
        return self._park(request, "permit_database",
                          {"path": str(p), "title": title,
                           "db_id": new_db_id(title),
                           "tables": len(snap.tables)}, detail)

    def _forget_db(self, request) -> AgentResult:
        meta = request.metadata or {}
        db_id = str(meta.get("db_id") or "").strip()
        if not db_id:
            dbs = self.store.databases()
            if not dbs:
                return self._reply(request,
                                   "No databases are permitted — nothing "
                                   "to forget.", action="forget_db")
            return self._reply(request,
                               "Which database? metadata {db_id: 'db-…'} — "
                               "'databases' lists them.",
                               action="forget_db", status=TaskStatus.NEEDS_INPUT)
        hit = self.store.get_database(db_id)
        if not hit:
            return self._reply(request,
                               f"Database {db_id!r} is not permitted.",
                               action="forget_db")
        detail = (f"revoke SQL-agent permission for database "
                  f"{hit.get('title')!r} [{db_id}] (the file itself is "
                  "not deleted)")
        return self._park(request, "forget_database",
                          {"db_id": db_id}, detail)

    def _resume_approved(self, request, resume_id: str, by: str) -> AgentResult:
        # The checkpoint is keyed by the task id the dispatcher used when
        # the action was PARKED. Only the parked payload executes.
        cp = None
        candidates = [resume_id] if resume_id and resume_id != "true" else []
        if request.task_id and request.task_id not in candidates:
            candidates.insert(0, request.task_id)
        for cand in candidates:
            hit = self.checkpoint.load_for(cand)
            if hit and hit.get("agent") == self.name:
                cp = hit
                resume_id = cand
                break
        if not cp:
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.FAILED,
                text=f"No pending approval checkpoint for {resume_id!r} "
                     "— nothing changed.", agent=self.name)
        if cp.get("status") != "awaiting_approval":
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.DECLINED,
                text=f"Approval {resume_id!r} is '{cp.get('status')}' — "
                     "already handled.", agent=self.name)
        action = cp.get("action")
        payload = cp.get("payload") or {}

        if action == "permit_database":
            return self._commit_permit(request, resume_id, payload, by)
        if action == "forget_database":
            return self._commit_forget(request, resume_id, payload, by)
        if action == "run_sql":
            return self._commit_run(request, resume_id, payload, by)
        cp["status"] = "failed"
        self.checkpoint.save_for(resume_id, cp)
        return AgentResult(task_id=request.task_id, status=TaskStatus.FAILED,
                           text=f"Unknown approved action {action!r}.",
                           agent=self.name)

    # ── commit paths (the ONLY writes) ───────────────────────────────────
    def _commit_permit(self, request, resume_id: str, payload: dict,
                       by: str) -> AgentResult:
        p = Path(str(payload.get("path") or "")).expanduser()
        snap, snap_err = sql_inspector.inspect_path(str(p), database="db0")
        if snap_err:
            return self._reply(
                request, f"Permit failed (approved by {by}): the file is "
                         f"no longer a readable sqlite database ({snap_err}). "
                         "Nothing was recorded.", action="permit")
        row = self.store.add_database(
            title=str(payload.get("title") or p.stem),
            source_path=str(p), kind="sqlite", ro=True,
            db_id=str(payload.get("db_id") or new_db_id(p.stem)))
        cp = {"task_id": resume_id, "agent": self.name,
              "action": "permit_database", "payload": payload,
              "status": f"permitted:{row['db_id']}"}
        self.checkpoint.save_for(resume_id, cp)
        self.store.audit("permitted", db_id=row["db_id"],
                         tables=len(snap.tables))
        return self._reply(
            request,
            f"Permitted (approved by {by}): database "
            f"{row['db_id']} — '{row['title']}' "
            f"({len(snap.tables)} tables/views). Read-only queries run "
            "without asking; writes will park behind approval.",
            action="permit",
            extra={"db_id": row["db_id"], "tables": len(snap.tables)})

    def _commit_forget(self, request, resume_id: str, payload: dict,
                       by: str) -> AgentResult:
        db_id = str(payload.get("db_id") or "")
        hit = self.store.get_database(db_id)
        self.store.forget_database(db_id)
        cp = {"task_id": resume_id, "agent": self.name,
              "action": "forget_database", "payload": payload,
              "status": "forgotten"}
        self.checkpoint.save_for(resume_id, cp)
        title = (hit or {}).get("title") or db_id
        return self._reply(request,
                           f"Revoked (approved by {by}): permission for "
                           f"database '{title}' [{db_id}]. Its file was "
                           "not touched.", action="forget_db")

    def _commit_run(self, request, resume_id: str, payload: dict,
                    by: str) -> AgentResult:
        sql_txt = str(payload.get("sql") or "")
        db_row, err = self._pick_db(dict(payload))
        if err:
            return self._reply(request, f"Run failed (approved by {by}): "
                                        f"{err}", action="run")
        # re-classify AT EXECUTION TIME: the approval covered THIS blob,
        # and a parked payload does not change under us.
        level = safety.classify_sql(sql_txt)
        if level == safety.BLOCKED:
            cp = {"task_id": resume_id, "agent": self.name,
                  "action": "run_sql", "payload": payload,
                  "status": "refused_blocked"}
            self.checkpoint.save_for(resume_id, cp)
            self.store.audit("blocked_at_execute", db_id=db_row["db_id"])
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.DECLINED,
                text=("Blocked SQL was parked for approval — refused at "
                      "execution time. Loads code from outside the "
                      "database or tampers with engine safety settings; "
                      "it never runs, even approved."),
                data={"sql": True, "action": "run", "refusal": True},
                agent=self.name)
        conn, err = self._open_reader(db_row)
        if err:
            return self._reply(request, f"Run failed (approved by {by}): "
                                        f"{err}", action="run")
        try:
            res = sql_executor.execute(conn, sql_txt,
                                       database=self.store.internal_name(0))
            res.database = self.store.internal_name(0)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        cp = {"task_id": resume_id, "agent": self.name,
              "action": "run_sql", "payload": payload,
              "status": (f"ran_ok:{res.row_count}" if res.ok
                         else f"ran_err:{str(res.error)[:40]}")}
        self.checkpoint.save_for(resume_id, cp)
        self.store.audit("mutation_executed" if res.ok else "failed_execute",
                         db_id=db_row["db_id"], status=res.ok, rows=res.row_count)
        head = (f"Ran (approved by {by}): " if res.ok
                else "Run failed (approved by {by}): ").format(by=by)
        return self._reply(request, head + "\n" + self._fmt(res),
                           action="run",
                           extra={"statement_class": res.statement_class,
                                  "rows": res.row_count,
                                  "db_id": db_row["db_id"]})

    # ── explain ──────────────────────────────────────────────────────────
    def _explain(self, request) -> AgentResult:
        meta = request.metadata or {}
        sql_txt = str(meta.get("sql") or "").strip()
        if not sql_txt:
            sql_txt = self._sql_from_text(request.text).strip()
        if not sql_txt:
            return self._reply(request,
                               "Which SQL? metadata {sql_action: 'explain', "
                               "sql: 'SELECT …'} — nothing runs to explain "
                               "it.", action="explain",
                               status=TaskStatus.NEEDS_INPUT)
        sql_txt = clean_sql(sql_txt)
        db_row, err = self._pick_db(meta)
        if err:
            return self._reply(request, err, action="explain")
        snap, snap_err = self._snapshot(db_row)
        if snap_err:
            return self._reply(request, f"Cannot explain: {snap_err}",
                               action="explain")
        plan, errors = validator.validate(QueryPlan(sql=sql_txt,
                                                    strategy="explain-only"),
                                          snap)
        level = safety.classify_sql(sql_txt)
        lines = [f"Statement ({safety.statement_class_of(sql_txt)}):",
                 f"  {re.sub(r'[ ]+', ' ', sql_txt)[:400]}", "",
                 f"Safety level: {level}"]
        if errors:
            lines.append("Validation:")
            for e in errors:
                lines.append(f"  • {e}")
        else:
            lines.append("Validation: clean against the permitted schema")
        if level == safety.READ_ONLY:
            lines.append("This is read-only — it would run without asking.")
        elif level == safety.BLOCKED:
            lines.append("This is BLOCKED — never runs, even approved.")
        else:
            lines.append("This would park behind approval before it runs.")
        self.store.audit("explained", class_=plan.statement_class,
                         level=level)
        return self._reply(request, "\n".join(lines), action="explain",
                           extra={"level": level, "errors": errors})

    # ── run / query ──────────────────────────────────────────────────────
    def _run(self, request) -> AgentResult:
        meta = request.metadata or {}
        sql_txt = str(meta.get("sql") or "").strip()
        db_row, err = self._pick_db(meta)
        if err:
            return self._reply(request, err, action="run")
        if not sql_txt:
            sql_txt = self._sql_from_text(request.text).strip()
        if not sql_txt:
            return self._reply(request,
                               "Which SQL? metadata {sql_action: 'run', "
                               "sql: 'SELECT …'}.", action="run",
                               status=TaskStatus.NEEDS_INPUT)
        if len(sql_txt) > MAX_SQL_CHARS:
            return self._reply(request,
                               f"SQL exceeds {MAX_SQL_CHARS} chars — "
                               "narrow it first.", action="run")
        sql_txt = clean_sql(sql_txt)
        level = safety.classify_sql(sql_txt)
        if level == safety.BLOCKED:
            self.store.audit("blocked_run_declined")
            return AgentResult(
                task_id=request.task_id, status=TaskStatus.DECLINED,
                text=("Blocked SQL: loads code from outside the database "
                      "or tampers with engine safety settings. Never runs, "
                      "even approved."),
                data={"sql": True, "action": "run", "refusal": True},
                agent=self.name)
        if level == safety.APPROVAL_REQUIRED:
            detail = (f"run {safety.classify_sql(sql_txt).upper()} against "
                      f"db '{db_row.get('title')}': "
                      + "; ".join(safety.destructive_summary(sql_txt)[:3]))
            return self._park(request, "run_sql",
                              {"sql": sql_txt, "db_id": db_row["db_id"]},
                              detail)
        snap, snap_err = self._snapshot(db_row)
        if snap_err:
            return self._reply(request, f"Cannot run: {snap_err}",
                               action="run")
        plan, errors = validator.validate(QueryPlan(sql=sql_txt,
                                                    strategy="direct"),
                                          snap)
        if errors:
            lines = ["I won't run that — it fails validation against the "
                     "permitted schema:"]
            for e in errors:
                lines.append(f"  • {e}")
            return self._reply(request, "\n".join(lines), action="run")
        conn, err = self._open_reader(db_row)
        if err:
            return self._reply(request, f"Cannot run: {err}", action="run")
        try:
            res = sql_executor.execute(conn, sql_txt,
                                       database=self.store.internal_name(0))
        finally:
            try:
                conn.close()
            except Exception:
                pass
        self.store.audit("ran_readonly", db_id=db_row["db_id"],
                         class_=res.statement_class, rows=res.row_count,
                         ok=res.ok)
        if res.ok and res.rows:
            self._last = res
            self._last_db_title = str(db_row.get("title") or "")
        return self._reply(request, ("" if res.ok else
                                     f"Query failed: {res.error}")
                           + ("\n" + self._fmt(res) if res.ok else ""),
                           action="run",
                           extra={"statement_class": res.statement_class,
                                  "rows": res.row_count,
                                  "columns": res.columns[:24]})

    def _query(self, request) -> AgentResult:
        meta = request.metadata or {}
        ask = str(meta.get("query") or request.text or "").strip()
        db_row, err = self._pick_db(meta)
        if err:
            return self._reply(request, err, action="query")
        # a typed-SQL fast path: a text that IS a SELECT
        if ask.upper().lstrip().startswith(("SELECT", "WITH", "EXPLAIN",
                                            "PRAGMA")):
            return self._run(request)
        snap, snap_err = self._snapshot(db_row)
        if snap_err:
            return self._reply(request, f"Cannot query: {snap_err}",
                               action="query")
        plans, misses = nl2sql.plan_sql(ask, snap, meta)
        if not plans:
            return self._reply(
                request,
                ("I could not map that to any query over the permitted "
                 "schema:\n  • " + "\n  • ".join(misses[:3])
                 + "\nSay 'inspect' to see what I may query, or give me "
                   "explicit SQL (metadata {sql: 'SELECT …}')."),
                action="query", status=TaskStatus.NEEDS_INPUT
                if not misses else TaskStatus.COMPLETED)
        results: list[QueryResult] = []
        for plan in plans:
            level = safety.classify_sql(plan.sql)
            if level == safety.BLOCKED:
                return AgentResult(
                    task_id=request.task_id, status=TaskStatus.DECLINED,
                    text="A generated statement was blocked by the "
                         "guardrail — nothing ran.",
                    data={"sql": True, "action": "query",
                          "refusal": True}, agent=self.name)
            if level == safety.APPROVAL_REQUIRED:
                detail = ("run " + "; ".join(
                    safety.destructive_summary(plan.sql)[:2]) +
                    f" against db '{db_row.get('title')}'")
                return self._park(request, "run_sql",
                                  {"sql": plan.sql,
                                   "db_id": db_row["db_id"]}, detail)
            _plan, errors = validator.validate(plan, snap)
            if errors:
                lines = ["Could not run this part — it fails validation:",
                         f"  SQL: {plan.sql[:200]}"]
                for e in errors:
                    lines.append(f"  • {e}")
                results.append(QueryResult(
                    ok=False, sql=plan.sql, database="db0",
                    error="validation failed: " + "; ".join(errors)[:160]))
                continue
            conn, err = self._open_reader(db_row)
            if err:
                return self._reply(request, f"Cannot query: {err}",
                                   action="query")
            try:
                res = sql_executor.execute(
                    conn, plan.sql, database=self.store.internal_name(0))
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            results.append(res)
        ran_rows = sum(r.row_count for r in results if r.ok)
        ran_ok = sum(1 for r in results if r.ok)
        failed = [r for r in results if not r.ok]
        self.store.audit("queried", parts=len(plans), ok=ran_ok,
                         rows=ran_rows, misses=len(misses))
        if len(results) == 1:
            body = self._fmt(results[0])
        else:
            body = "\n".join(sql_executor.multi_result_lines(results))
        for r in results:
            if r.ok and r.rows:
                self._last = r
                self._last_db_title = str(db_row.get("title") or "")
        if failed:
            body += (f"\n\n{len(failed)} part(s) failed validation — "
                     "shown above, nothing ran for them.")
        if misses:
            body += ("\n\nParts I could not map to SQL (left unrun):\n  • "
                     + "\n  • ".join(misses[:3]))
        return self._reply(request, body.strip(), action="query",
                           extra={"parts": len(results), "ok": ran_ok,
                                  "rows": ran_rows, "misses": len(misses)})

    # ── remember: the RAG integration ────────────────────────────────────
    def _remember(self, request) -> AgentResult:
        """Export the last query's rows into the sql_reports collection
        as a text note (caller sql_agent, can_write=true) so Research
        and other RAG callers can cite the numbers later. The SQL text
        and the db internal name ship in the note; no credentials, no
        data not already shown to the user."""
        meta = request.metadata or {}
        res = self._last
        if not (res and res.ok and res.rows):
            return self._reply(
                request,
                "Nothing to remember yet — run a query first; the result "
                "of the most recent successful query is what gets stored.",
                action="remember")
        title = (str(meta.get("title") or "").strip()
                 or f"sql query {res.statement_class} "
                    f"{self._last_db_title}".strip())[:120]
        lines = [f"# {title}", "",
                 f"Source: SQL agent, db {res.database}, "
                 f"{res.row_count} rows, {res.elapsed_ms} ms.", "",
                 "```sql", res.sql[:2_000], "```", ""]
        cols = res.columns
        for row in res.rows[:REMEMBER_MAX_ROWS]:
            cells = [f"{c}: {v}" for c, v in zip(cols, row)
                     if v is not None and str(v) != "[REDACTED]"]
            if cells:
                lines.append("- " + "; ".join(cells))
        if res.row_count > REMEMBER_MAX_ROWS:
            lines.append(f"(truncated to {REMEMBER_MAX_ROWS} of "
                         f"{res.row_count} rows)")
        text = "\n".join(lines)
        try:
            from core import rag_service
            result = rag_service.ingest_text(
                caller=self.name, text=text, title=title,
                collection=RAG_COLLECTION, source="sql_agent",
                metadata={"sql_export": True,
                          "db": res.database})
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        self.store.audit("remembered", ok=bool(result.get("ok")))
        if not result.get("ok"):
            return self._reply(
                request,
                f"Could not store the note: {result.get('error')}. Grant "
                f"'sql_agent' write on the {RAG_COLLECTION} collection in "
                "config/rag_callers.json.", action="remember")
        return self._reply(
            request,
            f"Stored '{title}' into the {RAG_COLLECTION} collection "
            f"({res.row_count} rows summarized). Research agents with "
            f"{RAG_COLLECTION} in their grant can cite it now.",
            action="remember",
            extra={"doc_id": result.get("doc_id"),
                   "collection": RAG_COLLECTION})

    def _sql_from_text(self, text: str) -> str:
        """Peel real SQL out of a mixed ask: backticks/fences or a bare
        statement head (SELECT/WITH/EXPLAIN/PRAGMA…)."""
        t = str(text or "")
        m = re.search(r"```(?:sql)?\s*([\s\S]*?)```", t, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        m = re.search(r"`([^`]{5,})`", t)
        if m:
            return m.group(1).strip()
        head = re.match(r"\s*(SELECT\b|WITH\b|EXPLAIN\b|PRAGMA\b|"
                        r"INSERT\b|UPDATE\b|DELETE\b|DROP\b|CREATE\b|"
                        r"ALTER\b|ATTACH\b)[\s\S]*$", t, re.IGNORECASE)
        if head:
            return t.strip()
        return ""

    _HANDLERS = {
        "inspect": _inspect,
        "schema": _inspect,
        "tables": _inspect,
        "query": _query,
        "explain": _explain,
        "run": _run,
        "databases": _databases,
        "permit": _permit,
        "forget_db": _forget_db,
        "forget_database": _forget_db,
        "remember": _remember,
        "export": _remember,
    }
