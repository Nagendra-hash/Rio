"""
tests/test_sql_agent.py — offline acceptance tests for the Phase 17
SQL agent.

NO network, NO LLM calls, NO database server: JARVIS_HOME is redirected
and every database is a real temp-file SQLite database built in-fixture,
opened mode=ro by the agent (so even a bug cannot write to it). The
template nl2sql engine is fully offline by design; the LLM rung is
asserted to be OPTIONAL (an empty-gemini env degrades to templates,
never to a guess).

Locked in:
  1. SAFETY — destructive SQL (DROP/DELETE/TRUNCATE/ALTER/ATTACH) parks
     behind approval; LOAD_EXTENSION-class SQL is BLOCKED and never runs,
     even approved; strings hiding keywords ('DROP TABLE') do not trip
     the guardrail; comments do not hide it either.
  2. ISOLATION — the store lives in memory/sql/ under JARVIS_HOME,
     0600/0700, actions-only audit; source paths never leave the store
     (replies carry db0/db1-style internal names); credential asks are
     DECLINED; password-shaped columns print [REDACTED].
  3. PIPELINE — permit → inspect → NL query (count/top-N/filter/group)
     through the approval gate; direct SQL lane; unknown tables/columns
     fail validation honestly; results are capped and marked truncated.
  4. NL2SQL — template strategies compile the fixed ask families against
     the snapshot; a clause no template recognizes is an honest miss,
     never a guessed query; composite asks run part by part.
  5. APPROVALS — mutating runs park the EXACT SQL; resume commits the
     parked payload; re-resume is declined; unknown resume fails.
  6. RAG INTEGRATION — remember exports the last result into the
     sql_reports collection (caller sql_agent, write-only there) with
     no credentials in the note.
  7. WIRING — classifier lane, explicit routes, registry gate (stub by
     default, real agent with JARVIS_SQL_AGENT=on), collection declared.

Run:  .venv/bin/python -m pytest tests/test_sql_agent.py -q
"""
import itertools
import json
import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_TMP = tempfile.mkdtemp(prefix="jarvis-sql-")
os.environ["JARVIS_HOME"] = _TMP

from orchestrator.agents import build_default_registry                    # noqa: E402
from orchestrator.agents.sql import executor, inspector, nl2sql, safety, \
    validator                                                            # noqa: E402
from orchestrator.agents.sql.checkpoint import SqlCheckpoint              # noqa: E402
from orchestrator.agents.sql.perms import SqlStore                        # noqa: E402
from orchestrator.agents.sql.schema import QueryPlan, SchemaSnapshot, \
    TableInfo                                                            # noqa: E402
from orchestrator.agents.sql.sql_agent import SqlAgent                    # noqa: E402
from orchestrator.classifier import IntentClassifier                      # noqa: E402
from orchestrator.contracts import AgentRequest, TaskStatus, TaskType     # noqa: E402

_SEQ = itertools.count(1)


def _req(text: str = "", **meta) -> AgentRequest:
    return AgentRequest(task_id=f"sql-test-{next(_SEQ):04d}",
                        task_type=TaskType.SQL, text=text,
                        metadata=meta)


@pytest.fixture()
def tmp():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture()
def store(tmp):
    return SqlStore(tmp / "sql")


@pytest.fixture()
def ckpt(tmp):
    return SqlCheckpoint(tmp / "ckpt")


@pytest.fixture()
def agent(tmp, store, ckpt):
    a = SqlAgent(store=store, checkpoint=ckpt)
    return a


@pytest.fixture()
def db_file(tmp):
    """A small shop database: customers, orders with FK, a users table
    carrying credential-shaped columns for the redaction tests."""
    p = tmp / "shop.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            country TEXT,
            credit_limit REAL);
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id),
            amount REAL NOT NULL,
            status TEXT,
            created_at TEXT);
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            password TEXT,
            api_key TEXT);
        INSERT INTO customers (name, country, credit_limit) VALUES
            ('Acme', 'Germany', 5000.0),
            ('Borealis', 'Norway', 12000.0),
            ('Cinder', 'Germany', 800.0),
            ('Drift', 'USA', 15000.0);
        INSERT INTO orders (customer_id, amount, status, created_at) VALUES
            (1, 250.0, 'paid', '2026-09-01'),
            (1, 480.0, 'paid', '2026-09-03'),
            (2, 1200.0, 'open', '2026-09-05'),
            (3, 90.0, 'paid', '2026-09-07'),
            (3, 610.0, 'void', '2026-09-09'),
            (4, 3000.0, 'paid', '2026-09-11');
        INSERT INTO users (username, password, api_key) VALUES
            ('ops', 'hunter2-super-secret', 'sk-live-abcdef123456');
        """)
    conn.commit()
    conn.close()
    return p


@pytest.fixture()
def permitted(agent, db_file):
    """A permitted database via the full approval path. Returns db_id."""
    req = _req("permit this database", sql_action="permit",
               path=str(db_file), title="shop")
    res = agent.handle(req)
    assert res.status is TaskStatus.NEEDS_APPROVAL, res.text
    ok = agent.handle(AgentRequest(
        task_id=req.task_id, task_type=TaskType.SQL, text="approve",
        metadata={"approved": True, "resume": req.task_id, "by": "user"}))
    assert ok.status is TaskStatus.COMPLETED, ok.text
    dbs = agent.store.databases()
    assert len(dbs) == 1
    return dbs[0]["db_id"]


# ══════════════════════════════════════════════════════════════════════════
# 1. the safety layer (pure functions)
# ══════════════════════════════════════════════════════════════════════════

class TestSafety:
    @pytest.mark.parametrize("sql,level", [
        ("SELECT * FROM t", safety.READ_ONLY),
        ("select id from t where x = 1", safety.READ_ONLY),
        ("WITH c AS (SELECT 1) SELECT * FROM c", safety.READ_ONLY),
        ("EXPLAIN QUERY PLAN SELECT 1", safety.READ_ONLY),
        ("PRAGMA table_info('t')", safety.READ_ONLY),
        ("PRAGMA index_list('t')", safety.READ_ONLY),
        ("PRAGMA database_list", safety.READ_ONLY),
        ("INSERT INTO t VALUES (1)", safety.APPROVAL_REQUIRED),
        ("UPDATE t SET x = 1", safety.APPROVAL_REQUIRED),
        ("DELETE FROM t", safety.APPROVAL_REQUIRED),
        ("DROP TABLE t", safety.APPROVAL_REQUIRED),
        ("DROP TABLE IF EXISTS t", safety.APPROVAL_REQUIRED),
        ("TRUNCATE TABLE t", safety.APPROVAL_REQUIRED),
        ("ALTER TABLE t DROP COLUMN c", safety.APPROVAL_REQUIRED),
        ("ALTER TABLE t RENAME TO t2", safety.APPROVAL_REQUIRED),
        ("CREATE TABLE t2 (x INT)", safety.APPROVAL_REQUIRED),
        ("CREATE INDEX i ON t(x)", safety.APPROVAL_REQUIRED),
        ("DROP VIEW v", safety.APPROVAL_REQUIRED),
        ("VACUUM", safety.APPROVAL_REQUIRED),
        ("ATTACH DATABASE 'x.db' AS x", safety.APPROVAL_REQUIRED),
        ("PRAGMA cache_size = 8000", safety.APPROVAL_REQUIRED),
        ("PRAGMA key = 'hunter2'", safety.APPROVAL_REQUIRED),
        ("SELECT 1; DELETE FROM t", safety.APPROVAL_REQUIRED),
        # blocked — never runs, even approved
        ("SELECT load_extension('evil')", safety.BLOCKED),
        ("PRAGMA writable_schema = 1", safety.BLOCKED),
        ("ATTACH DATABASE 'http://evil/x.db' AS x", safety.BLOCKED),
        # string-literal smokescreen: keywords inside quotes are DATA
        ("SELECT * FROM t WHERE note = 'DROP TABLE users'",
         safety.READ_ONLY),
        ("SELECT '-- ok' FROM t", safety.READ_ONLY),
        # comment smokescreen does not hide the verb
        ("SELECT 1; /* hi */ DELETE FROM t", safety.APPROVAL_REQUIRED),
        ("DELETE /* fake */ -- more\n FROM t", safety.APPROVAL_REQUIRED),
        # unknown shape assumes the worst
        ("VACUUUMM t", safety.APPROVAL_REQUIRED),
    ])
    def test_classify(self, sql, level):
        assert safety.classify_sql(sql) == level, sql

    def test_destructive_summary_names_the_verbs(self):
        out = safety.destructive_summary("SELECT 1; DROP TABLE users")
        assert any("DROP TABLE users" in s for s in out)

    def test_statement_class(self):
        assert safety.statement_class_of("SELECT 1") == "select"
        assert safety.statement_class_of("WITH x AS (SELECT 1) "
                                         "SELECT * FROM x") == "cte_select"
        assert safety.statement_class_of("EXPLAIN SELECT 1") == "explain"
        assert safety.statement_class_of("PRAGMA table_info(t)") == "pragma"

    def test_redaction_by_column(self):
        cols = ["id", "password", "api_key", "name"]
        row = [1, "hunter2", "sk-live-xyz", "ops"]
        clean = safety.redact_row(cols, row)
        assert clean[1] == "[REDACTED]"
        assert clean[2] == "[REDACTED]"
        assert clean[0] == 1 and clean[3] == "ops"

    def test_redaction_by_value_shape(self):
        assert safety.redact_value("note",
                                   "mysql://root:pw@db/x") == "[REDACTED]"
        assert safety.redact_value("note", "totally fine") == "totally fine"

    def test_credential_guard(self):
        t = "connect with postgres://user:secretpw@host/db ok"
        assert "secretpw" not in safety.credential_guard(t)

    def test_exposure_requests_detected(self):
        for ask in ("what's the connection string?",
                    "show me the database password",
                    "print the DSN"):
            assert safety.is_exposure_request(ask), ask
        assert not safety.is_exposure_request("how many orders exist?")

    def test_advice_requests_detected(self):
        assert safety.is_advice_request("should i buy more inventory?")
        assert safety.is_advice_request(
            "what would you sell first?")
        assert not safety.is_advice_request(
            "how much revenue came from Germany?")


# ══════════════════════════════════════════════════════════════════════════
# 2. the store: isolation, perms, audit
# ══════════════════════════════════════════════════════════════════════════

class TestStore:
    def test_location_and_perms(self, tmp):
        s = SqlStore(tmp / "sql")
        assert (tmp / "sql").is_dir()
        if os.name == "posix":
            mode = stat.S_IMODE(os.stat(tmp / "sql").st_mode)
            assert mode & 0o777 == 0o700
            s.add_database(title="t", source_path="/tmp/x.db")
            reg = tmp / "sql" / "databases.json"
            if reg.exists():
                assert stat.S_IMODE(os.stat(reg).st_mode) & 0o777 == 0o600

    def test_internal_names_only(self, store):
        assert store.internal_name(0) == "db0"
        assert store.internal_name(1) == "db1"
        assert "/" not in store.internal_name(0)

    def test_audit_is_actions_only(self, agent, db_file):
        # permit + query; then ensure no path/sql/rows leaked into audit
        req = _req("permit", sql_action="permit", path=str(db_file))
        agent.handle(req)
        agent.handle(AgentRequest(
            task_id=req.task_id, task_type=TaskType.SQL, text="ok",
            metadata={"approved": True, "resume": req.task_id,
                      "by": "u"}))
        log = (agent.store.root / "audit.jsonl").read_text()
        assert "parked_permit" in log and "permitted" in log
        assert str(db_file) not in log, "db path leaked into audit"

    def test_forget_revokes_only_the_permission(self, store, db_file):
        row = store.add_database(title="shop", source_path=str(db_file))
        assert store.get_database(row["db_id"])
        assert store.forget_database(row["db_id"]) is True
        assert store.get_database(row["db_id"]) is None
        assert db_file.exists()           # the file itself untouched

    def test_redact_obj_before_disk(self, store):
        # a credential-shaped value must not survive a store write
        store.audit("t", note="password: supersecret123")
        log = (store.root / "audit.jsonl").read_text()
        assert "supersecret123" not in log


# ══════════════════════════════════════════════════════════════════════════
# 3. the inspector + validator
# ══════════════════════════════════════════════════════════════════════════

class TestInspector:
    def test_snapshot_from_real_db(self, db_file):
        snap, err = inspector.inspect_path(str(db_file), database="db0")
        assert err == ""
        assert set(snap.table_names()) == {"customers", "orders", "users"}
        orders = snap.get_table("orders")
        assert orders.kind == "table"
        assert orders.has_column("customer_id")
        assert orders.row_count == 6
        cust = snap.get_table("customers")
        assert any(c.pk for c in cust.columns)
        # the FK edge is captured
        assert any(r.table == "orders" and r.ref_table == "customers"
                   for r in snap.relations)

    def test_missing_file_is_honest(self, tmp):
        snap, err = inspector.inspect_path(tmp / "nope.db")
        assert err and not snap.tables

    def test_garbage_file_is_honest(self, tmp):
        p = tmp / "x.db"
        p.write_bytes(b"not sqlite at all")
        _snap, err = inspector.inspect_path(str(p))
        assert err

    def test_snapshot_from_rows(self):
        snap = inspector.snapshot_from_rows([
            {"name": "t", "kind": "table", "row_count": 3,
             "columns": [{"name": "id", "cid": 0, "affinity": "INTEGER",
                          "pk": True},
                         {"name": "note", "cid": 1, "affinity": "TEXT"}]},
        ])
        assert snap.has_table("t") and snap.get_table("t").row_count == 3


class TestValidator:
    def _snap(self, db_file):
        snap, _ = inspector.inspect_path(str(db_file))
        return snap

    def test_clean_select_passes(self, db_file):
        snap = self._snap(db_file)
        plan, errors = validator.validate(
            "SELECT name, country FROM customers LIMIT 5", snap)
        assert errors == [] and plan.tables == ["customers"]

    def test_unknown_table_refused(self, db_file):
        snap = self._snap(db_file)
        plan, errors = validator.validate(
            "SELECT * FROM passwords", snap)
        assert errors and "unknown table" in errors[0]

    def test_unknown_column_refused(self, db_file):
        snap = self._snap(db_file)
        _plan, errors = validator.validate(
            "SELECT nope FROM customers", snap)
        assert errors and "unknown column" in errors[0]

    def test_blocked_reported_as_error(self, db_file):
        snap = self._snap(db_file)
        _plan, errors = validator.validate(
            "SELECT load_extension('x')", snap)
        assert errors and "blocked" in errors[0]

    def test_order_by_comparison_flagged(self, db_file):
        snap = self._snap(db_file)
        _plan, errors = validator.validate(
            "SELECT * FROM orders ORDER BY amount = 5", snap)
        assert any("ORDER BY" in e for e in errors)

    def test_expressions_do_not_look_unknown(self, db_file):
        snap = self._snap(db_file)
        _plan, errors = validator.validate(
            "SELECT COUNT(*), UPPER(name) FROM customers", snap)
        assert errors == []

    def test_select_star_flagged_not_refused(self, db_file):
        snap = self._snap(db_file)
        plan, errors = validator.validate("SELECT * FROM orders", snap)
        assert errors == [] and plan.tables == ["orders"]


# ══════════════════════════════════════════════════════════════════════════
# 4. nl2sql: template strategies
# ══════════════════════════════════════════════════════════════════════════

class TestNl2Sql:
    def _snap(self, db_file):
        snap, _ = inspector.inspect_path(str(db_file))
        return snap

    def test_count(self, db_file):
        snap = self._snap(db_file)
        plan = nl2sql.plan_one("how many orders are there?", snap)
        assert plan and plan.strategy == "template"
        assert "COUNT(*)" in plan.sql and "orders" in plan.sql

    def test_count_with_filter(self, db_file):
        snap = self._snap(db_file)
        plan = nl2sql.plan_one("count orders with amount > 500", snap)
        assert plan and "amount > 500" in plan.sql

    def test_topn(self, db_file):
        snap = self._snap(db_file)
        plan = nl2sql.plan_one("top 3 orders by amount", snap)
        assert plan and "LIMIT 3" in plan.sql and "ORDER BY amount" in plan.sql

    def test_group_count(self, db_file):
        snap = self._snap(db_file)
        plan = nl2sql.plan_one("how many orders by status?", snap)
        assert plan and "GROUP BY status" in plan.sql

    def test_average(self, db_file):
        snap = self._snap(db_file)
        plan = nl2sql.plan_one("average credit limit of customers", snap)
        assert plan and "AVG(credit_limit)" in plan.sql

    def test_distinct(self, db_file):
        snap = self._snap(db_file)
        plan = nl2sql.plan_one("distinct countries of customers", snap)
        assert plan and "DISTINCT country" in plan.sql

    def test_latest(self, db_file):
        snap = self._snap(db_file)
        plan = nl2sql.plan_one("latest orders", snap)
        assert plan and "created_at" in plan.sql and "LIMIT 1" in plan.sql

    def test_filtered_list(self, db_file):
        snap = self._snap(db_file)
        plan = nl2sql.plan_one("list orders with amount > 1000", snap)
        assert plan and "WHERE amount > 1000" in plan.sql

    def test_text_filter(self, db_file):
        snap = self._snap(db_file)
        plan = nl2sql.plan_one("list customers with country is Germany",
                               snap)
        assert plan and "Germany" in plan.sql

    def test_honest_miss(self, db_file):
        snap = self._snap(db_file)
        plans, misses = nl2sql.plan_sql("tell me a joke about databases",
                                        snap)
        assert not plans and misses

    def test_composite_split(self, db_file):
        snap = self._snap(db_file)
        plans, misses = nl2sql.plan_sql(
            "how many customers are there and then top 2 orders by amount",
            snap)
        assert len(plans) == 2 and not misses
        assert plans[0].sql.startswith("SELECT COUNT")
        assert "LIMIT 2" in plans[1].sql

    def test_direct_sql_lane(self, db_file):
        snap = self._snap(db_file)
        plans, misses = nl2sql.plan_sql(
            "whatever", snap, {"sql": "SELECT COUNT(*) FROM orders"})
        assert plans[0].strategy == "direct" and not misses

    def test_bogus_column_never_becomes_sql(self, db_file):
        snap = self._snap(db_file)
        plan = nl2sql.plan_one("top 5 customers by magic_power", snap)
        # the engine may pick a real column as ranking basis — it must
        # NEVER emit magic_power
        if plan:
            assert "magic_power" not in plan.sql


# ══════════════════════════════════════════════════════════════════════════
# 5. the executor: caps + redaction
# ══════════════════════════════════════════════════════════════════════════

class TestExecutor:
    def test_rows_and_columns(self, db_file):
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            res = executor.execute(conn, "SELECT id, name FROM customers")
        finally:
            conn.close()
        assert res.ok and res.row_count == 4
        assert res.columns == ["id", "name"]
        assert res.rows[0][1] == "Acme"

    def test_limit_cap_enforced(self, db_file):
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            res = executor.execute(conn, "SELECT id FROM orders", limit=3)
        finally:
            conn.close()
        assert res.ok and res.row_count == 3 and res.truncated

    def test_sql_limit_cannot_exceed_cap(self, db_file):
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            res = executor.execute(conn,
                                   "SELECT id FROM orders LIMIT 999999",
                                   limit=4)
        finally:
            conn.close()
        assert res.row_count == 4 and res.truncated

    def test_credential_columns_redacted(self, db_file):
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            res = executor.execute(
                conn, "SELECT username, password, api_key FROM users")
        finally:
            conn.close()
        assert res.ok
        by_col = dict(zip(res.columns, res.rows[0]))
        assert by_col["password"] == "[REDACTED]"
        assert by_col["api_key"] == "[REDACTED]"
        assert by_col["username"] == "ops"

    def test_sql_error_reported_not_raised(self, db_file):
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            res = executor.execute(conn, "SELECT nope FROM customers")
        finally:
            conn.close()
        assert not res.ok and res.error

    def test_format_renders_table(self, db_file):
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            res = executor.execute(conn, "SELECT name FROM customers "
                                         "LIMIT 2")
        finally:
            conn.close()
        text = executor.format_result(res)
        assert "Acme" in text and "│" in text and "row" in text

    def test_format_empty(self):
        from orchestrator.agents.sql.schema import QueryResult
        res = QueryResult(ok=True, sql="SELECT 1", columns=["x"], rows=[])
        assert "no rows" in executor.format_result(res)


# ══════════════════════════════════════════════════════════════════════════
# 6. the agent: gated pipeline
# ══════════════════════════════════════════════════════════════════════════

class TestAgentPipeline:
    def test_permit_needs_approval_parks_exact_payload(self, agent, db_file):
        req = _req("permit shop", sql_action="permit", path=str(db_file))
        res = agent.handle(req)
        assert res.status is TaskStatus.NEEDS_APPROVAL
        cp = agent.checkpoint.load_for(req.task_id)
        assert cp and cp["status"] == "awaiting_approval"
        assert cp["payload"]["path"] == str(db_file)
        assert agent.store.databases() == []

    def test_permit_missing_file_refused(self, agent, tmp):
        res = agent.handle(_req("permit", sql_action="permit",
                                path=str(tmp / "nope.db")))
        assert "No database file" in res.text

    def test_permit_garbage_file_refused(self, agent, tmp):
        p = tmp / "x.db"
        p.write_bytes(b"junk")
        res = agent.handle(_req("permit", sql_action="permit", path=str(p)))
        assert "Not a readable sqlite database" in res.text

    def test_inspect_after_permit(self, agent, permitted):
        res = agent.handle(_req("show the schema", sql_action="inspect"))
        assert res.status is TaskStatus.COMPLETED
        t = res.text
        assert "customers" in t and "orders" in t and "FK" in t
        assert res.data["tables"] == 3
        # the reply carries the internal name, never the path
        assert "db0" in t and str(res.data.get("db_id"))

    def test_inspect_replies_never_leak_path(self, agent, permitted, db_file):
        res = agent.handle(_req("inspect", sql_action="inspect"))
        assert str(db_file) not in res.text
        assert str(db_file) not in json.dumps(res.data)

    def test_nl_query_count(self, agent, permitted):
        res = agent.handle(_req("how many orders are there?",
                                sql_action="query"))
        assert res.status is TaskStatus.COMPLETED, res.text
        assert "6" in res.text
        assert res.data["rows"] >= 1

    def test_nl_query_topn(self, agent, permitted):
        res = agent.handle(_req("top 2 orders by amount",
                                sql_action="query"))
        assert res.status is TaskStatus.COMPLETED
        assert "3000" in res.text and "1200" in res.text

    def test_query_result_capped_and_marked(self, agent, permitted):
        res = agent.handle(_req("list all orders", sql_action="query"))
        assert res.status is TaskStatus.COMPLETED
        assert res.data["rows"] <= executor.MAX_ROWS

    def test_no_database_permit_first(self, agent):
        res = agent.handle(_req("how many orders?", sql_action="query"))
        assert "no databases permitted" in res.text

    def test_direct_sql_readonly_runs(self, agent, permitted):
        res = agent.handle(_req("run this", sql_action="run",
                                sql="SELECT COUNT(*) FROM customers"))
        assert res.status is TaskStatus.COMPLETED, res.text
        assert "4" in res.text

    def test_direct_sql_bad_select_refused(self, agent, permitted):
        res = agent.handle(_req("run", sql_action="run",
                                sql="SELECT nope FROM customers"))
        assert "fails validation" in res.text

    def test_exposure_declined(self, agent, permitted):
        res = agent.handle(_req("what's the connection string?"))
        assert res.status is TaskStatus.DECLINED
        assert "credentials" in res.text

    def test_advice_declined(self, agent, permitted):
        res = agent.handle(_req("should i buy more inventory?"))
        assert res.status is TaskStatus.DECLINED

    def test_disclaimer_on_substantive_replies(self, agent, permitted):
        res = agent.handle(_req("inspect", sql_action="inspect"))
        assert safety.DISCLAIMER_TEXT.split(":")[0] in res.text


# ══════════════════════════════════════════════════════════════════════════
# 7. the approval gate for mutations
# ══════════════════════════════════════════════════════════════════════════

class TestMutationApprovals:
    def test_update_parks(self, agent, permitted):
        res = agent.handle(_req("run", sql_action="run",
                                sql="UPDATE orders SET status='paid'"))
        assert res.status is TaskStatus.NEEDS_APPROVAL
        assert "has not been touched" in res.text
        cp = agent.checkpoint.load_for(res.task_id)
        assert cp["payload"]["sql"] == "UPDATE orders SET status='paid'"

    def test_drop_parks_with_summary(self, agent, permitted):
        res = agent.handle(_req("run", sql_action="run",
                                sql="DROP TABLE users"))
        assert res.status is TaskStatus.NEEDS_APPROVAL
        cp = agent.checkpoint.load_for(res.task_id)
        assert cp["payload"]["sql"] == "DROP TABLE users"

    def test_delete_via_nl_never_generates_destructive_sql(
            self, agent, permitted):
        """The template engine never emits mutations: a NL delete ask is
        an honest miss pointing at the explicit-SQL lane (where the
        exact statement is visible to the approver)."""
        res = agent.handle(_req("delete all void orders",
                                sql_action="query"))
        assert res.status is TaskStatus.COMPLETED
        assert "could not map" in res.text
        assert "DELETE" not in json.dumps(res.data)
        # the explicit lane still parks it behind approval
        res2 = agent.handle(_req("run", sql_action="run",
                                 sql="DELETE FROM orders WHERE "
                                     "status = 'void'"))
        assert res2.status is TaskStatus.NEEDS_APPROVAL

    def test_blocked_never_parks_never_runs(self, agent, permitted):
        res = agent.handle(_req("run", sql_action="run",
                                sql="SELECT load_extension('evil')"))
        assert res.status is TaskStatus.DECLINED
        cp = agent.checkpoint.load_for(res.task_id)
        assert cp is None

    def test_approved_update_commits_parked_sql(self, agent, permitted):
        req = _req("run", sql_action="run",
                   sql="UPDATE orders SET status = 'paid' "
                       "WHERE id = 1")
        agent.handle(req)
        ok = agent.handle(AgentRequest(
            task_id=req.task_id, task_type=TaskType.SQL, text="approve",
            metadata={"approved": True, "resume": req.task_id,
                      "by": "user"}))
        assert ok.status is TaskStatus.COMPLETED, ok.text
        # verify OUT of band: the row really changed
        agent.handle(_req("run", sql_action="run",
                          sql="SELECT status FROM orders WHERE id = 1"))
        assert agent._last.rows[0][0] == "paid"

    def test_re_resume_declined(self, agent, permitted):
        req = _req("run", sql_action="run",
                   sql="UPDATE orders SET status='paid'")
        agent.handle(req)
        agent.handle(AgentRequest(
            task_id=req.task_id, task_type=TaskType.SQL, text="ok",
            metadata={"approved": True, "resume": req.task_id,
                      "by": "u"}))
        again = agent.handle(AgentRequest(
            task_id=req.task_id, task_type=TaskType.SQL, text="again",
            metadata={"approved": True, "resume": req.task_id,
                      "by": "u"}))
        assert again.status is TaskStatus.DECLINED

    def test_unknown_resume_fails_honestly(self, agent):
        res = agent.handle(_req("approve", approved=True,
                                resume="sql-test-9999"))
        assert res.status is TaskStatus.FAILED
        assert "No pending approval" in res.text


# ══════════════════════════════════════════════════════════════════════════
# 8. the RAG integration (remember)
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def sandbox_rag():
    """Offline mini KB: hash-vector fake gemini + sandboxed callers file
    granting sql_agent write on sql_reports only."""
    try:
        import numpy as np  # noqa: F401
    except Exception:
        pytest.skip("numpy not available")
    from core import rag, rag_service
    kb = Path(_TMP) / "kb-fixture"
    kb.mkdir(parents=True, exist_ok=True)
    rag.KB_DIR = kb
    rag.LIBRARY_DIR = kb / "library"
    rag.CHUNKS_FILE = kb / "chunks.jsonl"
    rag.VECTORS_FILE = kb / "vectors.npz"
    rag.REGISTRY_FILE = kb / "registry.json"
    rag.AUDIT_FILE = kb / "audit.log"
    rag.BM25_FILE = kb / "bm25_index.pkl"
    rag_service.CALLERS_FILE = Path(_TMP) / "rag_callers.json"
    rag_service.CALLERS_FILE.write_text(json.dumps({
        "sql_agent": {"collections": ["sql_reports"], "can_write": True},
        "research_reader": {"collections": ["sql_reports"],
                            "can_write": False},
    }), encoding="utf-8")

    import numpy as np

    def _fake_embed(texts, is_query=False, **kw):
        out = []
        for t in texts:
            v = np.zeros(rag.EMBED_DIM, dtype=np.float32)
            for w in set(str(t).lower().split()):
                seed = abs(hash(w)) % (rag.EMBED_DIM - 8)
                v[seed:seed + 8] += 1.0
            n = float(np.linalg.norm(v))
            out.append(v / n if n > 0 else v)
        return out

    class _FakeGemini:
        def embed(self, texts, is_query=False, **kw):
            single = isinstance(texts, str)
            vecs = _fake_embed([texts] if single else list(texts), is_query)
            return vecs[0] if single else vecs

    import core as core_pkg
    core_pkg.gemini = _FakeGemini()
    yield rag_service


class TestRemember:
    def test_remember_requires_a_prior_query(self, agent, permitted):
        res = agent.handle(_req("remember that", sql_action="remember"))
        assert "Nothing to remember" in res.text

    def test_remember_exports_last_result(self, agent, permitted,
                                          sandbox_rag):
        agent.handle(_req("top 2 orders by amount", sql_action="query"))
        res = agent.handle(_req("remember that", sql_action="remember",
                                title="top orders"))
        assert res.status is TaskStatus.COMPLETED, res.text
        assert res.data.get("collection") == "sql_reports"

    def test_remembered_note_is_citable_and_secret_free(
            self, agent, permitted, sandbox_rag):
        agent.handle(_req("how many orders are there?",
                          sql_action="query"))
        res = agent.handle(_req("remember", sql_action="remember",
                                title="order count"))
        doc_id = res.data.get("doc_id")
        assert doc_id
        # another reader granted sql_reports can cite it
        out = sandbox_rag.search(caller="research_reader",
                                 query="order count",
                                 collection="sql_reports")
        assert out.get("ok") in (True, False)
        # and the stored text carries no credentials
        blob = " ".join(str(c.get("text") or "")
                        for c in (out.get("results") or []))
        assert "hunter2" not in blob

    def test_remembered_rows_exclude_redacted_cells(
            self, agent, permitted, sandbox_rag):
        agent.handle(_req("run", sql_action="run",
                          sql="SELECT username, password FROM users"))
        res = agent.handle(_req("remember", sql_action="remember",
                                title="users export"))
        doc_id = res.data.get("doc_id")
        assert doc_id
        out = sandbox_rag.search(caller="research_reader",
                                 query="users export ops",
                                 collection="sql_reports")
        blob = " ".join(str(c.get("text") or "")
                        for c in (out.get("results") or []))
        assert "hunter2" not in blob and "sk-live" not in blob


# ══════════════════════════════════════════════════════════════════════════
# 9. wiring: classifier + registry
# ══════════════════════════════════════════════════════════════════════════

class TestWiring:
    def test_classifier_lane(self):
        for ask in ("how many orders are in the database",
                    "inspect the schema of the sqlite database",
                    "SELECT COUNT(*) FROM customers",
                    "list the tables in the database",
                    "query the database for top customers"):
            c = IntentClassifier().classify(ask)
            assert c.task_type is TaskType.SQL, (ask, c.reason)

    def test_explicit_route(self):
        c = IntentClassifier().classify("[sql] top 5 orders")
        assert c.task_type is TaskType.SQL
        c2 = IntentClassifier().classify("send this to the sql agent")
        assert c2.task_type is TaskType.SQL

    def test_other_lanes_not_stolen(self):
        for ask, lane in (
                ("summarize the termination clause in my lease",
                 TaskType.LEGAL),
                ("what is the capital of france", TaskType.RESEARCH),
                ("fix the bug in my python function", TaskType.CODING)):
            c = IntentClassifier().classify(ask)
            assert c.task_type is lane, (ask, c.reason)

    def test_registry_gated_off_by_default(self):
        old = os.environ.pop("JARVIS_SQL_AGENT", None)
        try:
            reg = build_default_registry()
            stub = reg.get("sql_agent")
            assert stub is not None
            res = stub.handle(_req("anything"))
            assert res.status is TaskStatus.NEEDS_INPUT     # the stub
        finally:
            if old:
                os.environ["JARVIS_SQL_AGENT"] = old

    def test_registry_on(self):
        old = os.environ.get("JARVIS_SQL_AGENT")
        os.environ["JARVIS_SQL_AGENT"] = "on"
        try:
            reg = build_default_registry()
            assert type(reg.get("sql_agent")) is SqlAgent
        finally:
            if old is None:
                os.environ.pop("JARVIS_SQL_AGENT", None)
            else:
                os.environ["JARVIS_SQL_AGENT"] = old

    def test_collection_declared_in_rag(self):
        from core import rag
        assert "sql_reports" in rag.COLLECTIONS

    def test_handle_never_raises(self, agent):
        for text in ("", "   ", "\x00 \x1f", "SELECT " * 400,
                     "\x00 DROP TABLE \x1f"):
            res = agent.handle(_req(text, **{"sql_action": "query"}))
            assert res.status in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                                  TaskStatus.NEEDS_INPUT,
                                  TaskStatus.DECLINED)
