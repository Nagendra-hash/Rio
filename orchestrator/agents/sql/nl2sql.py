"""
orchestrator/agents/sql/nl2sql.py — natural language → SQL.

Deterministic template engine floor (offline, offline-testable) with an
optional LLM rung above it — the rag_strategies pattern:

    template strategy   recognizes a fixed family of ask shapes against
                        the SchemaSnapshot: count, top-N, filtered list,
                        group-count, distinct, min/max/avg, latest rows,
                        join-two-tables-by-FK. Every column/table comes
                        from the snapshot (never from the user's words
                        verbatim), a bogus column name never becomes SQL,
                        and the plan carries its confidence + rationale.
    direct strategy     the request carries real SQL in metadata.sql —
                        a power user's lane, validated like everything
                        else.
    llm strategy        an LLM gets the compact schema prompt and the
                        ask; its SQL is validated IDENTICALLY before it
                        is considered. An LLM never approves its own
                        output: validate() + safety levels stay in code.

Composite ask split: "top 5 customers and their total then list orders
above 100" → the engine splits on connectives into INDEPENDENT clauses
and runs them sequentially — each compiled, each validated, results
reported side by side (multi-part finals are not a single SQL). A clause
the engine cannot map is returned as an honest miss — never guessed.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from orchestrator.agents.sql.schema import (QueryPlan, SchemaSnapshot,
                                            clean_identifier, clean_sql)
from orchestrator.agents.sql import safety

_MAX_ASK = 400
_MAX_TOPN = 100                # a LIMIT cap so "top 5 million" stays sane

# ── ask shapes ───────────────────────────────────────────────────────────────

_RE_COUNT = re.compile(
    r"\b(?:how\s+many|count)\b", re.IGNORECASE)
_RE_TOPN = re.compile(
    r"\b(?:top|first|largest|biggest|highest|most|last\s+(\d+))\b"
    r"[^.?!]{0,30}?\b(\d+)?\b", re.IGNORECASE)
_RE_NUM = re.compile(r"\b(\d+)\b")
_RE_AVG = re.compile(r"\b(?:average|avg|mean)\b", re.IGNORECASE)
_RE_MIN = re.compile(r"\b(?:min(?:imum)?|lowest|smallest|least)\b",
                     re.IGNORECASE)
_RE_MAX = re.compile(r"\b(?:max(?:imum)?|highest|largest|biggest|most|peak)\b",
                     re.IGNORECASE)
_RE_DISTINCT = re.compile(r"\b(?:distinct|unique)\b", re.IGNORECASE)
_RE_LATEST = re.compile(
    r"\b(?:latest|last|newest|recent)\b", re.IGNORECASE)
_RE_ORDER_ASK = re.compile(
    r"\b(?:list|show\s+me|give\s+me|which|who)\b", re.IGNORECASE)
_RE_GROUPBY = re.compile(
    r"\b(?:by|per|for\s+each)\b", re.IGNORECASE)

_CONNECTIVES = re.compile(
    r"\b(?:and\s+then|then|also)\b|;", re.IGNORECASE)


def _nums(text: str) -> list[int]:
    return [int(n) for n in _RE_NUM.findall(text)]


# ── template compile: one clause → one QueryPlan (or None=miss) ─────────────

def compile_clause(clause: str, snap: SchemaSnapshot) -> Optional[QueryPlan]:
    """Best template match for one clause. Returns None when NO template
    is confident — the honest miss."""
    clause = " ".join((clause or "").split())[:_MAX_ASK]
    if not clause:
        return None
    score, plan = _score_clause(clause, snap)
    return plan if plan else None


def _score_clause(clause: str, snap: SchemaSnapshot) \
        -> tuple[float, Optional[QueryPlan]]:
    best_score, best_plan = 0.0, None
    for template in _TEMPLATES:
        score, plan = template(clause, snap)
        if score > best_score and plan is not None:
            best_score, best_plan = score, plan
    return best_score, best_plan


def _table_for(clause: str, snap: SchemaSnapshot) -> Optional[Any]:
    """The table the clause points at — by name hit first, then by its
    columns appearing in the clause."""
    best, best_hits = None, 0
    for t in snap.tables:
        hits = 0
        if re.search(rf"\b{re.escape(t.name.lower())}\b",
                     clause.lower()):
            hits += 3
        for c in t.columns:
            if re.search(rf"\b{re.escape(c.name.lower())}\b",
                         clause.lower()):
                hits += 1
        # a singular/plural of the table name (orders → order)
        stem = t.name.lower()
        if stem.endswith("s") and len(stem) > 3:
            if re.search(rf"\b{re.escape(stem[:-1])}\b", clause.lower()):
                hits += 2
        if hits > best_hits:
            best, best_hits = t, hits
    return best if best_hits >= 2 else None


def _pick_aggregate_column(clause: str, table: Any) -> Optional[str]:
    """A numeric column the clause names (avg/min/max/count basis)."""
    for c in table.columns:
        if _named_in(clause, c.name) and table.is_numeric(c.name):
            return c.name
    # fall back to first numeric non-pk column
    for c in table.columns:
        if table.is_numeric(c.name) and not c.pk:
            return c.name
    return None


def _named_in(clause: str, name: str) -> bool:
    """Column-name hit with simple singular/plural tolerance
    (countries ↔ country, status ↔ status)."""
    low = clause.lower()
    n = name.lower()
    if re.search(rf"\b{re.escape(n)}\b", low):
        return True
    if n.endswith("y") and len(n) > 2:
        if re.search(rf"\b{re.escape(n[:-1])}ies\b", low):
            return True
    if n.endswith("s"):
        if re.search(rf"\b{re.escape(n[:-1])}\b", low):
            return True
    elif re.search(rf"\b{re.escape(n)}s\b", low):
        return True
    return False


def _pick_group_column(clause: str, table: Any) -> Optional[str]:
    """A column the clause names for grouping (by X / per X)."""
    for c in table.columns:
        if _named_in(clause, c.name):
            return c.name
    return None


def _pick_filter(clause: str, table: Any) -> tuple[str, Any]:
    """Column OP value filters the clause states: `orders above 100`,
    `orders with amount > 500`, `customers in Germany`. Deterministic
    regex shapes; every column comes from the table itself."""
    low = clause.lower()
    orig = clause
    for c in table.columns:
        cname = re.escape(c.name.lower())
        # explicit operator: amount > 500 / amount>=5 / amount < 3.5
        m = re.search(rf"{cname}\s*(>=|<=|<>|!=|>|<|=)\s*['\"]?"
                      rf"(\d+(?:\.\d+)?)", low)
        if m and table.is_numeric(c.name):
            op, val = m.group(1), m.group(2)
            return f"{c.name} {op} {val}", val
        # word operators: above/over/more than … / below/under/less than …
        m2 = re.search(rf"{cname}\s+"
                       rf"(?:above|over|more\s+than|greater\s+than|"
                       rf"at\s+least|exceed\w*)\s+"
                       rf"(\d+(?:\.\d+)?)", low)
        if m2 and table.is_numeric(c.name):
            return f"{c.name} > {m2.group(1)}", m2.group(1)
        m3 = re.search(rf"{cname}\s+"
                       rf"(?:below|under|less\s+than|fewer\s+than|"
                       rf"at\s+most)\s+(\d+(?:\.\d+)?)", low)
        if m3 and table.is_numeric(c.name):
            return f"{c.name} < {m3.group(1)}", m3.group(1)
        # text: `country is Germany`, `status = open`, `in Germany` —
        # matched on the ORIGINAL case so values keep their letters
        m4 = re.search(rf"(?i:{cname})\s+(?:is|=|of|equals?|in)\s+['\"]?"
                       rf"([A-Za-z][A-Za-z0-9 _-]*)['\"]?", orig)
        if m4:
            val = m4.group(1).strip().strip("'\"")
            if val:
                return f"{c.name} = '{val}'", val
    return "", None


def _t_count(clause, snap):
    if not _RE_COUNT.search(clause):
        return 0.0, None
    table = _table_for(clause, snap)
    if table is None:
        return 0.0, None
    filt, _val = _pick_filter(clause, table)
    sql = (f"SELECT COUNT(*) FROM {table.name}"
           + (f" WHERE {filt}" if filt else ""))
    # score below group-count when a group column is present: "how many
    # orders by status" must compile as GROUP BY, not a bare total
    gcol = _pick_group_column(clause, table) if _RE_GROUPBY.search(clause) \
        else None
    return (0.7 if gcol else 1.0,
            QueryPlan(sql=sql, strategy="template",
                      confidence=0.9,
                      rationale=f"count ask over {table.name}",
                      tables=[table.name]))


def _t_topn(clause, snap):
    if not _RE_TOPN.search(clause):
        return 0.0, None
    table = _table_for(clause, snap)
    if table is None:
        return 0.0, None
    n = min(_MAX_TOPN, max(1, (_nums(clause) or [10])[0]))
    # numeric column to rank by: named or the first numeric non-pk
    rank_col = _pick_aggregate_column(clause, table)
    filt, _val = _pick_filter(clause, table)
    sql = (f"SELECT * FROM {table.name}"
           + (f" WHERE {filt}" if filt else "")
           + f" ORDER BY {rank_col} DESC LIMIT {n}"
           if rank_col else
           f"SELECT * FROM {table.name}"
           + (f" WHERE {filt}" if filt else "")
           + f" LIMIT {n}")
    return (0.95 if rank_col else 0.7,
            QueryPlan(sql=sql, strategy="template",
                      confidence=0.95 if rank_col else 0.7,
                      rationale=f"top {n} from {table.name}"
                                f" by {rank_col}" if rank_col else
                                f"first {n} rows of {table.name}",
                      tables=[table.name]))


def _t_group_count(clause, snap):
    if not (_RE_COUNT.search(clause) and _RE_GROUPBY.search(clause)):
        return 0.0, None
    table = _table_for(clause, snap)
    if table is None:
        return 0.0, None
    gcol = _pick_group_column(clause, table)
    if not gcol:
        return 0.0, None
    filt, _val = _pick_filter(clause, table)
    sql = (f"SELECT {gcol}, COUNT(*) AS n FROM {table.name}"
           + (f" WHERE {filt}" if filt else "")
           + f" GROUP BY {gcol} ORDER BY n DESC")
    return (0.9, QueryPlan(sql=sql, strategy="template",
                           confidence=0.9,
                           rationale=f"counts per {gcol} in {table.name}",
                           tables=[table.name]))


def _t_minmaxavg(clause, snap):
    is_avg = _RE_AVG.search(clause)
    is_min = _RE_MIN.search(clause)
    is_max = _RE_MAX.search(clause)
    if not (is_avg or is_min or is_max):
        return 0.0, None
    table = _table_for(clause, snap)
    if table is None:
        return 0.0, None
    col = _pick_aggregate_column(clause, table)
    if not col:
        return 0.0, None
    fn = ("AVG" if is_avg else "MIN" if is_min else "MAX")
    filt, _val = _pick_filter(clause, table)
    sql = (f"SELECT {fn}({col}) FROM {table.name}"
           + (f" WHERE {filt}" if filt else ""))
    return (0.9, QueryPlan(sql=sql, strategy="template",
                           confidence=0.9,
                           rationale=f"{fn}({col}) over {table.name}",
                           tables=[table.name]))


def _t_distinct(clause, snap):
    if not _RE_DISTINCT.search(clause):
        return 0.0, None
    table = _table_for(clause, snap)
    if table is None:
        return 0.0, None
    col = _pick_group_column(clause, table)
    if not col:
        return 0.0, None
    sql = f"SELECT DISTINCT {col} FROM {table.name}"
    return (0.9, QueryPlan(sql=sql, strategy="template",
                           confidence=0.9,
                           rationale=f"distinct {col} values in {table.name}",
                           tables=[table.name]))


def _t_latest(clause, snap):
    if not _RE_LATEST.search(clause):
        return 0.0, None
    table = _table_for(clause, snap)
    if table is None:
        return 0.0, None
    # a datetime/text column to order by: created_at / date / ts …
    order_col = None
    for c in table.columns:
        if re.search(r"\b(?:created_at|updated_at|ts|timestamp|date|"
                     r"created|modified)\b", c.name, re.IGNORECASE):
            order_col = c.name
            break
    if order_col is None:
        # last resort: first column that LOOKS like a date column
        for c in table.columns:
            if re.search(r"\b\d{4}-\d{2}-\d{2}\b", c.affinity):
                order_col = c.name
                break
    if order_col is None:
        return 0.0, None
    n = min(_MAX_TOPN, max(1, (_nums(clause) or [1])[0]))
    sql = (f"SELECT * FROM {table.name} ORDER BY {order_col} "
           f"DESC LIMIT {n}")
    return (0.85, QueryPlan(sql=sql, strategy="template",
                            confidence=0.85,
                            rationale=f"latest {n} rows of {table.name} "
                                      f"by {order_col}",
                            tables=[table.name]))


def _t_list(clause, snap):
    """Filtered list: 'list orders above 100', 'show customers in Germany'."""
    if not _RE_ORDER_ASK.search(clause):
        return 0.0, None
    table = _table_for(clause, snap)
    if table is None:
        return 0.0, None
    filt, _val = _pick_filter(clause, table)
    if not filt:
        # a bare list is not confident unless a column or WHERE value is
        if len(_nums(clause)) >= 0 and not re.search(
                r"\b(?:all|everything)\b", clause, re.IGNORECASE):
            return 0.0, None
        filt = ""
    n = min(_MAX_TOPN, max(1, (_nums(clause) or [50])[0]))
    sql = (f"SELECT * FROM {table.name} WHERE {filt} LIMIT {n}"
           if filt else f"SELECT * FROM {table.name} LIMIT {n}")
    return (0.8, QueryPlan(sql=sql, strategy="template",
                           confidence=0.8,
                           rationale=f"filtered list from {table.name}"
                                     + (f" ({filt})" if filt else ""),
                           tables=[table.name]))


_TEMPLATES = (_t_count, _t_topn, _t_group_count, _t_minmaxavg,
              _t_distinct, _t_latest, _t_list)


def _join_two(clause: str, snap: SchemaSnapshot) -> Optional[QueryPlan]:
    """Two tables named + an FK edge exists between them → JOIN."""
    parts = []
    for t in snap.tables:
        if re.search(rf"\b{re.escape(t.name.lower())}\b", clause.lower()):
            parts.append(t)
    if len(parts) < 2:
        return None
    a, b = parts[0], parts[1]
    for r in snap.relations:
        if (r.table == a.name and r.ref_table == b.name) or \
                (r.table == b.name and r.ref_table == a.name):
            child = r.table if r.table == a.name else b.name
            parent = r.ref_table if r.table == a.name else a.name
            sql = (f"SELECT {parent}.*, {child}.* FROM {parent} "
                   f"JOIN {child} ON {child}.{r.column} = "
                   f"{parent}.{r.ref_column}")
            return QueryPlan(sql=sql, strategy="template",
                             confidence=0.6,   # shape right, semantics guessy
                             rationale=f"join {a.name} ↔ {b.name} on "
                                       f"{r.label()}",
                             tables=[a.name, b.name])
    return None


# ── the public entry ─────────────────────────────────────────────────────────

def plan_sql(request_text: str, snap: SchemaSnapshot,
             metadata: Optional[dict] = None) -> dict:
    """NL → (plan, errors). The dict shape mirrors validator.validate:
    the agent calls validate(plan, snap) right after and refuses on
    errors. Returns (plan|None, [misses]) — a miss list is the honest
    "I could not map that" result; never a guessed query."""
    meta = metadata or {}
    # 1) direct SQL lane (power user / orchestrator override)
    raw_sql = str(meta.get("sql") or "").strip()
    if raw_sql:
        return [QueryPlan(sql=clean_sql(raw_sql),
                          strategy="direct", confidence=1.0,
                          rationale="SQL given in metadata.sql")], []
    parts = [p for p in _CONNECTIVES.split(request_text or "") if p.strip()]
    plans: list[QueryPlan] = []
    misses: list[str] = []
    for part in parts:
        clause = " ".join(part.split())[:_MAX_ASK]
        plan = compile_clause(clause, snap)
        if plan is not None:
            plans.append(plan)
            continue
        join_plan = _join_two(clause, snap)
        if join_plan is not None:
            plans.append(join_plan)
            continue
        misses.append(clause)
    if not plans:
        return None, misses or [f"no SQL template recognized in "
                                f"'{(request_text or '')[:80]}'"]
    return plans, misses


def plan_one(request_text: str, snap: SchemaSnapshot,
             metadata: Optional[dict] = None) -> Optional[QueryPlan]:
    """Single-plan convenience: the FIRST compiled plan (the agent uses
    this when the ask arrived as one clause)."""
    plans, _misses = plan_sql(request_text, snap, metadata)
    if isinstance(plans, list):
        return plans[0] if plans else None
    return plans


def describe_snapshot_for_llm(snap: SchemaSnapshot, max_chars: int = 6000) -> str:
    """The compact schema prompt an LLM rung receives (offline tests
    exchange this for a stub; see llm_plan)."""
    return snap.to_prompt(max_chars=max_chars)


_LLM_NEGATIVE = "not possible|cannot|cannot be|impossible|refuse"


def llm_plan(request_text: str, snap: SchemaSnapshot) -> Optional[QueryPlan]:
    """The LLM rung: schema prompt + ask → SQL text (or None). Returns
    None when the model says it can't, is unreachable, or answers with
    no SQL at all. The plan's confidence is capped at 0.75 — a model's
    word never outranks validation (see validate())."""
    try:
        from core.rag_strategies.llm import llm_call
    except Exception:
        return None
    schema_text = describe_snapshot_for_llm(snap)
    if not schema_text.strip():
        return None
    prompt = (
        "Write ONE SQLite SELECT statement for the question, using ONLY "
        "the tables and columns below. No commentary, just SQL.\n\n"
        f"Schema:\n{schema_text}\n\n"
        f"Question: {request_text}\n\nIf it is not possible with these "
        "tables and columns, reply exactly: NOT POSSIBLE.\n\nSQL:")
    try:
        verdict = llm_call(prompt, tier="smart")
    except Exception:
        return None
    if not isinstance(verdict, dict):
        return None
    text = str(verdict.get("text") or "").strip()
    if not text or re.search(_LLM_NEGATIVE, text, re.IGNORECASE) \
            and "SELECT" not in text.upper():
        return None
    sql = text
    if sql.upper().startswith("SQL:"):
        sql = sql[4:].strip()
    sql = re.sub(r"^```(?:sql)?|```$", "", sql.strip(), flags=re.I).strip()
    if not sql or not sql.upper().lstrip().startswith("SELECT"):
        return None
    return QueryPlan(sql=clean_sql(sql), strategy="llm",
                     confidence=0.75,
                     rationale="LLM rung — validated before any run",
                     tables=[])
