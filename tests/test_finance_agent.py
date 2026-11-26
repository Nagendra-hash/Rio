"""
tests/test_finance_agent.py — offline acceptance tests for the Phase 14
Personal Finance agent.

NO network, NO LLM, NO bank connections: JARVIS_HOME points at a fresh
temp dir, stores/checkpoints are built on explicit temp directories.

What this locks in:
  1. SAFETY    — advice requests are DECLINED in code; permission levels
                 (writes need approval, payments/credentials BLOCKED);
                 token material never survives a reply.
  2. SCHEMA    — validation, date normalization, currency handling,
                 dedupe keys, amount sanity.
  3. PARSER    — receipts (vendor/date/total/items/qty/currency,
                 confidence) and statement lines; garbage is reported,
                 never guessed.
  4. ENGINE    — categorization (user categories first), monthly totals,
                 budget status with pace, recurring detection, overspend
                 callouts, summaries. monthly_total never renders as a
                 category.
  5. STORE     — the vault: 0600 files in a 0700 dir, redaction on write,
                 ACTIONS-ONLY audit (no amounts, no vendors), dedupe,
                 update/delete, wipe.
  6. CHECKPOINT— snapshots, prune, perms, redaction, never raises.
  7. AGENT     — parse preview (no write), import PARKS (nothing saved
                 until approval), resume commits the EXACT payload,
                 duplicates skipped at commit, add/edit/delete/recategorize
                 all gate, budget_set immediate, user categories, garbage
                 never raises.
  8. WIRING    — classifier lane, explicit routes, registry gates.

Run:  .venv/bin/python -m pytest tests/test_finance_agent.py -q
"""
import itertools
import json
import os
import stat
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ── isolation: this module gets a fresh JARVIS_HOME ──────────────────────────
_TMP = tempfile.mkdtemp(prefix="jarvis-finance-")
os.environ["JARVIS_HOME"] = _TMP

from orchestrator.agents import build_default_registry                 # noqa: E402
from orchestrator.agents.finance import engine, parser, safety, schema  # noqa: E402
from orchestrator.agents.finance.checkpoint import FinanceCheckpoint    # noqa: E402
from orchestrator.agents.finance.finance_agent import FinanceAgent      # noqa: E402
from orchestrator.agents.finance.store import FinanceStore              # noqa: E402
from orchestrator.agents.productivity import security as prod_security  # noqa: E402
from orchestrator.classifier import IntentClassifier                    # noqa: E402
from orchestrator.contracts import AgentRequest, TaskStatus, TaskType   # noqa: E402

_SEQ = itertools.count(1)


def _req(text: str = "", **meta) -> AgentRequest:
    return AgentRequest(task_id=f"finance-test-{next(_SEQ)}",
                        task_type=TaskType.FINANCE, text=text, metadata=meta)


@pytest.fixture()
def tmp():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture()
def store(tmp):
    return FinanceStore(tmp / "vault")


@pytest.fixture()
def ckpt(tmp):
    return FinanceCheckpoint(tmp / "ckpt")


@pytest.fixture()
def agent(tmp, ckpt):
    return FinanceAgent(store=FinanceStore(tmp / "vault"),
                        checkpoint=ckpt)


RECEIPT = """BIG BAZAR
2026-10-07
2 x Basmati Rice 5kg  900.00
Milk 1L  58.00
GST 95.80
TOTAL 1053.80
Thank you, visit again!"""


# ════════════════════════════════════════════════ 1. safety ═════════════════

class TestSafety:
    def test_advice_requests_detected(self):
        for text in ("should I invest in mutual funds?",
                     "buy some stocks for me",
                     "what crypto should I buy",
                     "make a retirement plan for me",
                     "is this a good portfolio"):
            assert safety.is_advice_request(text), text

    def test_data_asks_are_not_advice(self):
        for text in ("how much did I spend this month",
                     "show my budget status",
                     "any recurring subscriptions",
                     "parse this receipt",
                     "summarize my expenses"):
            assert not safety.is_advice_request(text), text

    def test_permission_levels(self):
        for a in ("summary", "parse", "budget_status", "recurring"):
            assert safety.permission_for(a) == safety.READ_ONLY, a
        for a in ("budget_set", "category_add"):
            assert safety.permission_for(a) == safety.SAFE_ACTION, a
        for a in ("import", "add", "edit", "delete", "recategorize"):
            assert safety.permission_for(a) == safety.APPROVAL_REQUIRED, a
        for a in ("pay", "transfer", "connect_bank", "read_credentials",
                  "disable_approvals"):
            assert safety.permission_for(a) == safety.BLOCKED, a
            assert safety.is_blocked(safety.permission_for(a))

    def test_blocked_is_not_approval(self):
        # BLOCKED must never be unlockable by an approval.
        assert not safety.needs_approval(safety.BLOCKED)
        assert safety.is_blocked(safety.BLOCKED)

    def test_redaction_reused_from_productivity(self):
        out = safety.redact("receipt note: token ya29.SHOULD_NEVER_APPEAR")
        assert "ya29." not in out and "[REDACTED]" in out
        assert safety.redact is prod_security.redact


# ════════════════════════════════════════════════ 2. schema ═════════════════

class TestSchema:
    def test_validate_clean_row(self):
        clean, errors = schema.validate_expense(
            {"date": "2026-10-07", "vendor": "Zomato", "amount": "450.50",
             "currency": "₹"})
        assert errors == [] and clean is not None
        assert clean["amount"] == 450.5 and clean["currency"] == "INR"
        assert clean["id"].startswith("exp-") and clean["category"]

    def test_rejects_bad_rows(self):
        for bad in ({}, {"date": "2026-10-07"},
                    {"date": "2026-10-07", "vendor": "X", "amount": -5},
                    {"date": "2026-10-07", "vendor": "X", "amount": "abc"},
                    {"vendor": "X", "amount": 10},
                    {"date": "2026-10-07", "vendor": "X", "amount": 10,
                     "currency": "BTC"}):
            clean, errors = schema.validate_expense(bad)
            assert clean is None and errors, bad

    def test_amount_must_be_positive(self):
        clean, errors = schema.validate_expense(
            {"date": "2026-10-07", "vendor": "X", "amount": 0})
        assert clean is None and any("positive" in e for e in errors)

    def test_date_normalization(self):
        cases = {"2026-10-07": "2026-10-07", "07/10/2026": "2026-10-07",
                 "07-10-26": "2026-10-07", "7 Oct 2026": "2026-10-07",
                 "Oct 7, 2026": "2026-10-07", "07/10": None}
        for raw, want in cases.items():
            got = schema.normalize_date(raw, today=date(2026, 10, 7))
            if want is None:
                assert got == "2026-10-07", (raw, got)
            else:
                assert got == want, (raw, got)
        assert schema.normalize_date("not a date") == ""

    def test_currency_normalization(self):
        assert schema.normalize_currency("₹") == "INR"
        assert schema.normalize_currency("Rs.") == "INR"
        assert schema.normalize_currency("$") == "USD"
        assert schema.normalize_currency("usd") == "USD"
        assert schema.normalize_currency("") == ""

    def test_dedupe_key_shape(self):
        a = {"date": "2026-10-07", "vendor": "Zomato", "amount": 450.0,
             "currency": "INR"}
        b = {"date": "2026-10-07", "vendor": "zomato", "amount": 450.004,
             "currency": "INR"}
        c = {"date": "2026-10-08", "vendor": "Zomato", "amount": 450.0,
             "currency": "INR"}
        assert schema.dedupe_key(a) == schema.dedupe_key(b)
        assert schema.dedupe_key(a) != schema.dedupe_key(c)

    def test_items_clamped(self):
        clean, _ = schema.validate_expense(
            {"date": "2026-10-07", "vendor": "X", "amount": 10,
             "items": [{"name": "tea", "qty": 2, "price": 5},
                       "garbage", {"name": "", "price": 3}]})
        assert clean["items"] == [{"name": "tea", "qty": 2, "price": 5}]


# ════════════════════════════════════════════════ 3. parser ═════════════════

class TestParser:
    def test_receipt_full(self):
        r = parser.parse_receipt(RECEIPT)
        assert r["ok"] is True and r["confidence"] == 1.0
        e = r["expense"]
        assert e["vendor"] == "BIG BAZAR"
        assert e["date"] == "2026-10-07"
        assert e["amount"] == 1053.80 and e["currency"] == "INR"
        assert e["source"] == "receipt"
        names = [i["name"] for i in e["items"]]
        assert any("Basmati Rice" in n for n in names)
        assert any("Milk" in n for n in names)
        rice = next(i for i in e["items"] if "Basmati" in i["name"])
        assert rice["qty"] == 2.0 and rice["price"] == 900.0

    def test_instruction_leadins_are_not_the_vendor(self):
        r = parser.parse_receipt("parse this receipt:\n" + RECEIPT)
        assert r["expense"]["vendor"] == "BIG BAZAR"

    def test_total_beats_largest_amount(self):
        text = "SHOP\n2026-10-07\nBig item 5000.00\nTOTAL 250.00"
        r = parser.parse_receipt(text)
        assert r["expense"]["amount"] == 250.0

    def test_fallback_largest_amount_without_total_line(self):
        text = "CAFE MUG\n2026-10-07\nCoffee 180.00\nSandwich 240.00"
        r = parser.parse_receipt(text)
        assert r["expense"]["amount"] == 240.0
        assert "total amount not found" not in r["errors"]

    def test_missing_fields_reported_not_guessed(self):
        r = parser.parse_receipt("TOTAL 100.00")
        assert r["expense"]["amount"] == 100.0
        assert "date not found" in r["errors"]
        assert "vendor not found" in r["errors"]
        assert r["confidence"] < 1.0

    def test_garbage_is_honest(self):
        r = parser.parse_receipt("")
        assert r["ok"] is False and r["expense"] is None
        r2 = parser.parse_receipt("hello there how are you")
        assert r2["expense"] is None or r2["ok"] is False

    def test_statement_lines(self):
        text = ("12/10/2026 | ZOMATO | -450.00\n"
                "13 Oct 2026 | Uber | 320.00\n"
                "no date here 99.00\n"
                "14/10/2026 | no amount line")
        r = parser.parse_statement(text)
        assert len(r["expenses"]) == 2
        assert r["expenses"][0]["vendor"].lower().startswith("zomato")
        assert r["expenses"][0]["amount"] == 450.0
        assert r["skipped_count"] == 2

    def test_statement_currency_detection(self):
        r = parser.parse_statement("2026-10-07 | Amazon | $25.99")
        assert r["expenses"][0]["currency"] == "USD"


# ════════════════════════════════════════════════ 4. engine ═════════════════

def _exp(d, vendor, amount, category="other", **kw):
    return {"date": d, "vendor": vendor, "amount": amount,
            "currency": kw.pop("currency", "INR"),
            "category": category, "note": kw.pop("note", ""), **kw}


class TestEngine:
    def test_categorize_builtins(self):
        assert engine.categorize(_exp("2026-10-01", "Zomato", 400))[0] \
            == "food"
        assert engine.categorize(_exp("2026-10-01", "Uber", 300))[0] \
            == "transport"
        assert engine.categorize(_exp("2026-10-01", "Netflix", 499))[0] \
            == "entertainment"
        assert engine.categorize(_exp("2026-10-01", "Blimp & Sons", 10))[0] \
            == "other"
        # word-boundary matching: 'shop' never fires inside 'workshop'
        assert engine.categorize(_exp("2026-10-01", "Workshop tools", 10))[0] \
            == "other"

    def test_user_categories_win(self):
        user = [{"name": "pets", "keywords": ["vet", "petco"]}]
        cat, by = engine.categorize(_exp("2026-10-01", "Vet Clinic", 800),
                                    user)
        assert cat == "pets" and by == "keyword"
        # a user category also overrides a builtin keyword match
        cat2, _ = engine.categorize(_exp("2026-10-01", "Zomato", 400),
                                    [{"name": "eating out",
                                      "keywords": ["zomato"]}])
        assert cat2 == "eating out"

    def test_monthly_totals(self):
        rows = [_exp("2026-10-01", "Zomato", 400, "food"),
                _exp("2026-10-02", "Uber", 300, "transport"),
                _exp("2026-10-03", "Zomato", 100, "food"),
                _exp("2026-09-30", "Zomato", 999, "food")]
        t = engine.monthly_totals(rows, "2026-10")
        assert t["total"] == 800.0 and t["count"] == 3
        assert t["totals"]["food"] == 500.0

    def test_budget_status_labels_and_pace(self):
        rows = [_exp("2026-10-02", "Zomato", 2500, "food"),
                _exp("2026-10-02", "Uber", 500, "transport")]
        status = engine.budget_status(rows, {"food": 2000, "transport": 500,
                                             "monthly_total": 5000},
                                      "2026-10", today=date(2026, 10, 7))
        by_cat = {c["category"]: c for c in status["categories"]}
        assert by_cat["food"]["label"] == "over"
        assert by_cat["food"]["pct"] == 125.0
        assert by_cat["transport"]["label"] in ("ahead", "over", "ok")
        assert "monthly_total" not in by_cat      # overall, not a category
        assert status["overall"]["label"] == "ok"

    def test_budget_status_pace_flag(self):
        rows = [_exp("2026-10-02", "Zomato", 900, "food")]
        status = engine.budget_status(rows, {"food": 2000}, "2026-10",
                                      today=date(2026, 10, 3))
        food = status["categories"][0]
        assert food["label"] == "ahead" and food["pace"] > 1.1

    def test_overspend_report_callouts(self):
        rows = [_exp("2026-10-02", "Zomato", 2500, "food")]
        status = engine.budget_status(rows, {"food": 2000, "monthly_total": 2000},
                                      "2026-10", today=date(2026, 10, 7))
        callouts = engine.overspend_report(status)
        assert any("food" in c and "over" in c for c in callouts)
        assert any("Overall budget exceeded" in c for c in callouts)

    def test_recurring_detection(self):
        rows = [_exp(f"2026-0{m}-01", "Netflix", 499, "entertainment")
                for m in (7, 8, 9)]
        rows += [_exp("2026-09-03", "Random Cafe", 250, "food")]
        found = engine.find_recurring(rows, today=date(2026, 9, 15))
        assert len(found) == 1
        r = found[0]
        assert r["vendor"] == "Netflix" and r["occurrences"] == 3
        assert r["median_amount"] == 499.0
        assert r["next_due_is_estimate"] is True

    def test_recurring_requires_stable_amounts(self):
        rows = [_exp(f"2026-0{m}-01", "Big Store", 100 * m)
                for m in (7, 8, 9)]
        assert engine.find_recurring(rows) == []

    def test_monthly_summary_never_recommends(self):
        rows = [_exp("2026-10-02", "Zomato", 2500, "food"),
                _exp("2026-09-01", "Netflix", 499, "entertainment"),
                _exp("2026-08-01", "Netflix", 499, "entertainment"),
                _exp("2026-10-01", "Netflix", 499, "entertainment")]
        text = engine.monthly_summary(rows, {"food": 2000}, "2026-10",
                                      today=date(2026, 10, 7))
        assert "Finance summary" in text and "food" in text
        assert "Recurring" in text
        assert "no investment or financial recommendations" in text


# ═════════════════════════════════════════════════ 5. store ════════════════

class TestStore:
    def test_permissions_owner_only(self, store):
        store.add_expenses([_exp("2026-10-07", "Zomato", 400)])
        store.set_budgets({"food": 2000})
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        for f in store.root.iterdir():
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, f.name

    def test_add_validates_and_dedupes(self, store):
        first = store.add_expenses([
            _exp("2026-10-07", "Zomato", 400),
            _exp("2026-10-07", "Zomato", 400),      # same purchase
            {"vendor": "no date", "amount": 5}])     # invalid
        assert first["added"] == 1
        assert first["skipped_duplicates"] == 1
        assert first["invalid"] == 1
        second = store.add_expenses([_exp("2026-10-07", "Zomato", 400)])
        assert second["added"] == 0
        assert second["skipped_duplicates"] == 1

    def test_store_categorizes_uncategorized(self, store):
        store.add_expenses([_exp("2026-10-07", "Uber", 300, "")])
        row = store.expenses()[0]
        assert row["category"] == "transport"

    def test_expenses_filtering_and_get(self, store):
        store.add_expenses([_exp("2026-10-07", "Zomato", 400),
                            _exp("2026-09-07", "Uber", 300)])
        assert len(store.expenses(month="2026-10")) == 1
        assert len(store.expenses(category="transport")) == 1
        rid = store.expenses()[0]["id"]
        assert store.get_expense(rid)["id"] == rid
        assert store.get_expense("exp-nope") is None

    def test_update_and_delete(self, store):
        store.add_expenses([_exp("2026-10-07", "Mystery Shop", 400)])
        rid = store.expenses()[0]["id"]
        hit = store.update_expense(rid, {"category": "shopping",
                                         "note": "gift"})
        assert hit["category"] == "shopping" and hit["note"] == "gift"
        assert store.update_expense(rid, {"amount": 1}) is None   # not allowed
        assert store.delete_expense(rid)["id"] == rid
        assert store.expenses() == []
        assert store.delete_expense(rid) is None

    def test_budgets_and_categories(self, store):
        store.set_budgets({"food": "2000", "bad": "x"})
        assert store.budgets() == {"food": 2000.0}
        cats = store.set_user_categories(
            [{"name": "Pets", "keywords": ["Vet", "petco"]},
             {"name": ""}])
        assert cats == [{"name": "pets", "keywords": ["vet", "petco"]}]

    def test_audit_records_actions_only(self, store):
        store.add_expenses([_exp("2026-10-07", "Secret Vendor Xyz", 4242.42)])
        audit = (store.root / "audit.jsonl").read_text()
        assert "expenses_added" in audit
        assert "Secret Vendor Xyz" not in audit
        assert "4242.42" not in audit

    def test_redaction_on_write(self, store):
        store.set_budgets({"note": 1})     # numeric only — control case
        store._write_json("probe", {"access_token": "ya29.SHOULD_NEVER"})
        raw = (store.root / "probe.json").read_text()
        assert "ya29." not in raw and "[REDACTED]" in raw

    def test_wipe_keeps_audit_event(self, store):
        store.add_expenses([_exp("2026-10-07", "Zomato", 400)])
        store.set_budgets({"food": 1000})
        assert store.wipe()
        assert store.summary()["expenses"] == 0
        assert store.budgets() == {}
        assert "wipe" in (store.root / "audit.jsonl").read_text()


# ═════════════════════════════════════════════════ 6. checkpoint ═══════════

class TestCheckpoint:
    def test_save_load_prune_and_perms(self, tmp):
        cp = FinanceCheckpoint(tmp / "ck")
        for i in range(25):
            assert cp.save({"i": i})
        assert len(cp.list_ids()) == 20
        assert cp.load_latest()["i"] == 24
        assert stat.S_IMODE(
            (tmp / "ck" / "latest.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp / "ck").stat().st_mode) == 0o700

    def test_parked_payload_roundtrip_and_redaction(self, tmp):
        cp = FinanceCheckpoint(tmp / "ck")
        payload = {"task_id": "t1", "action": "import_expenses",
                   "payload": {"rows": [{"amount": 10,
                                          "note": "ya29.SECRET123"}]},
                   "status": "awaiting_approval"}
        assert cp.save_for("t1", payload)
        loaded = cp.load_for("t1")
        assert loaded["action"] == "import_expenses"
        raw = (tmp / "ck" / "approval-t1.json").read_text()
        assert "ya29." not in raw

    def test_never_raises_on_broken_dir(self, tmp):
        cp = FinanceCheckpoint(tmp / "ck")
        blocked = tmp / "blocked"
        blocked.write_text("not a dir")
        cp.dir = blocked
        assert cp.save({"x": 1}) is None
        assert cp.load_latest() is None
        assert cp.list_ids() == []


# ════════════════════════════════════════ 7. agent flows ════════════════════

class TestFinanceAgent:
    def test_parse_previews_without_writing(self, agent, tmp):
        r = agent.handle(_req(f"parse this receipt:\n{RECEIPT}"))
        assert r.status is TaskStatus.COMPLETED
        assert "NOT imported yet" in r.text
        assert "BIG BAZAR" in r.text and "1053.80" in r.text
        vault = tmp / "vault"
        assert not vault.exists() or not (vault / "expenses.jsonl").exists()

    def test_import_parks_and_writes_nothing(self, agent, tmp):
        r = agent.handle(_req(f"import this receipt:\n{RECEIPT}"))
        assert r.status is TaskStatus.NEEDS_APPROVAL
        assert "approval_summary" in r.data
        assert (tmp / "vault" / "expenses.jsonl").exists() is False

    def test_import_resume_commits_exact_payload(self, agent, tmp):
        park = agent.handle(_req(f"import this receipt:\n{RECEIPT}"))
        r = agent.handle(_req("x", approved=True, resume=park.task_id,
                              by="user"))
        assert r.status is TaskStatus.COMPLETED
        rows = FinanceStore(tmp / "vault").expenses()
        assert len(rows) == 1
        row = rows[0]
        assert row["vendor"] == "BIG BAZAR" and row["amount"] == 1053.80
        assert row["date"] == "2026-10-07"
        assert row["items"] and row["items"][0]["qty"] == 2.0

    def test_import_categories_before_approval(self, agent):
        r = agent.handle(_req(f"import receipt:\n{RECEIPT}"))
        assert r.status is TaskStatus.NEEDS_APPROVAL
        assert "(category: other)" in r.text

    def test_duplicate_import_skipped_at_commit(self, agent):
        park1 = agent.handle(_req(f"import receipt:\n{RECEIPT}"))
        agent.handle(_req("x", approved=True, resume=park1.task_id))
        park2 = agent.handle(_req(f"import receipt again:\n{RECEIPT}"))
        r = agent.handle(_req("x", approved=True, resume=park2.task_id))
        assert "0 record(s) added" in r.text
        assert "1 duplicate(s) skipped" in r.text

    def test_import_unreadable_asks_for_input(self, agent):
        r = agent.handle(_req("import this: hello there friend"))
        assert r.status is TaskStatus.NEEDS_INPUT
        r2 = agent.handle(_req("import receipt:\nTOTAL 100.00"))
        # total but no date/vendor → validation fails → nothing importable
        assert "Nothing importable" in r2.text

    def test_add_gates_and_respects_user_categories(self, agent):
        agent.handle(_req("", finance_action="category_add",
                          category_name="pets", keywords=["vet"]))
        park = agent.handle(_req("", finance_action="add",
                                 date="2026-10-07", vendor="Vet Clinic",
                                 amount=800, currency="INR"))
        assert park.status is TaskStatus.NEEDS_APPROVAL
        assert "(category: pets)" in park.text
        r = agent.handle(_req("x", approved=True, resume=park.task_id))
        assert r.status is TaskStatus.COMPLETED

    def test_add_needs_valid_fields(self, agent):
        r = agent.handle(_req("", finance_action="add", vendor="X",
                              amount=-5))
        assert r.status is TaskStatus.NEEDS_INPUT
        assert "positive" in r.text

    def test_edit_and_delete_gate(self, agent, tmp):
        park = agent.handle(_req("", finance_action="add",
                                 date="2026-10-07", vendor="Cafe",
                                 amount=100))
        agent.handle(_req("x", approved=True, resume=park.task_id))
        rid = FinanceStore(tmp / "vault").expenses()[0]["id"]
        edit = agent.handle(_req("", finance_action="edit",
                                 expense_id=rid, note="team lunch"))
        assert edit.status is TaskStatus.NEEDS_APPROVAL
        r = agent.handle(_req("x", approved=True, resume=edit.task_id))
        assert "team lunch" in r.text
        dele = agent.handle(_req("", finance_action="delete",
                                 expense_id=rid))
        assert dele.status is TaskStatus.NEEDS_APPROVAL
        r2 = agent.handle(_req("x", approved=True, resume=dele.task_id))
        assert "Deleted" in r2.text
        assert FinanceStore(tmp / "vault").expenses() == []

    def test_recategorize_gates(self, agent, tmp):
        park = agent.handle(_req("", finance_action="add",
                                 date="2026-10-07", vendor="Mystery",
                                 amount=50))
        agent.handle(_req("x", approved=True, resume=park.task_id))
        rid = FinanceStore(tmp / "vault").expenses()[0]["id"]
        recat = agent.handle(_req("", finance_action="recategorize",
                                  expense_id=rid, category="shopping"))
        assert recat.status is TaskStatus.NEEDS_APPROVAL
        r = agent.handle(_req("x", approved=True, resume=recat.task_id))
        assert "shopping" in r.text
        assert FinanceStore(tmp / "vault").expenses()[0]["category"] \
            == "shopping"

    def test_resume_rejects_stale_or_unknown(self, agent):
        r = agent.handle(_req("x", approved=True, resume="finance-nope"))
        assert r.status is TaskStatus.FAILED
        assert "nothing was changed" in r.text

    def test_double_resume_is_declined(self, agent):
        park = agent.handle(_req(f"import receipt:\n{RECEIPT}"))
        agent.handle(_req("x", approved=True, resume=park.task_id))
        r2 = agent.handle(_req("x", approved=True, resume=park.task_id))
        assert r2.status is TaskStatus.DECLINED

    def test_budget_set_is_immediate_config(self, agent):
        r = agent.handle(_req("", finance_action="budget_set",
                              budgets={"food": 8000, "monthly_total": 25000}))
        assert r.status is TaskStatus.COMPLETED
        assert "food: 8000.00" in r.text
        r2 = agent.handle(_req("budget status"))
        assert "monthly_total" not in r2.text      # never a category row
        assert "8000.00" in r2.text

    def test_summary_reports_and_flags(self, agent):
        for d, v, amt, cat in (("2026-10-02", "Zomato", 2500, "food"),
                               ("2026-10-03", "Uber", 900, "transport")):
            p = agent.handle(_req("", finance_action="add", date=d,
                                  vendor=v, amount=amt))
            agent.handle(_req("x", approved=True, resume=p.task_id))
        agent.handle(_req("", finance_action="budget_set",
                          budgets={"food": 2000, "transport": 500}))
        r = agent.handle(_req("summarize my spending"))
        assert "Finance summary" in r.text
        assert "'transport' is over budget" in r.text
        assert "no investment or financial recommendations" in r.text

    def test_recurring_flow(self, agent):
        for m in ("08", "09", "10"):
            p = agent.handle(_req("", finance_action="add",
                                  date=f"2026-{m}-01", vendor="Netflix",
                                  amount=499))
            agent.handle(_req("x", approved=True, resume=p.task_id))
        r = agent.handle(_req("any recurring subscriptions?"))
        assert "Netflix" in r.text and "estimate" in r.text

    def test_advice_declined(self, agent):
        for text in ("should I invest in stocks",
                     "plan my retirement portfolio",
                     "which crypto to buy"):
            r = agent.handle(_req(text))
            assert r.status is TaskStatus.DECLINED, text
            assert "don't make investment decisions" in r.text

    def test_user_category_config(self, agent):
        r = agent.handle(_req("", finance_action="category_add",
                              category_name="pet care",
                              keywords=["vet", "pet shop"]))
        assert "pet care" in r.text
        r2 = agent.handle(_req("show my categories"))
        assert "pet care" in r2.text and "vet" in r2.text
        r3 = agent.handle(_req("", finance_action="category_remove",
                               category_name="pet care"))
        assert "removed" in r3.text

    def test_no_token_material_in_any_reply(self, agent):
        leak = f"import receipt:\nPAYPAL\n2026-10-07\ntoken ya29.LEAK_ME\nTOTAL 40.00"
        for req in (_req(leak), _req("budget status"),
                    _req("", finance_action="expenses")):
            r = agent.handle(req)
            assert "ya29." not in r.text
            assert "LEAK_ME" not in r.text

    def test_menu_and_never_raises(self, agent):
        r = agent.handle(_req("what's the weather"))
        assert r.status is TaskStatus.COMPLETED     # falls to summary
        for text in ("", "   ", "!!!", "x" * 5000, "import",
                     "delete expense", "budget"):
            assert agent.handle(_req(text)) is not None


# ═════════════════════════════════════════════════ 8. wiring ═══════════════

class TestWiring:
    def test_classifier_finance_lane(self):
        c = IntentClassifier()
        for text in ("track my spending", "show my budget status",
                     "any recurring subscriptions?",
                     "make an expense report",
                     "how much did I spend this month"):
            got = c.classify(text)
            assert got.task_type is TaskType.FINANCE, (text, got.reason)

    def test_explicit_routes(self):
        c = IntentClassifier()
        assert c.classify("route this to finance") \
            .task_type is TaskType.FINANCE
        assert c.classify("[finance] parse this receipt") \
            .task_type is TaskType.FINANCE
        assert c.classify("finance agent summarize my expenses") \
            .task_type is TaskType.FINANCE

    def test_other_lanes_unchanged(self):
        c = IntentClassifier()
        assert c.classify("fix the bug in my python function") \
            .task_type is TaskType.CODING
        assert c.classify("analyze RELIANCE chart setup") \
            .task_type is TaskType.TRADING
        assert c.classify("what's the latest news about EVs") \
            .task_type is TaskType.RESEARCH
        assert c.classify("I have fever and headache") \
            .task_type is TaskType.HEALTH
        assert c.classify("plan my weekly menu") \
            .task_type is TaskType.MEAL
        assert c.classify("find flights from Delhi to Goa") \
            .task_type is TaskType.TRAVEL
        assert c.classify("hi").task_type is TaskType._FALLBACK

    def test_registry_off_by_default(self):
        os.environ.pop("JARVIS_FINANCE_AGENT", None)
        reg = build_default_registry()
        assert type(reg.get("finance_agent")).__name__ == "StubAgent"
        r = reg.get("finance_agent").handle(_req("summary"))
        assert r.status is TaskStatus.NEEDS_INPUT

    def test_registry_on(self):
        os.environ["JARVIS_FINANCE_AGENT"] = "on"
        try:
            reg = build_default_registry()
            assert type(reg.get("finance_agent")).__name__ == "FinanceAgent"
        finally:
            os.environ.pop("JARVIS_FINANCE_AGENT", None)
