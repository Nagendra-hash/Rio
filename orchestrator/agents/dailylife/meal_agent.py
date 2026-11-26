"""
orchestrator/agents/dailylife/meal_agent.py — the Meal & Grocery Agent
(Phase 13).

Everything is local and deterministic: preferences, pantry (with expiry),
weekly menu from the built-in template library, grocery list = menu
ingredients minus pantry, budget awareness from the user's OWN price
hints. No price is ever presented as live — no live price source exists
in this agent. Writes (pantry/prefs/menu) snapshot a crash-safe
checkpoint afterwards.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime

from orchestrator.agents.dailylife import meal_logic
from orchestrator.agents.dailylife.checkpoint import DailyLifeCheckpoint
from orchestrator.agents.dailylife.store import DailyLifeStore
from orchestrator.agents.productivity import security
from orchestrator.base import BaseAgent
from orchestrator.contracts import (AgentRequest, AgentResult, TaskStatus,
                                    TaskType)

log = logging.getLogger("jarvis.meal")


class MealAgent(BaseAgent):
    name = "meal_agent"
    description = ("Meal & grocery specialist: preferences, pantry and "
                   "expiry tracking, a weekly menu from built-in templates, "
                   "a grocery list (menu minus pantry), and budget "
                   "awareness from your saved price hints — never live "
                   "prices.")
    task_types = (TaskType.MEAL,)

    def __init__(self, store: DailyLifeStore | None = None,
                 checkpoint: DailyLifeCheckpoint | None = None) -> None:
        self.store = store or DailyLifeStore()
        self.checkpoint = checkpoint or DailyLifeCheckpoint()

    # ── entry point ───────────────────────────────────────────────────────
    def handle(self, request: AgentRequest) -> AgentResult:
        try:
            action = str((request.metadata or {}).get("meal_action")
                         or "").strip().lower() or self._intent(request.text)
            handler = self._HANDLERS.get(action, MealAgent._menu)
            return handler(self, request)
        except Exception as exc:                     # never raise outward
            log.warning("meal agent error: %s", type(exc).__name__)
            return AgentResult(task_id=request.task_id,
                               status=TaskStatus.FAILED,
                               error="meal action failed", agent=self.name)

    @staticmethod
    def _intent(text: str) -> str:
        t = (text or "").lower()
        if re.search(r"\b(?:expir\w*|going bad|gone bad|stale)\b", t):
            return "expiry"
        if re.search(r"\b(?:used|consumed|ran out of|finished off)\b", t):
            return "pantry_take"
        if re.search(r"\b(?:pantry|fridge|stock)\b", t):
            if re.search(r"\b(?:add|put|stock)\b", t):
                return "pantry_add"
            if re.search(r"\b(?:used|use|consume|ran out|remove|finished)\b", t):
                return "pantry_take"
            return "pantry_list"
        if re.search(r"\bgrocer\w*|\bshopping list\b", t):
            return "grocery"
        if re.search(r"\b(?:prefs?|preferences|diet|budget|price hints?)\b", t):
            return "prefs"
        return "menu"

    # ── result helper ─────────────────────────────────────────────────────
    def _reply(self, request: AgentRequest, text: str, *, action: str,
               status: TaskStatus = TaskStatus.COMPLETED,
               extra: dict | None = None) -> AgentResult:
        return AgentResult(
            task_id=request.task_id, status=status,
            text=security.redact(text),
            data={"dailylife": True, "domain": "meal",
                  "action": action, **(extra or {})},
            agent=self.name)

    def _snapshot(self, kind: str) -> None:
        try:
            self.checkpoint.save({"kind": kind,
                                  "state": self.store.summary()})
        except Exception:
            pass

    # ── actions ───────────────────────────────────────────────────────────
    def _menu(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        rebuild = bool(meta.get("rebuild")) or \
            re.search(r"\b(?:build|plan|new|make)\b", request.text or "",
                      re.IGNORECASE) is not None
        if not rebuild:
            saved = self.store.last_menu()
            if saved:
                return self._reply(request, meal_logic.render_menu(saved),
                                   action="menu",
                                   extra={"built_at": saved.get("built_at")})
        prefs = self.store.prefs_get("meal")
        # metadata wins for THIS build (the store keeps its own values)
        for key in ("diet", "allergies", "household"):
            if key in meta and meta[key] is not None:
                prefs[key] = meta[key]
        start = self._parse_day(str(meta.get("start") or "")) or date.today()
        menu = meal_logic.build_menu(prefs, self.store.pantry_items(),
                                     start=start,
                                     overrides=meta.get("overrides"))
        self.store.save_menu(menu)
        self._snapshot("menu")
        return self._reply(request, meal_logic.render_menu(menu),
                           action="menu", extra={"start": menu["start"]})

    def _grocery(self, request: AgentRequest) -> AgentResult:
        prefs = self.store.prefs_get("meal")
        menu = self.store.last_menu()
        if not menu:
            start = date.today()
            menu = {"start": start.isoformat(),
                    **meal_logic.build_menu(prefs, self.store.pantry_items(),
                                            start=start)}
            self.store.save_menu(menu)
        grocery = meal_logic.grocery_list(
            menu, self.store.pantry_items(),
            prices=prefs.get("price_hints") or {},
            budget=prefs.get("budget_week"))
        self.store.save_grocery(grocery)
        self._snapshot("grocery")
        return self._reply(request, meal_logic.render_grocery(grocery),
                           action="grocery",
                           extra={"items": len(grocery.get("buy") or []),
                                  "est_cost": grocery.get("est_cost"),
                                  "budget": grocery.get("budget")})

    def _pantry_add(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        name, qty, unit, expires = self._parse_item(request, meta)
        if not name:
            return self._reply(request, "What should I add to the pantry? "
                               "(e.g. metadata {pantry_item: 'rice', "
                               "qty: 2, unit: 'kg', expires: '2026-10-20'})",
                               action="pantry_add",
                               status=TaskStatus.NEEDS_INPUT)
        row = self.store.pantry_add(name, qty=qty, unit=unit, expires=expires)
        self._snapshot("pantry")
        return self._reply(request,
                           f"Pantry updated: {row['name']} — {row['qty']} "
                           f"{row['unit']}".rstrip()
                           + (f" (expires {row['expires']})"
                              if row["expires"] else "") + ".",
                           action="pantry_add")

    def _pantry_take(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        name, qty, _unit, _exp = self._parse_item(request, meta)
        if not name:
            return self._reply(request, "Which item did you use up?",
                               action="pantry_take",
                               status=TaskStatus.NEEDS_INPUT)
        hit = self.store.pantry_take(name, qty=qty)
        if not hit:
            return self._reply(request,
                               f"'{name}' isn't in the pantry.",
                               action="pantry_take")
        self._snapshot("pantry")
        remaining = next((r for r in self.store.pantry_items()
                          if r["id"] == hit["id"]), None)
        if remaining:
            return self._reply(request,
                               f"Noted: {remaining['name']} — "
                               f"{remaining['qty']} {remaining['unit']} left.",
                               action="pantry_take")
        return self._reply(request,
                           f"Noted: {hit['name']} is finished — removed "
                           f"from the pantry.", action="pantry_take")

    def _pantry_list(self, request: AgentRequest) -> AgentResult:
        pantry = self.store.pantry_items()
        if not pantry:
            return self._reply(request, "The pantry list is empty. Add "
                               "items with metadata {pantry_item, qty, "
                               "unit, expires}.", action="pantry_list")
        lines = [f"Pantry ({len(pantry)} item(s)):"]
        for r in pantry:
            exp = f" (expires {r['expires']})" if r.get("expires") else ""
            lines.append(f"  • {r['name']} — {r.get('qty', 0)} "
                         f"{r.get('unit', '')}".rstrip() + exp)
        return self._reply(request, "\n".join(lines), action="pantry_list")

    def _expiry(self, request: AgentRequest) -> AgentResult:
        report = meal_logic.expiry_report(self.store.pantry_items())
        return self._reply(request, meal_logic.render_expiry(report),
                           action="expiry",
                           extra={"expired": len(report["expired"]),
                                  "soon": len(report["soon"])})

    def _prefs(self, request: AgentRequest) -> AgentResult:
        meta = request.metadata or {}
        patch = {}
        if "diet" in meta:
            patch["diet"] = str(meta["diet"]).strip().lower()
        if "allergies" in meta:
            patch["allergies"] = [str(a).strip().lower()
                                  for a in (meta["allergies"] or [])]
        if "household" in meta:
            patch["household"] = max(1, int(meta["household"] or 1))
        if "budget_week" in meta:
            patch["budget_week"] = float(meta["budget_week"] or 0)
        if "price_hints" in meta and isinstance(meta["price_hints"], dict):
            patch["price_hints"] = {str(k).lower(): float(v)
                                    for k, v in meta["price_hints"].items()
                                    if v is not None}
        prefs = self.store.prefs_set("meal", patch) if patch \
            else self.store.prefs_get("meal")
        self._snapshot("prefs") if patch else None
        lines = ["Meal preferences:"] + [
            f"  • {k}: {v}" for k, v in sorted(prefs.items())]
        if not prefs:
            lines.append("  (empty — set diet, allergies, household, "
                         "budget_week, price_hints via metadata)")
        return self._reply(request, "\n".join(lines), action="prefs")

    # ── helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _parse_day(value: str):
        try:
            return datetime.strptime(str(value or "").strip()[:10],
                                     "%Y-%m-%d").date()
        except ValueError:
            return None

    def _parse_item(self, request: AgentRequest, meta: dict) -> tuple:
        """(name, qty, unit, expires) from metadata first, then the text.
        Text shapes handled:
          add:        'add rice', 'add rice to the pantry'
          take:       'used 500 g rice', 'used rice', 'ran out of rice'
        """
        name = str(meta.get("pantry_item") or meta.get("item") or "").strip()
        qty = 0.0
        try:
            qty = float(meta.get("qty") or 0.0)
        except (TypeError, ValueError):
            qty = 0.0
        unit = str(meta.get("unit") or "")
        expires = str(meta.get("expires") or "")
        text = request.text or ""
        if not name and text:
            # consume phrasing: 'used [qty unit] item'
            m = re.search(r"\b(?:used|consumed|ran\s+out\s+of|finished\s+off)"
                          r"\b\s*(.*)$", text, re.IGNORECASE)
            if m:
                rest = m.group(1).strip() or ""
                qm = re.match(r"^(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s+(.+)$",
                              rest)
                if qm:
                    try:
                        qty = qty or float(qm.group(1))
                    except (TypeError, ValueError):
                        qty = qty or 0.0
                    unit = unit or qm.group(2)
                    name = qm.group(3).strip()
                else:
                    name = rest
            else:
                # add phrasing: 'add [the] item [to the pantry]'
                m = re.search(r"\b(?:add|put|stock)\b\s*(?:the\s+)?"
                              r"(.*?)\s*(?:\b(?:to|in|into)\s+(?:the\s+)?"
                              r"(?:pantry|fridge|stock)\b)?\s*$",
                              text, re.IGNORECASE)
                if m and m.group(1).strip():
                    name = m.group(1).strip()
        return name, qty, unit, expires

    _HANDLERS = {
        "menu": _menu,
        "grocery": _grocery,
        "pantry_add": _pantry_add,
        "pantry_take": _pantry_take,
        "pantry_list": _pantry_list,
        "expiry": _expiry,
        "prefs": _prefs,
    }
