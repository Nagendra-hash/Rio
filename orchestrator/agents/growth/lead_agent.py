"""
orchestrator/agents/growth/lead_agent.py — the LEAD SCORE agent (Phase 18).

BaseAgent contract, canonical name `lead_agent`, enabled with
JARVIS_LEAD_AGENT=on (shadow-mode default).

ACTIONS

    score (default)     read the vault's leads, apply the user's stored
                        rules, reply with the PRIORITIZED outreach list —
                        every score carries its per-rule breakdown, and
                        every reply ends with the human-review disclaimer.
    rules               show the current scoring rules with points.
    add_rule            propose a user-defined rule; PARKS the exact rule
                        behind approval; resume saves it to the vault.
                        A saved rule changes future scores — that is a
                        write, hence gated.
    edit_rule / drop_rule   same gate, exact payload.
    ingest              approval-gated ADD of permitted lead data
                        (metadata.path口红 a json/csv/txt of lead dicts,
                        or metadata.leads inline). Resume commits the
                        parsed rows — dedupe counts are honest.
    forget_lead         approval-gated remove (privacy right).
    review              per-lead detail: score, breakdown, last actions —
                        the artifact a human reviews before outreach.
    mark_contacted      approval-gated consequential action.

SAFETY (in code)
    * scoring is READ_ONLY — it never needs approval and never writes;
    * ingest / rules-fork-write / forget / mark_* park the EXACT payload
      (checkpoint) and the approved re-dispatch executes that payload;
    * outreach execution (sending, dialing) is BLOCKED — the agent
      prioritizes, a human reaches out (safety.EXECUTION_REFUSAL).
    * a lead with zero rule hits is reported cold with "no defined rule
      matched" — never imputed points.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from orchestrator.agents.growth import lead_scoring, safety, shared
from orchestrator.agents.growth.lead_scoring import LeadScore
from orchestrator.agents.growth.shared import (GrowthCheckpoint,
                                               GrowthVault)
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)

log = logging.getLogger("jarvis.lead")

HUMAN_REVIEW_NOTE = shared.HUMAN_REVIEW_NOTE
MAX_CSV_ROWS = 500
SUPPORTED_SUFFIXES = (".json", ".csv", ".txt", ".md", ".log")



class LeadAgent(BaseAgent):
    name = "lead_agent"
    description = (
        "Lead scoring: ingests PERMITTED lead data behind approval, "
        "applies USER-DEFINED scoring rules deterministically, explains "
        "every factor with a per-rule breakdown, and prioritizes outreach. "
        "A score is advisory — a human reviews and decides anything "
        "consequential; execution of outreach is refused in code.")
    task_types = (TaskType.LEAD_SCORE,)

    def __init__(self, store: GrowthVault | None = None,
                 checkpoint: GrowthCheckpoint | None = None) -> None:
        self.store = store or GrowthVault()
        self.checkpoint = checkpoint or GrowthCheckpoint()
        self._last_scores: list[LeadScore] = []

    # ── entry ────────────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            meta = request.metadata or {}
            if meta.get("approved") and meta.get("resume"):
                return self._resume_approved(request, str(meta["resume"]),
                                             str(meta.get("by") or "user"))
            if safety.is_execution_request(request.text):
                self.store.audit("execution_refused")
                return AgentResult(
                    task_id=request.task_id, status=TaskStatus.DECLINED,
                    text=safety.EXECUTION_REFUSAL,
                    data={"lead": True, "action": "execution_refused",
                          "refusal": True}, agent=self.name)
            action = str(meta.get("lead_action") or "").strip().lower() \
                or self._intent(request)
            handler = self._HANDLERS.get(action, LeadAgent._score)
            return handler(self, request)
        except Exception as exc:                  # never raise outward
            log.warning("lead agent error: %s", type(exc).__name__)
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               error="lead action failed",
                               agent=self.name)

    def _intent(self, request: AgentRequest) -> str:
        t = " ".join(str(request.text or "").lower().split())
        if re.search(r"\b(?:add|new|create)\b.{0,24}\brule\b", t) or \
                "add_rule" in t:
            return "add_rule"
        if re.search(r"\b(?:drop|remove|delete)\b.{0,24}\brule\b", t) or \
                "drop_rule" in t:
            return "drop_rule"
        if re.search(r"\b(?:edit|change|retune|adjust)\b.{0,24}\brule\b", t):
            return "edit_rule"
        if re.search(r"\brules?\b", t):
            return "rules"
        if re.search(r"\b(?:ingest|import|add|load)\b.{0,30}"
                     r"\b(?:leads?|prospects?|contacts?)\b", t) or "ingest" in t:
            return "ingest"
        if re.search(r"\b(?:forget|delete|remove)\b.{0,20}\blead\b", t):
            return "forget_lead"
        if re.search(r"\b(?:review|detail|breakdown)\b.{0,30}"
                     r"\b(?:lead|score|contact)\b", t) \
                and str((request.metadata or {}).get("lead_id")):
            return "review"
        if re.search(r"\b(?:mark|set)\b.{0,20}\bcontacted\b", t):
            return "mark_contacted"
        return "score"

    # ── helpers ──────────────────────────────────────────────────────────
    def _reply(self, request, text, *, action: str,
               status: TaskStatus = TaskStatus.COMPLETED,
               extra: dict | None = None) -> AgentResult:
        from orchestrator.agents.productivity import security as prod_sec
        body = prod_sec.redact(str(text or ""))
        if status is TaskStatus.COMPLETED:
            body = (body.rstrip() + ("\n\n" if body else "")
                    + HUMAN_REVIEW_NOTE)
        return AgentResult(
            task_id=request.task_id, status=status, text=body,
            data={"lead": True, "action": action, **(extra or {})},
            agent=self.name)

    def _park(self, request, action: str, payload: dict,
              detail: str) -> AgentResult:
        cp = {"task_id": request.task_id, "agent": self.name,
              "action": action, "payload": payload,
              "status": "awaiting_approval"}
        self.checkpoint.save_for(request.task_id, cp)
        self.store.audit(f"parked_{action}")
        return safety.approval_result(request, self.name, action, detail)

    # ── the read-only core ───────────────────────────────────────────────
    def _score(self, request) -> AgentResult:
        leads = self.store.leads()
        rules = self.store.rules()
        if not rules:
            return self._reply(
                request,
                "No scoring rules defined yet — I won't invent your "
                "priorities. Add one with metadata {lead_action: "
                "'add_rule', rule: {name: 'senior-depth', points: 30, "
                "where: {industry: 'saas'}, min: {team_size: 50}}}. "
                "Every clause is optional but a rule must match something.",
                action="score", status=TaskStatus.NEEDS_INPUT)
        if not leads:
            return self._reply(
                request,
                "No leads stored yet — ingest permitted lead data with "
                "metadata {lead_action: 'ingest', path: '/path/to/"
                "leads.json'} (approval-gated).",
                action="score", status=TaskStatus.NEEDS_INPUT)
        scores = lead_scoring.score_leads(leads, rules)
        self._last_scores = scores
        self.store.log_score_write({
            "event": "scored", "at": shared._now_iso(),
            "leads": len(scores), "rules": len(rules)})
        ranked = lead_scoring.format_league(scores)
        detail = "\n\n".join(s.explain_line() for s in scores[:10])
        cold = [s for s in scores if s.grade == "cold"]
        body = ranked + "\n\nScore breakdown (top 10):\n" + detail
        if cold:
            body += (f"\n\n{len(cold)} lead(s) matched no rule — cold and "
                     "left out of outreach priority (add a rule that "
                     "covers them, or accept them as cold).")
        return self._reply(request, body, action="score",
                           extra={"scored": len(scores),
                                  "top": scores[0].lead_id if scores else "",
                                  "rules": len(rules)})

    def _rules(self, request) -> AgentResult:
        rules = self.store.rules()
        if not rules:
            return self._reply(request,
                               "No scoring rules yet — scores stay at 0 "
                               "until you define them. Your rules, your "
                               "weights: nothing here is a default "
                               "opinion.", action="rules")
        lines = [f"Scoring rules ({len(rules)}) — user-defined, applied in "
                 "this order:"]
        for i, r in enumerate(rules, 1):
            clauses = []
            if r.get("where"):
                clauses.append("where " + json.dumps(r["where"]))
            if r.get("min"):
                clauses.append("min " + json.dumps(r["min"]))
            if r.get("max"):
                clauses.append("max " + json.dumps(r["max"]))
            if r.get("has"):
                clauses.append("has " + ", ".join(r["has"]))
            lines.append(f"  {i:>2}. {r['name']:<24} +{r['points']}  "
                         f"{' · '.join(clauses)}")
        self.store.audit("rules_listed", n=len(rules))
        return self._reply(request, "\n".join(lines), action="rules",
                           extra={"rules": len(rules)})

    # ── rule writes (approval-gated) ─────────────────────────────────────
    def _rule_payload_from_meta(self, meta: dict) -> tuple[dict | None, str]:
        rule_raw = meta.get("rule") if isinstance(meta.get("rule"), dict) \
            else None
        if rule_raw is None and meta.get("rule_json"):
            try:
                rule_raw = json.loads(str(meta["rule_json"]))
            except Exception:
                rule_raw = None
        if rule_raw is None:
            return None, (".metadata {lead_action: 'add_rule', rule: "
                          "{name: '…', points: 30, where|min|max|has: …}} "
                          "defines the rule.")
        clean, errors = shared.clean_rule(rule_raw)
        if clean is None:
            return None, ("Rule rejected: " + "; ".join(errors[:3]) +
                          ". Fix the rule and re-propose it.")
        return clean, ""

    def _add_rule(self, request) -> AgentResult:
        meta = request.metadata or {}
        rule, err = self._rule_payload_from_meta(meta)
        if rule is None:
            return self._reply(request, err, action="add_rule",
                               status=TaskStatus.NEEDS_INPUT)
        detail = (f"SAVE scoring rule '{rule['name']}' "
                  f"(+{rule['points']} pts) into the rule set — future lead "
                  "scores change. Exact rule: " + json.dumps(rule)[:300])
        return self._park(request, "add_rule", {"rule": rule}, detail)

    def _edit_rule(self, request) -> AgentResult:
        meta = request.metadata or {}
        rule, err = self._rule_payload_from_meta(meta)
        name = (rule or {}).get("name", "")
        known = any(str(r.get("name") or "").strip() == name
                    for r in self.store.rules())
        if rule is None:
            return self._reply(request, err, action="edit_rule",
                               status=TaskStatus.NEEDS_INPUT)
        if not known:
            return self._reply(request,
                               f"No rule named '{name}' — say 'rules' to "
                               "list them.",
                               action="edit_rule")
        detail = (f"REPLACE scoring rule '{name}' — same parking rule as "
                  "add.")
        return self._park(request, "edit_rule", {"rule": rule}, detail)

    def _drop_rule(self, request) -> AgentResult:
        meta = request.metadata or {}
        name = str(meta.get("rule_name") or "").strip()
        rules = self.store.rules()
        if not name:
            return self._reply(request,
                               "Which rule? metadata {rule_name: '…'} — "
                               "'rules' lists them.", action="drop_rule",
                               status=TaskStatus.NEEDS_INPUT)
        hit = next((r for r in rules
                    if str(r.get("name") or "") == name), None)
        if not hit:
            return self._reply(request, f"No rule named '{name}'.",
                               action="drop_rule")
        detail = f"REMOVE scoring rule '{name}' from the rule set."
        return self._park(request, "drop_rule", {"rule_name": name},
                          detail)

    # ── ingest (approval-gated) ──────────────────────────────────────────
    def _ingest(self, request) -> AgentResult:
        meta = request.metadata or {}
        path = str(meta.get("path") or "").strip()
        rows = meta.get("leads") if isinstance(meta.get("leads"), list) \
            else None
        if not path and rows is None:
            return self._reply(
                request,
                "Which lead data? metadata {lead_action: 'ingest', path: "
                "'/path/to/leads.json'} (json list of dicts or a csv with "
                "a header row), or inline {leads: [{name: …, company: …,"
                " fields: {…}}]} — the write is approval-gated.",
                action="ingest", status=TaskStatus.NEEDS_INPUT)
        parsed, err = self._parse_rows(path, rows)
        if err:
            return self._reply(request, err, action="ingest")
        if not parsed:
            return self._reply(request,
                               "Parsed 0 usable leads — every row needs a "
                               "name or a company. Nothing proposed.",
                               action="ingest")
        detail = (f"INGEST {len(parsed)} permitted lead record(s) " +
                  (f"from {Path(path).name}" if path else "inline") +
                  " into the growth vault (dedupe on name+company,email). "
                  "Nothing stored until you approve.")
        return self._park(request, "ingest_leads",
                          {"rows": parsed[:MAX_CSV_ROWS],
                           "source": path or "inline"}, detail)

    def _parse_rows(self, path: str, rows) -> tuple[list[dict], str]:
        if rows is not None:
            out = []
            for r in (rows or [])[:MAX_CSV_ROWS]:
                cl = shared.clean_lead(r)
                if cl is not None:
                    out.append(cl)
            return out, ""
        p = Path(path).expanduser()
        if not p.exists() or not p.is_file():
            return [], f"No file at {path}."
        if p.suffix.lower() not in SUPPORTED_SUFFIXES:
            return [], (f"Unsupported type {p.suffix!r} — ingest json, "
                        "csv, txt, md or log lead lists.")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return [], f"Unreadable file: {exc}"
        if p.suffix.lower() == ".json":
            try:
                data = json.loads(text)
            except Exception as exc:
                return [], f"Invalid JSON: {exc}"
            if isinstance(data, dict) and isinstance(data.get("leads"), list):
                data = data["leads"]
            if not isinstance(data, list):
                return [], ("JSON must be a list of lead objects (or "
                            "{leads: […]})")
            out = []
            for r in data[:MAX_CSV_ROWS]:
                cl = shared.clean_lead(r)
                if cl is not None:
                    out.append(cl)
            return out, ""
        # csv / plain text: header row maps columns; name/company/email
        # are the identity columns, everything numeric-ish joins fields
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) < 2:
            return [], "file has no data rows under the header"
        header = [h.strip().strip('"').lower()
                  for h in lines[0].split(",")]
        if "name" not in header and "company" not in header:
            return [], "csv header needs at least a 'name' or 'company' column"
        out: list[dict] = []
        for line in lines[1:MAX_CSV_ROWS + 1]:
            cells = [c.strip().strip('"') for c in line.split(",")]
            if len(cells) != len(header):
                continue
            row = dict(zip(header, cells))
            fields = {}
            for k, v in row.items():
                if k in ("name", "company", "email", "phone", "notes",
                         "lead_id", "source"):
                    continue
                try:
                    fv = float(v)
                    fields[k] = int(fv) if fv == int(fv) else fv
                except (TypeError, ValueError):
                    fields[k] = v
            cl = shared.clean_lead({
                "lead_id": row.get("lead_id"),
                "name": row.get("name"),
                "company": row.get("company"),
                "email": row.get("email"),
                "phone": row.get("phone"),
                "source": row.get("source") or str(p.name),
                "notes": row.get("notes"),
                "fields": fields})
            if cl is not None:
                out.append(cl)
        return out, ""

    # ── consequential actions (all gated) ────────────────────────────────
    def _mark_contacted(self, request) -> AgentResult:
        meta = request.metadata or {}
        lead_id = str(meta.get("lead_id") or "").strip()
        if not lead_id:
            return self._reply(request,
                               "Which lead? metadata {lead_id: 'lead-…'} "
                               "— 'score' lists them.", action="mark_contacted",
                               status=TaskStatus.NEEDS_INPUT)
        lead = next((l for l in self.store.leads()
                     if l.get("lead_id") == lead_id), None)
        if not lead:
            return self._reply(request, f"No lead {lead_id!r} in the vault.",
                               action="mark_contacted")
        detail = (f"MARK lead '{lead.get('name') or lead_id}' as contacted "
                  "(an outreach action happened in the real world)")
        return self._park(request, "mark_contacted", {"lead_id": lead_id},
                          detail)

    def _forget_lead(self, request) -> AgentResult:
        meta = request.metadata or {}
        lead_id = str(meta.get("lead_id") or "").strip()
        if not lead_id:
            return self._reply(request,
                               "Which lead? metadata {lead_id: 'lead-…'}.",
                               action="forget_lead",
                               status=TaskStatus.NEEDS_INPUT)
        lead = next((l for l in self.store.leads()
                     if l.get("lead_id") == lead_id), None)
        if not lead:
            return self._reply(request, f"No lead {lead_id!r} in the vault.",
                               action="forget_lead")
        detail = (f"DELETE lead '{lead.get('name') or lead_id}' "
                  f"[{lead_id}] from the vault (a write)")
        return self._park(request, "forget_lead", {"lead_id": lead_id},
                          detail)

    # ── review (the human-review artifact) ───────────────────────────────
    def _review(self, request) -> AgentResult:
        meta = request.metadata or {}
        lead_id = str(meta.get("lead_id") or "").strip()
        rules = self.store.rules()
        leads = self.store.leads()
        lead = next((l for l in leads
                     if lead_id and l.get("lead_id") == lead_id), None)
        if lead is None and leads:
            lead = leads[0]
        if lead is None:
            return self._reply(request, "No leads stored yet.",
                               action="review")
        score = lead_scoring.score_lead(lead, rules)
        self._last_scores.append(score)
        writes = [w for w in self.store.score_writes(limit=50)
                  if w.get("event") == "scored"][-3:]
        lines = [f"Review sheet — {score.explain_line()}",
                 f"  company: {lead.get('company') or '—'}",
                 f"  email: {lead.get('email') or '—'}",
                 f"  added: {str(lead.get('added_at') or '')[:19]}",
                 f"  source: {lead.get('source') or '—'}"]
        if lead.get("notes"):
            lines.append(f"  notes: {str(lead['notes'])[:200]}")
        fields = lead.get("fields") or {}
        if fields:
            lines.append("  fields: "
                         + ", ".join(f"{k}={v}" for k, v in
                                     list(fields.items())[:10]))
        lines.append("")
        lines.append(f"Recent scoring runs: {len(writes)}"
                     + (f" (last {writes[-1].get('at', '')[:19]})"
                        if writes else " (none — score now first)"))
        lines.append("")
        lines.append("Outreach decision is yours: this sheet is evidence, "
                     "not a queue.")
        return self._reply(request, "\n".join(lines), action="review",
                           extra={"lead_id": score.lead_id,
                                  "total": score.total,
                                  "grade": score.grade,
                                  "breakdown": score.to_dict()["breakdown"]})

    # ── resume (the ONLY writes) ──────────────────────────────────────
    def _resume_approved(self, request, resume_id: str,
                         by: str) -> AgentResult:
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
        commits = {
            "add_rule": self._commit_add_rule,
            "edit_rule": self._commit_add_rule,
            "drop_rule": self._commit_drop_rule,
            "ingest_leads": self._commit_ingest,
            "mark_contacted": self._commit_mark_contacted,
            "forget_lead": self._commit_forget_lead,
        }
        commit = commits.get(str(cp.get("action") or ""))
        if commit is None:
            cp["status"] = "failed"
            self.checkpoint.save_for(resume_id, cp)
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               text=f"Unknown approved action "
                                    f"{cp.get('action')!r}.",
                               agent=self.name)
        return commit(request, resume_id, cp.get("payload") or {}, by)

    def _commit_add_rule(self, request, resume_id: str, payload: dict,
                         by: str) -> AgentResult:
        rule = payload.get("rule") if isinstance(payload.get("rule"), dict) \
            else None
        if not rule:
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               text="Approved payload carried no rule.",
                               agent=self.name)
        rules = self.store.rules()
        rules = [r for r in rules
                 if str(r.get("name") or "") != str(rule.get("name") or "")]
        rules.append(rule)
        self.store.save_rules(rules)
        self._touch_cp(resume_id, "add_rule", payload,
                       f"saved:{rule.get('name')}")
        self.store.audit("rule_saved", name=str(rule.get("name"))[:60],
                         by=by[:60])
        return self._reply(
            request,
            f"Saved (approved by {by}): rule '{rule.get('name')}' "
            f"(+{rule.get('points')} pts, match "
            f"{json.dumps({k: rule.get(k) for k in ('where', 'min', 'max', 'has') if rule.get(k)})[:200]}). "
            "Score now to see its effect.",
            action="add_rule")

    def _commit_drop_rule(self, request, resume_id: str, payload: dict,
                          by: str) -> AgentResult:
        name = str(payload.get("rule_name") or "")
        rules = self.store.rules()
        keep = [r for r in rules if str(r.get("name") or "") != name]
        self.store.save_rules(keep)
        self._touch_cp(resume_id, "drop_rule", payload, "dropped")
        self.store.audit("rule_dropped", name=name[:60], by=by[:60])
        return self._reply(request,
                           f"Removed (approved by {by}): rule '{name}'. "
                           f"{len(keep)} rule(s) remain.",
                           action="drop_rule")

    def _commit_ingest(self, request, resume_id: str, payload: dict,
                       by: str) -> AgentResult:
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) \
            else []
        result = self.store.add_leads(rows)
        self._touch_cp(resume_id, "ingest_leads", payload,
                       f"added:{result['added']}")
        lines = [f"Ingested (approved by {by}): "
                 f"{result['added']} lead(s) stored",
                 f"  duplicates skipped: {result['duplicates']}",
                 f"  invalid rows skipped: {result['invalid']}"]
        if not self.store.rules():
            lines.append("  next: define rules ({lead_action: 'add_rule'}) "
                         "— with no rules nothing scores above 0.")
        return self._reply(request, "\n".join(lines), action="ingest",
                           extra=result)

    def _commit_mark_contacted(self, request, resume_id: str,
                               payload: dict, by: str) -> AgentResult:
        lead_id = str(payload.get("lead_id") or "")
        self.store.log_activity({"event": "lead_contacted",
                                 "lead_id": lead_id, "by": by,
                                 "at": shared._now_iso()})
        self._touch_cp(resume_id, "mark_contacted", payload, "marked")
        self.store.audit("lead_marked_contacted", lead_id=lead_id[:60],
                         by=by[:60])
        return self._reply(
            request,
            f"Marked (approved by {by}): lead {lead_id} contacted. Re-score "
            "before the next round — contact history can be a rule input.",
            action="mark_contacted")

    def _commit_forget_lead(self, request, resume_id: str, payload: dict,
                            by: str) -> AgentResult:
        lead_id = str(payload.get("lead_id") or "")
        leads = self.store.leads()
        keep = [l for l in leads
                if str(l.get("lead_id") or "") != lead_id]
        self.store.save_leads(keep)
        self._touch_cp(resume_id, "forget_lead", payload, "forgotten")
        self.store.audit("lead_forgotten", lead_id=lead_id[:60],
                         by=by[:60])
        return self._reply(request,
                           f"Deleted (approved by {by}): lead {lead_id}. "
                           f"{len(keep)} lead(s) remain.",
                           action="forget_lead")

    def _touch_cp(self, resume_id: str, action: str, payload: dict,
                  status: str) -> None:
        self.checkpoint.save_for(resume_id, {
            "task_id": resume_id, "agent": self.name,
            "action": action, "payload": payload,
            "status": status})

    _HANDLERS = {
        "score": _score,
        "prioritize": _score,
        "rules": _rules,
        "add_rule": _add_rule,
        "new_rule": _add_rule,
        "edit_rule": _edit_rule,
        "drop_rule": _drop_rule,
        "ingest": _ingest,
        "import": _ingest,
        "review": _review,
        "detail": _review,
        "mark_contacted": _mark_contacted,
        "forget_lead": _forget_lead,
    }
