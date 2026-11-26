"""
orchestrator/agents/finance/finance_agent.py — the Personal Finance Agent
(Phase 14).

Capabilities: receipt parsing (preview first), expense categorization
(user-defined categories first), monthly budget tracking, recurring
expense detection, overspending detection, summaries.

THE THREE HARD RULES (enforced in code, see safety.py):
  * no investment decisions or financial recommendations — advice-shaped
    asks are DECLINED with a fixed refusal;
  * financial data lives ONLY in the finance vault (memory/finance/),
    redacted before any reply or disk write, never fed to the RAG layer;
  * EVERY write to a financial record (import / add / edit / delete /
    recategorize) parks the EXACT payload behind NEEDS_APPROVAL and
    executes only that payload on resume — nothing is ever re-parsed
    from the user's words at execution time.
"""
from __future__ import annotations

import logging
from datetime import date

from orchestrator.agents.finance import engine, parser, schema, safety
from orchestrator.agents.finance.checkpoint import FinanceCheckpoint
from orchestrator.agents.finance.store import FinanceStore
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)

log = logging.getLogger("jarvis.finance")


class FinanceAgent(BaseAgent):
    name = "finance_agent"
    description = ("Personal finance specialist: parses pasted receipts, "
                   "categorizes expenses (your own categories first), "
                   "tracks monthly budgets, finds recurring payments, "
                   "flags overspending, and produces summaries. Records "
                   "live in a separate vault; every import or edit needs "
                   "your approval; never gives investment advice.")
    task_types = (TaskType.FINANCE,)

    def __init__(self, store: FinanceStore | None = None,
                 checkpoint: FinanceCheckpoint | None = None) -> None:
        self.store = store or FinanceStore()
        self.checkpoint = checkpoint or FinanceCheckpoint()

    # ── entry point ───────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            meta = request.metadata or {}
            if meta.get("approved") and meta.get("resume"):
                return self._resume_approved(request, str(meta["resume"]),
                                             str(meta.get("by") or "user"))
            if safety.is_advice_request(request.text):
                return self._decline_advice(request)
            action = str(meta.get("finance_action") or "").strip().lower() \
                or self._intent(request.text)
            handler = self._HANDLERS.get(action, FinanceAgent._summary)
            return handler(self, request)
        except Exception as exc:                     # never raise outward
            log.warning("finance agent error: %s", type(exc).__name__)
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               error="finance action failed",
                               agent=self.name)

    @staticmethod
    def _intent(text: str) -> str:
        t = (text or "").lower()
        if "parse" in t and ("receipt" in t or "bill" in t or "invoice" in t):
            return "parse"
        if "import" in t:
            return "import"
        if ("delete" in t or "remove" in t) and "expense" in t:
            return "delete"
        if "recategor" in t or ("change" in t and "categor" in t):
            return "recategorize"
        if ("edit" in t or "fix" in t) and "expense" in t:
            return "edit"
        if ("add" in t or "log" in t or "record" in t) and \
                ("expense" in t or "spend" in t or "purchase" in t):
            return "add"
        if "recurring" in t or "subscription" in t:
            return "recurring"
        if "categor" in t:
            return "categories"
        if ("budget" in t or "how much" in t or "spent" in t or
                "overspend" in t or "status" in t):
            return "budget"
        if "expenses" in t and ("list" in t or "show" in t or "all" in t):
            return "expenses"
        return "summary"

    # ── result helpers ────────────────────────────────────────────────────
    def _reply(self, request: AgentRequest, text: str, *, action: str,
               status: TaskStatus = TaskStatus.COMPLETED,
               extra: dict | None = None) -> AgentResult:
        return AgentResult(
            task_id=request.task_id, status=status,
            text=safety.redact(text),
            data={"finance": True, "action": action, **(extra or {})},
            agent=self.name)

    def _decline_advice(self, request: AgentRequest) -> AgentResult:
        self.store.audit("advice_declined")
        return AgentResult(
            task_id=request.task_id, status=TaskStatus.DECLINED,
            text=safety.ADVICE_REFUSAL,
            data={"finance": True, "action": "advice_refused",
                  "refusal": True},
            agent=self.name)

    def _month(self, request: AgentRequest) -> str:
        meta = request.metadata or {}
        month = str(meta.get("month") or "").strip()
        if len(month) == 7 and month[:4].isdigit() and month[5:7].isdigit():
            return month
        return date.today().strftime("%Y-%m")

    def _snapshot(self) -> None:
        try:
            self.checkpoint.save({"kind": "state",
                                  "state": self.store.summary()})
        except Exception:
            pass

    def _park(self, request: AgentRequest, action: str, payload: dict,
              detail: str) -> AgentResult:
        cp = {"task_id": request.task_id, "agent": self.name,
              "action": action, "payload": payload,
              "status": "awaiting_approval"}
        self.checkpoint.save_for(request.task_id, cp)
        self.store.audit(f"parked_{action}")
        return safety.approval_result(request, self.name, action, detail)

    _HANDLERS = {}   # filled at the bottom

    # ── read-only actions ──────────────────────────────────────────────
    def _summary(self, request: AgentRequest) -> AgentResult:
        month = self._month(request)
        text = engine.monthly_summary(
            self.store.expenses(), self.store.budgets(), month,
            user_categories=self.store.user_categories())
        self.store.audit("summary", month=month)
        return self._reply(request, text, action="summary",
                           extra={"month": month})

    def _budget(self, request: AgentRequest) -> AgentResult:
        month = self._month(request)
        status = engine.budget_status(
            self.store.expenses(), self.store.budgets(), month,
            user_categories=self.store.user_categories())
        lines = [f"Budget status for {month}:"]
        ov = status["overall"]
        if ov.get("limit"):
            lines.append(f"  overall: {ov['spent']:.2f} of "
                         f"{ov['limit']:.2f} ({ov.get('pct')}%)"
                         + (f" — {ov['label']}" if ov.get("label") != "ok"
                            else ""))
        else:
            lines.append(f"  overall: {ov['spent']:.2f} "
                         "(no overall limit set)")
        for c in status["categories"]:
            if not c.get("limit"):
                lines.append(f"  • {c['category']}: {c['spent']:.2f} "
                             "(unbudgeted)")
                continue
            tail = f" — {c['label']}" if c["label"] != "ok" else ""
            if c.get("pace") is not None and c["label"] != "over":
                tail += f" (pace {c['pace']}×)"
            lines.append(f"  • {c['category']}: {c['spent']:.2f} of "
                         f"{c['limit']:.2f} ({c['pct']}%){tail}")
        callouts = engine.overspend_report(status)
        if callouts:
            lines += [""] + [f"  ⚠ {c}" for c in callouts]
        self.store.audit("budget_status", month=month)
        return self._reply(request, "\n".join(lines), action="budget",
                           extra={"month": month,
                                  "status": status})

    def _recurring(self, request: AgentRequest) -> AgentResult:
        rows = engine.find_recurring(self.store.expenses())
        if not rows:
            return self._reply(request,
                               "No recurring payments detected yet — "
                               "I need at least 3 months of a vendor at "
                               "stable amounts.", action="recurring")
        lines = [f"Recurring payments detected ({len(rows)}):"]
        lines += [f"  • {r['vendor']} — ~{r['median_amount']:.2f} "
                  f"{r['currency']} every ~{r['interval_days']} days, "
                  f"{r['occurrences']}× since {r['last_date']} "
                  f"(next due ~{r['next_due']}, estimate)"
                  for r in rows]
        self.store.audit("recurring_list", count=len(rows))
        return self._reply(request, "\n".join(lines), action="recurring",
                           extra={"recurring": rows})

    def _categories(self, request: AgentRequest) -> AgentResult:
        user = self.store.user_categories()
        lines = [f"Categories (yours: {len(user)}, plus built-ins):"]
        for c in user:
            kws = ", ".join(c.get("keywords") or [])[:80]
            lines.append(f"  • {c['name']}" + (f" — {kws}" if kws else ""))
        if not user:
            lines.append("  (none of your own — built-ins apply: "
                         + ", ".join(schema.CATEGORY_NAMES) + ")")
        lines.append("Add one with metadata {finance_action: "
                     "'category_add', category_name: 'X', keywords: [...]}")
        return self._reply(request, "\n".join(lines), action="categories")

    def _expenses(self, request: AgentRequest) -> AgentResult:
        month = self._month(request)
        rows = self.store.expenses(month=month)
        if not rows:
            return self._reply(request,
                               f"No expenses recorded for {month}.",
                               action="expenses")
        lines = [f"Expenses for {month} ({len(rows)}):"]
        lines += [f"  • [{r['id']}] {r['date']} {r['vendor']} — "
                  f"{r['amount']:.2f} {r['currency']} ({r['category']})"
                  for r in rows[-30:]]
        return self._reply(request, "\n".join(lines), action="expenses")

    def _parse(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        text = str(meta.get("receipt_text") or request.text or "")
        result = parser.parse_receipt(text)
        self.store.audit("receipt_parsed", ok=bool(result["ok"]))
        lines = ["Receipt preview (NOT imported yet):", ""]
        lines.append(result["preview"] or "(nothing readable)")
        if result["errors"]:
            lines += ["", "Unreadable: " + "; ".join(result["errors"])]
        lines += ["", "Say 'import this receipt' (or reply to the "
                     "approval) to save it — imports always need your "
                     "confirmation."]
        return self._reply(request, "\n".join(lines), action="parse",
                           extra={"confidence": result["confidence"],
                                  "ok": result["ok"]})

    # ── mutating actions (ALL park behind approval) ────────────────────
    def _import(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        text = str(meta.get("receipt_text") or meta.get("statement_text")
                   or request.text or "")
        rows: list[dict] = []
        notes: list[str] = []
        if isinstance(meta.get("expenses"), list) and meta["expenses"]:
            rows = [r for r in meta["expenses"] if isinstance(r, dict)]
            notes.append(f"{len(rows)} record(s) from metadata")
        else:
            parsed = parser.parse_receipt(text)
            if parsed["expense"]:
                rows.append(parsed["expense"])
                notes.append("1 receipt")
            statement = parser.parse_statement(text)
            if statement["expenses"]:
                rows.extend(statement["expenses"])
                notes.append(f"{len(statement['expenses'])} statement "
                             "line(s)")
                if statement["skipped_count"]:
                    notes.append(f"{statement['skipped_count']} line(s) "
                                 "skipped (no readable date+amount)")
        if not rows:
            return self._reply(request,
                               "I couldn't read any expense from that. "
                               "Paste the receipt text, or provide "
                               "metadata {expenses: [{date, vendor, "
                               "amount, currency}]}.", action="import",
                               status=TaskStatus.NEEDS_INPUT)
        clean_rows, dropped, drop_reasons = [], 0, []
        for r in rows:
            good, errors = schema.validate_expense(r)
            if good is None:
                dropped += 1
                drop_reasons.extend(errors[:2])
            else:
                clean_rows.append(good)
        if not clean_rows:
            why = "; ".join(dict.fromkeys(drop_reasons)) or \
                "nothing readable"
            return self._reply(request,
                               f"Nothing importable: {dropped} row(s) "
                               f"failed validation ({why}). Nothing was "
                               "saved — fix the input and try again.",
                               action="import")
        # categorize NOW so the human approves the final record
        user_cats = self.store.user_categories()
        for r in clean_rows:
            cat, _by = engine.categorize(r, user_cats)
            r["category"] = cat
        existing_keys = {schema.dedupe_key(x)
                         for x in self.store.expenses()}
        dupe_count = sum(1 for r in clean_rows
                         if schema.dedupe_key(r) in existing_keys)
        summary_bits = [f"{len(clean_rows)} record(s)"] + notes
        if dupe_count:
            summary_bits.append(f"{dupe_count} look(s) like duplicates of "
                                "records already in the vault")
        if dropped:
            summary_bits.append(f"{dropped} unreadable row(s) will be "
                                "dropped")
        detail = ("import into the finance vault: "
                  + ", ".join(summary_bits) + ". Nothing is saved until "
                    "you approve.")
        if len(clean_rows) <= 3:                  # show what will be saved
            detail += "\n  Records: " + "; ".join(
                f"{r['vendor']} {r['amount']:.2f} {r['currency']} on "
                f"{r['date']} (category: {r['category']})"
                for r in clean_rows)
        return self._park(request, "import_expenses",
                          {"rows": clean_rows}, detail)

    def _add(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        row = {k: meta[k] for k in ("date", "vendor", "amount", "currency",
                                    "note", "category") if k in meta}
        clean, errors = schema.validate_expense(row)
        if clean is None:
            return self._reply(request,
                               "I need date, vendor and a positive amount "
                               "to record an expense (metadata {date: "
                               "'2026-10-07', vendor: 'Zomato', amount: "
                               "450}). Unreadable: " + "; ".join(errors),
                               action="add", status=TaskStatus.NEEDS_INPUT)
        cat, _by = engine.categorize(clean, self.store.user_categories())
        clean["category"] = clean["category"] if \
            clean.get("category") != "other" else cat
        detail = (f"add expense {clean['vendor']} {clean['amount']:.2f} "
                  f"{clean['currency']} on {clean['date']} "
                  f"(category: {clean['category']}) to the vault")
        return self._park(request, "add_expense", {"row": clean}, detail)

    def _edit(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        expense_id = str(meta.get("expense_id") or "").strip()
        patch = {k: meta[k] for k in ("vendor", "note", "category")
                 if k in meta}
        if not expense_id or not patch:
            return self._reply(request,
                               "Which record, and what should change? "
                               "(metadata {expense_id, vendor/note/category})",
                               action="edit", status=TaskStatus.NEEDS_INPUT)
        hit = self.store.get_expense(expense_id)
        if not hit:
            return self._reply(request,
                               f"No expense with id {expense_id!r}.",
                               action="edit")
        detail = (f"edit {hit['vendor']} {hit['amount']:.2f} "
                  f"[{expense_id}]: "
                  + ", ".join(f"{k}→{v}" for k, v in patch.items()))
        return self._park(request, "update_expense",
                          {"expense_id": expense_id, "patch": patch},
                          detail)

    def _delete(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        expense_id = str(meta.get("expense_id") or "").strip()
        if not expense_id:
            return self._reply(request, "Which expense id should I delete? "
                               "(metadata {expense_id})", action="delete",
                               status=TaskStatus.NEEDS_INPUT)
        hit = self.store.get_expense(expense_id)
        if not hit:
            return self._reply(request,
                               f"No expense with id {expense_id!r}.",
                               action="delete")
        detail = (f"DELETE expense {hit['vendor']} "
                  f"{hit['amount']:.2f} {hit['currency']} on "
                  f"{hit['date']} [{expense_id}] from the vault")
        return self._park(request, "delete_expense",
                          {"expense_id": expense_id}, detail)

    def _recategorize(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        expense_id = str(meta.get("expense_id") or "").strip()
        category = str(meta.get("category") or
                       meta.get("new_category") or "").strip().lower()
        if not expense_id or not category:
            return self._reply(request,
                               "Which record, and into which category? "
                               "(metadata {expense_id, category})",
                               action="recategorize",
                               status=TaskStatus.NEEDS_INPUT)
        hit = self.store.get_expense(expense_id)
        if not hit:
            return self._reply(request,
                               f"No expense with id {expense_id!r}.",
                               action="recategorize")
        detail = (f"recategorize {hit['vendor']} {hit['amount']:.2f} "
                  f"[{expense_id}] from '{hit.get('category')}' to "
                  f"'{category}'")
        return self._park(request, "update_expense",
                          {"expense_id": expense_id,
                           "patch": {"category": category}}, detail)

    # ── config actions (SAFE_ACTION: no records touched) ───────────────
    def _budget_set(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        budgets = meta.get("budgets")
        if not isinstance(budgets, dict) or not budgets:
            single = {k[7:]: meta[k] for k in ("budget_food", "budget_transport",
                                               "budget_utilities", "budget_rent",
                                               "budget_shopping", "budget_health",
                                               "budget_entertainment",
                                               "budget_subscriptions",
                                               "budget_monthly_total",
                                               "budget_other") if k in meta}
            budgets = single or {}
            if not budgets and any(k.startswith("budget_") for k in meta):
                budgets = {k[len("budget_"):]: meta[k]
                           for k in meta if k.startswith("budget_")}
        if not budgets:
            return self._reply(request,
                               "Give me limits as metadata, e.g. "
                               "{budgets: {food: 8000, transport: 3000, "
                               "monthly_total: 25000}}.",
                               action="budget_set",
                               status=TaskStatus.NEEDS_INPUT)
        saved = self.store.set_budgets(budgets)
        self._snapshot()
        lines = ["Monthly budgets saved:"]
        lines += [f"  • {k}: {v:.2f}" for k, v in sorted(saved.items())]
        return self._reply(request, "\n".join(lines), action="budget_set")

    def _category_add(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        name = str(meta.get("category_name") or
                   meta.get("category") or "").strip().lower()
        keywords = [str(k).lower() for k in (meta.get("keywords") or [])]
        if not name:
            return self._reply(request,
                               "What's the category name? (metadata "
                               "{category_name: 'pet care', keywords: "
                               "['vet', 'petco']})", action="category_add",
                               status=TaskStatus.NEEDS_INPUT)
        cats = [c for c in self.store.user_categories()
                if c.get("name") != name]
        cats.append({"name": name, "keywords": keywords})
        saved = self.store.set_user_categories(cats)
        self._snapshot()
        return self._reply(request,
                           f"Category '{name}' saved with keywords: "
                           f"{', '.join(keywords) or '(none)'}. Your "
                           "categories are matched before the built-ins.",
                           action="category_add")

    def _category_remove(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        name = str(meta.get("category_name") or
                   meta.get("category") or "").strip().lower()
        if not name:
            return self._reply(request, "Which category should I remove?",
                               action="category_remove",
                               status=TaskStatus.NEEDS_INPUT)
        cats = self.store.user_categories()
        remaining = [c for c in cats if c.get("name") != name]
        if len(remaining) == len(cats):
            return self._reply(request,
                               f"'{name}' isn't one of your categories.",
                               action="category_remove")
        self.store.set_user_categories(remaining)
        self._snapshot()
        return self._reply(request,
                           f"Category '{name}' removed (records keep "
                           "their saved category).",
                           action="category_remove")

    def _budgets_view(self, request: AgentRequest) -> AgentResult:
        budgets = self.store.budgets()
        if not budgets:
            return self._reply(request,
                               "No budgets set yet — add some with "
                               "metadata {budgets: {food: 8000, ...}}.",
                               action="budgets_view")
        lines = ["Monthly budgets:"]
        lines += [f"  • {k}: {v:.2f}" for k, v in sorted(budgets.items())]
        return self._reply(request, "\n".join(lines), action="budgets_view")

    # ── the approval path: execute the EXACT saved payload ─────────────
    def _resume_approved(self, request: AgentRequest, resume_id: str,
                         by: str) -> AgentResult:
        cp = self.checkpoint.load_for(resume_id)
        if not cp or cp.get("task_id") != resume_id:
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               text=f"No pending approval checkpoint for "
                                    f"{resume_id!r} — nothing was changed.",
                               agent=self.name)
        if cp.get("status") != "awaiting_approval":
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.DECLINED,
                               text=f"Approval {resume_id!r} is "
                                    f"'{cp.get('status')}' — already "
                                    "handled.",
                               agent=self.name)
        action = cp.get("action")
        payload = cp.get("payload") or {}
        user_cats = self.store.user_categories()

        if action == "import_expenses":
            result = self.store.add_expenses(payload.get("rows") or [],
                                             user_cats)
            cp["status"] = "imported"
            self.checkpoint.save_for(resume_id, cp)
            self._snapshot()
            return self._reply(request,
                               f"Imported (approved by {by}): "
                               f"{result['added']} record(s) added, "
                               f"{result['skipped_duplicates']} duplicate(s) "
                               f"skipped, {result['invalid']} invalid "
                               "row(s) dropped.", action="import",
                               extra={"result": result})
        if action == "add_expense":
            result = self.store.add_expenses([payload.get("row") or {}],
                                             user_cats)
            cp["status"] = "added"
            self.checkpoint.save_for(resume_id, cp)
            self._snapshot()
            if result["added"]:
                return self._reply(request,
                                   f"Expense recorded (approved by {by}). "
                                   f"Vault now has "
                                   f"{len(self.store.expenses())} "
                                   "record(s).", action="add",
                                   extra={"result": result})
            return self._reply(request,
                               "Nothing added — the record was a "
                               "duplicate of one already in the vault.",
                               action="add", extra={"result": result})
        if action == "update_expense":
            hit = self.store.update_expense(payload.get("expense_id", ""),
                                            payload.get("patch") or {})
            cp["status"] = "updated" if hit else "failed"
            self.checkpoint.save_for(resume_id, cp)
            if hit:
                self._snapshot()
                return self._reply(request,
                                   f"Record updated (approved by {by}): "
                                   f"{hit['vendor']} is now "
                                   + ", ".join(f"{k}={v}" for k, v in
                                               (payload.get("patch")
                                                or {}).items()) + ".",
                                   action="edit")
            return self._reply(request, "The record could not be updated.",
                               action="edit")
        if action == "delete_expense":
            hit = self.store.delete_expense(payload.get("expense_id", ""))
            cp["status"] = "deleted" if hit else "failed"
            self.checkpoint.save_for(resume_id, cp)
            if hit:
                self._snapshot()
                return self._reply(request,
                                   f"Deleted (approved by {by}): "
                                   f"{hit['vendor']} "
                                   f"{hit['amount']:.2f} on "
                                   f"{hit['date']}.", action="delete")
            return self._reply(request, "The record could not be deleted.",
                               action="delete")
        return AgentResult(task_id=request.task_id, status=TaskStatus.FAILED,
                           text=f"Unknown approved action {action!r}.",
                           agent=self.name)

    _HANDLERS = {
        "summary": _summary,
        "budget": _budget,
        "budgets_view": _budgets_view,
        "budget_set": _budget_set,
        "recurring": _recurring,
        "categories": _categories,
        "category_add": _category_add,
        "category_remove": _category_remove,
        "expenses": _expenses,
        "parse": _parse,
        "import": _import,
        "add": _add,
        "edit": _edit,
        "delete": _delete,
        "recategorize": _recategorize,
    }