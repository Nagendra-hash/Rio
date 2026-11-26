"""
orchestrator/agents/sql/ — the Phase 17 SQL agent.

A specialist on the standard BaseAgent contract that reads PERMITTED
database schemas, turns natural language into SQL through a
deterministic template engine (with an optional validated LLM rung),
validates every statement against the snapshot and the guardrails,
executes read-only queries by default, formats results, parks EVERY
mutating statement behind approval, and never exposes credentials:

    schema.py       value shapes: TableInfo/ColumnInfo/Relation/
                    SchemaSnapshot/QueryPlan/QueryResult + validation
    safety.py       the destructive-SQL guardrail (permission levels in
                    code), exposure guardrail, credential redaction
    perms.py        the isolated store (memory/sql/): permitted-database
                    registry, connection bookkeeping, actions-only audit
    inspector.py    the schema reader: snapshot from a live connection
                    (mode=ro) or from plain rows for offline tests
    validator.py    SQL validation: safety level, identifier existence,
                    structure pragmas — the gate before any run
    nl2sql.py       NL→SQL: template strategies + direct lane + LLM rung
                    (validated identically), composite-ask splitting
    executor.py     execution with row/time caps + credential redaction,
                    bounded table formatting
    checkpoint.py   crash-safe approval checkpoints (productivity contract)
    sql_agent.py    the BaseAgent wiring the pipeline

Registered behind JARVIS_SQL_AGENT=on (shadow-mode convention).

Safety posture, all in code:
    * classify_sql decides read-only vs approval-required vs BLOCKED
      from the SQL itself — never from what the user said;
    * DROP/DELETE/TRUNCATE/destructive ALTER and friends never run
      without an explicit approval resume; LOAD_EXTENSION-style escapes
      never run at all;
    * permitted database files/DSNs live only in memory/sql/ (0600/0700,
      redact_obj before every disk write); agent replies carry db0/db1
      internal names, never paths or credentials;
    * credential-shaped cells ([password], tokens, API keys) print
      [REDACTED] in results.
"""
