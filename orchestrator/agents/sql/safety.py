"""
orchestrator/agents/sql/safety.py — the Phase 17 guardrail layer.

Three jobs, all baked into CODE (the finance/legal-agent pattern):

1. THE DESTRUCTIVE-SQL GUARDRAIL — default to read-only. classify_sql()
   puts every statement into one of three permission levels, decided by
   the SQL itself — never by what the user said and never by a model:

       READ_ONLY            SELECT, WITH…SELECT, EXPLAIN, PRAGMA
                            introspection (table_info/index/stats…).
                            No approval needed. Nothing is written.
       APPROVAL_REQUIRED    anything that mutates: INSERT / UPDATE /
                            DELETE / DROP / TRUNCATE / ALTER (incl.
                            destructive shapes like `DROP COLUMN`,
                            `ALTER TABLE … RENAME`, triggers/views/
                            indexes creates+drops), ATTACH/DETACH,
                            VACUUM/REINDEX, non-introspection PRAGMAs,
                            multi-statement scripts (any script that
                            contains a mutating statement).
       BLOCKED              ATTACH on non-file URLs, PRAGMA
                            key/cache_size-style credential or sandbox
                            escape attempts, and anything aiming OUTSIDE
                            the open database via load_extension /
                            fts3 tokenizers — never runs, even approved.

   `approval_level()` is the single entry point the agent uses: the level
   IS the answer. Multi-statement text follows its WORST statement, so a
   SELECT glued to a DELETE is approval-required, and a script glued to
   PRAGMA key is BLOCKED.

2. THE EXPOSURE GUARDRAIL — never expose database credentials. The agent
   never prints DSNs, file paths with credentials, or connection strings;
   `credential_guard()` scans text for credential shapes and the results
   formatter drops password-keyed columns ([REDACTED]) instead of values.
   is_exposure_request() declines asks that try to get the agent to print
   its own connection material.

3. RESULT LEAKAGE — reads stay readable but secrets inside DATA (a
   users.password column, an api_key) are redacted at format time:
   password/passwd/secret/token/api_key columns print [REDACTED].
"""
from __future__ import annotations

import re
from typing import Any

from orchestrator.contracts import TaskStatus

# ── permission levels ────────────────────────────────────────────────────────

READ_ONLY = "read_only"
APPROVAL_REQUIRED = "approval_required"
BLOCKED = "blocked"


def is_read_only(level: str) -> bool:
    return level == READ_ONLY


def needs_approval(level: str) -> bool:
    return level == APPROVAL_REQUIRED


def is_blocked(level: str) -> bool:
    return level == BLOCKED


# ── statement splitting ──────────────────────────────────────────────────────

def strip_comments_and_strings(sql: str) -> tuple[str, list[str]]:
    """Split SQL into (skeleton, literals): skeleton has comments and quoted
    literals replaced by spaces ('' → '') — keywords scan the skeleton only,
    so `WHERE name = 'DROP TABLE'` cannot fire the destructive patterns.
    The literals come back separately for literal-affecting checks."""
    skeleton = []
    literals: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", '"', "`"):
            quote = c
            j = i + 1
            buf = []
            while j < n:
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:   # doubled quote
                        buf.append(quote)
                        j += 2
                        continue
                    break
                buf.append(sql[j])
                j += 1
            literals.append("".join(buf))
            skeleton.append(" ")
            i = j + 1
        elif c == "-" and sql[i:i + 2] == "--":
            j = sql.find("\n", i)
            j = n if j == -1 else j
            skeleton.append(" ")
            i = j
        elif c == "/" and sql[i:i + 2] == "/*":
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            skeleton.append(" ")
            i = j
        elif c == "[":
            j = sql.find("]", i + 1)
            j = n if j == -1 else j + 1
            skeleton.append(sql[i:j])          # bracketed identifier: keep
            i = j
        else:
            skeleton.append(c)
            i += 1
    return "".join(skeleton), literals


def statements(sql: str) -> list[str]:
    """Split a script into statements at top-level semicolons.
    The skeleton (comments/strings blanked) only LOCATES the splits —
    the returned chunks are cut from the ORIGINAL text so each
    statement keeps its literals for the blocked-literal scan."""
    skel, _ = strip_comments_and_strings(sql or "")
    cuts: list[int] = []
    depth = 0
    for i, ch in enumerate(skel):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == ";" and depth == 0:
            cuts.append(i)
    pieces: list[str] = []
    prev = 0
    for c in cuts + [len(str(sql or ""))]:
        piece = str(sql or "")[prev:c].strip()
        prev = c + 1
        if piece:
            pieces.append(piece.strip())
    return pieces or [skel.strip()]


# ── statement classification (the level IS the answer) ──────────────────────

_READ_KEYWORD = re.compile(r"^\s*(?:WITH\s|SELECT\b|EXPLAIN\b|VALUES\b)",
                           re.IGNORECASE)

# introspection PRAGMAs only — anything else (key, cache_size,
# synchronous, writable_schema, secure_delete, auto_vacuum, user_version…
# and any database-scoped pragma `db.table_info`) needs approval.
_INTROSPECT_PRAGMA = re.compile(
    r"^\s*PRAGMA\s+(?:TABLE_INFO|TABLE_XINFO|INDEX_LIST|INDEX_INFO|"
    r"FOREIGN_KEY_(?:LIST|CHECK)|COLUMN_LIST|DATABASE_LIST|SCHEMA_VERSION|"
    r"DATA_VERSION|PAGE_COUNT|PAGE_SIZE|INTEGRITY_CHECK|QUICK_CHECK|"
    r"COMPILE_OPTIONS|FUNCTION_LIST|MODULE_LIST|TABLE_LIST)\b",
    re.IGNORECASE)
_ANY_PRAGMA = re.compile(r"^\s*PRAGMA\b", re.IGNORECASE)
_ANY_SELECT = re.compile(r"^SELECT\b", re.IGNORECASE)

# mutations — the whole mutating surface
_MUTATION_RE = re.compile(
    r"\b(?:INSERT|REPLACE|UPDATE|DELETE|MERGE|UPSERT|TRUNCATE|"
    r"DROP|CREATE|ALTER|RENAME|ATTACH|DETACH|VACUUM|REINDEX|"
    r"ANALYZE|GRANT|REVOKE|SAVEPOINT|RELEASE|ROLLBACK|COMMIT|BEGIN|END)\b",
    re.IGNORECASE)

# sandbox escapes — never run, even approved
_BLOCKED_RE = re.compile(
    r"\bLOAD\s+EXTENSION\b"
    + r"|\bLOAD_EXTENSION\s*\("
    + r"|\bATTACH\b[^;]{0,200}(?:://|ftp|http|memory:|temp:)"
    + r"|\bFTS\d*\b.{0,40}\bTOKENIZE\s*=\s*['\"]?(?:ICU|UNICODE61)"
    + r"|\bPRAGMA\s+(?:WRITABLE_SCHEMA|SECURE_DELETE)\b",
    re.IGNORECASE)

# any ATTACH at all is approval-required (rewireable virtual db);
# PRAGMA key/cache/attempts are credential material — also approval.
_APPROVAL_RE = re.compile(
    r"\bATTACH\b|\bDETACH\b|\bPRAGMA\s+(?:KEY|CACHE_SIZE|SYNCHRONOUS)\b",
    re.IGNORECASE)


def classify_statement(stmt: str) -> str:
    """One statement → its permission level."""
    skeleton, lits = strip_comments_and_strings(stmt or "")
    if not skeleton.strip():
        return READ_ONLY            # nothing executable
    if _BLOCKED_RE.search(skeleton):
        return BLOCKED
    # blocked material can also hide inside a literal (an ATTACH URL,
    # an extension path) — scan the literal VALUES too
    for lit in lits:
        if re.search(r"(?:://|ftp|http|load_extension|\\x00)",
                     lit, re.IGNORECASE):
            return BLOCKED
    head = skeleton.strip()
    if _READ_KEYWORD.match(head):
        return READ_ONLY
    if _INTROSPECT_PRAGMA.match(head):
        return READ_ONLY
    if _ANY_PRAGMA.match(head):
        # any non-introspection pragma touches engine state
        return APPROVAL_REQUIRED if not _BLOCKED_RE.search(head) else BLOCKED
    if _MUTATION_RE.search(head) or _APPROVAL_RE.match(head):
        # a mutation keyword anywhere in the statement body (subquery GLUE
        # like `SELECT 1 INTO…` aside, mutating verbs never appear inside a
        # pure select's skeleton by accident)
        return APPROVAL_REQUIRED
    if _ANY_SELECT.match(head):
        return READ_ONLY
    # unknown statement shape — assume the worst, ask the human
    return APPROVAL_REQUIRED


def statement_class_of(stmt: str) -> str:
    """A short human class for logging: select/pragma/explain/other."""
    skeleton, _ = strip_comments_and_strings(stmt or "")
    head = skeleton.strip().upper()
    if head.startswith("SELECT"):
        return "select"
    if head.startswith("WITH"):
        return "cte_select"
    if head.startswith("EXPLAIN"):
        return "explain"
    if head.startswith("PRAGMA"):
        return "pragma"
    if head.startswith("VALUES"):
        return "values"
    return "other"


def classify_sql(sql: str) -> str:
    """A whole script → its WORST statement's level. Read-only only when
    EVERY statement is read-only; one DELETE glues it to approval; one
    LOAD_EXTENSION blocks the whole thing."""
    levels = [classify_statement(s) for s in statements(sql or "")]
    if not levels:
        return READ_ONLY
    if any(lv == BLOCKED for lv in levels):
        return BLOCKED
    if all(lv == READ_ONLY for lv in levels):
        return READ_ONLY
    return APPROVAL_REQUIRED


def destructive_summary(sql: str) -> list[str]:
    """Which statements are non-read-only — for the approval message."""
    out: list[str] = []
    for stmt in statements(sql or ""):
        if classify_statement(stmt) != READ_ONLY:
            s = re.sub(r"\s+", " ", stmt).strip()
            out.append(s[:160] + ("…" if len(s) > 160 else ""))
    return out


# ── exposure + credential guardrail ─────────────────────────────────────────

EXPOSURE_REFUSAL = (
    "I don't expose connection details — no DSNs, no connection strings, "
    "no credentials. I report what a query returns, not how I'm wired to "
    "the database.")

DISCLAIMER_TEXT = (
    "Read-only unless approved: SELECT/EXPLAIN/introspection PRAGMA run "
    "without asking; anything that writes, deletes, drops or reconfigures "
    "first parks behind approval. Column values shaped like credentials "
    "are redacted in output.")

# column names whose VALUES are credential-shaped
_SECRET_COLUMN_RE = re.compile(
    r"\b(?:password|passwd|pwd|secret|token|api_?key|access_?token|"
    r"auth|credential|credentials|private_?key|session_?id|"
    r"card_?number|cvv|ssn)\b", re.IGNORECASE)

# credential-shaped VALUE material (connection strings etc.)
_CRED_VALUE_RE = re.compile(
    r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s]{8,}"
    + r"|(?:password|passwd|pwd|secret|token|api_?key|access_?key|"
      r"private_?key)\s*[:=]\s*\S{4,}",
    re.IGNORECASE)

_REDACTED = "[REDACTED]"


def redact_value(column: str, value) -> Any:
    """A cell goes [REDACTED] if its column name is credential-shaped, or
    if the value itself is a connection-string / secret-assignment shape."""
    if value is None:
        return value
    if _SECRET_COLUMN_RE.search(str(column or "")):
        return _REDACTED
    text = str(value)
    if _CRED_VALUE_RE.search(text):
        return _REDACTED
    return value


def redact_row(columns: list[str], row: list) -> list:
    return [redact_value(c, v) for c, v in zip(columns, row)]


def credential_guard(text: str) -> str:
    """Scrub credential-shaped material from any user-facing string."""
    if not text:
        return text
    return _CRED_VALUE_RE.sub(_REDACTED, str(text))


def is_exposure_request(text: str) -> bool:
    """Asks that try to get the agent to print its own wiring."""
    t = " ".join(str(text or "").lower().split())
    return bool(re.search(
        r"\b(?:connection\s+(?:string|details?)|dsn|show\s+(?:me\s+)?"
        r"(?:the\s+)?(?:password|credentials?|connection)|"
        r"what(?:'s|s|)\s+(?:the\s+)?(?:password|connection\s+string)|"
        r"database\s+password)\b", t))


# ── advice guardrail (trading-style decisions inside SQL? no) ────────────────

def is_advice_request(text: str) -> bool:
    """Data-analysis questions are fine; investment-advice-shaped asks
    (what should I buy/sell based on this table) are refused."""
    t = " ".join(str(text or "").lower().split())
    return bool(re.search(
        r"\b(?:should|shall)\s+(?:i|we)\s+(?:buy|sell|invest|hold)\b"
        + r"|\bwhat\s+(?:should|would)\s+you\s+(?:do|buy|sell)\b"
        + r"|\b(?:is\s+it\s+a\s+good\s+time\s+to\s+(?:buy|sell|invest))\b",
        t))


ADVICE_REFUSAL = (
    "I can't advise on that. I inspect, query, explain and validate SQL "
    "against your permitted schema — I don't make investment or business "
    "decisions. Ask me to find the data and I'll happily fetch it; ask a "
    "human advisor what to do with it.")


# ── approvals (the productivity envelope) ────────────────────────────────────

def approval_summary(agent: str, action: str, detail: str) -> str:
    return f"{agent}: {action} — {detail}"[:200]


def approval_result(request, agent: str, action: str, detail: str):
    """The standard NEEDS_APPROVAL envelope for a mutating query."""
    from orchestrator.contracts import AgentResult
    return AgentResult(
        task_id=request.task_id,
        status=TaskStatus.NEEDS_APPROVAL,
        text=(f"This needs your approval before anything runs:\n"
              f"  What:   {detail}\n"
              f"  Action: {action}\n"
              f"  Say 'approve' (or resolve the approval) to let it run — "
              f"the database has not been touched yet."),
        data={"approval_summary": approval_summary(agent, action, detail),
              "needs_approval": True, "action": action},
        agent=agent,
    )
