"""
orchestrator/agents/growth/lead_scoring.py — the scoring ENGINE.

The engine is deterministic && offline: user-defined rules in, explained
scores out, sorted outreach priorities back. No LLM anywhere in the path —
"apply user-defined scoring rules" is exactly the contract, and an LLM
would make the scores unexplainable.

SCORING (all three rules classes, evaluated per lead in rule order)
    where   exact string match on a lead field (case-insensitive)
    min     numeric floor on a lead field (field ≥ min)
    max     numeric ceiling on a lead field (field ≤ max)
    has     membership: field lists the token (fields["x"] in has)
            or a top-level token matches (industry/role buckets)

THE CONTRACT (hard rule)
    * a rule that matches NOTHING contributes 0 — never a silent floor,
      never default points;
    * every lead keeps its PER-RULE breakdown (which rule fired, its
      own points) — "explain score factors" is structural, not a summary;
    * ties are stable (rule order, then ingest order), so re-running the
      same rules on the same leads yields the same order — a priority
      list must be reproducible;
    * grades: hot ≥ 50, warm ≥ 30, cool ≥ 10, else cold. Grades are
      labels for humans, not actions.

HONESTY
    Leads with no rule hits score 0 with explanation "no defined rule
    matched" — a cold lead is a finding, not a failure. A lead missing
    a field a rule needs simply doesn't match that rule; the breakdown
    shows it. Nothing is imputed or guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

GRADE_BANDS = ((50, "hot"), (30, "warm"), (10, "cool"), (0, "cold"))
MAX_BREAKDOWN = 30            # per-lead rule hits carried in the reply


def _clip(v: Any, cap: int = 120) -> str:
    return str(v or "").strip()[:cap]


def _as_num(v: Any) -> float | None:
    try:
        if isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class RuleHit:
    rule: str
    points: int
    why: str

    def to_dict(self) -> dict:
        return {"rule": self.rule, "points": self.points, "why": self.why}


@dataclass
class LeadScore:
    lead_id: str
    name: str
    company: str
    total: int
    grade: str
    hits: list[RuleHit] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"lead_id": self.lead_id, "name": self.name,
                "company": self.company, "total": self.total,
                "grade": self.grade,
                "breakdown": [h.to_dict() for h in self.hits],
                "missed_rules": self.misses}

    def explain_line(self) -> str:
        head = (f"{self.name or self.company} [{self.lead_id}] — "
                f"{self.total} pts ({self.grade})")
        rows = [f"    +{h.points}  {h.rule}  ({h.why})" for h in self.hits]
        if self.misses:
            rows.append(f"    (did not match: {', '.join(self.misses[:6])})")
        if not rows:
            rows.append("    no defined rule matched — score 0")
        return "\n".join([head] + rows)


def grade_for(total: int) -> str:
    for floor, name in GRADE_BANDS:
        if total >= floor:
            return name
    return "cold"


def _lower(v: Any) -> str:
    return _clip(v).lower()


def _field_val(lead: dict, key: str) -> Any:
    """Top-level first (name/company/email/source), then the lead's
    fields dict (industries, counts, recency day offsets)."""
    key = _clip(key)
    if key in lead and not isinstance(lead.get(key), (dict, list)):
        return lead.get(key)
    fields = lead.get("fields")
    if isinstance(fields, dict) and key in fields:
        return fields.get(key)
    return None


def rule_matches(rule: dict, lead: dict) -> tuple[RuleHit | None, str]:
    """Does ONE rule fire on ONE lead? (hit, why) — (None, reason) when not."""
    name = _clip(rule.get("name"), 60) or "rule"
    points = int(rule.get("points") or 0)
    matched = True
    whys: list[str] = []

    where = rule.get("where") if isinstance(rule.get("where"), dict) else {}
    for key, want in where.items():
        got = _lower(_field_val(lead, str(key)))
        want_l = _lower(want)
        if got != want_l:
            return None, f"where {key}={got or '∅'} ≠ {want_l}"

    mins = rule.get("min") if isinstance(rule.get("min"), dict) else {}
    for key, floor in mins.items():
        num = _as_num(_field_val(lead, str(key)))
        floor_n = _as_num(floor)
        if num is None or floor_n is None or num < floor_n:
            return None, f"min {key}={('∅' if num is None else num)} < {floor}"

    maxs = rule.get("max") if isinstance(rule.get("max"), dict) else {}
    for key, ceil in maxs.items():
        num = _as_num(_field_val(lead, str(key)))
        ceil_n = _as_num(ceil)
        if num is None or ceil_n is None or num > ceil_n:
            return None, f"max {key}={('∅' if num is None else num)} > {ceil}"

    has_items = [str(v).strip().lower() for v in (rule.get("has") or [])
                 if str(v).strip()]
    if has_items:
        hay = {_lower(_field_val(lead, tok)) for tok in has_items}
        # a "has" token matches when ANY of those fields carries it — the
        # user writes has: ["decision_maker"], the lead carries it in a
        # field named like the token or as its value
        hit_tok = next((tok for tok in has_items
                        if _field_val(lead, tok) not in (None, "", False)
                        or any(_lower(v) == tok or
                               _lower(v) and tok in _lower(v)
                               for v in lead.get("fields", {}).values()
                               if isinstance(v, (str, int)))), None)
        if hit_tok is None:
            matched = False
        else:
            whys.append(hit_tok)
    if not matched:
        return None, "no rule clause matched"
    why = "; ".join(whys) if whys else "all clauses matched"
    return RuleHit(rule=name, points=points, why=why), why


def score_lead(lead: dict, rules: list[dict]) -> LeadScore:
    """One lead × the user's rules, in rule order. The breakdown IS the
    explanation."""
    hits: list[RuleHit] = []
    misses: list[str] = []
    for rule in rules or []:
        hit, why = rule_matches(rule, lead or {})
        if hit is not None:
            hits.append(hit)
        else:
            misses.append(_clip(rule.get("name"), 40) or "unnamed rule")
    total = sum(h.points for h in hits)
    return LeadScore(lead_id=_clip(lead.get("lead_id"), 60) or "?",
                     name=_clip(lead.get("name"), 80),
                     company=_clip(lead.get("company"), 80),
                     total=total, grade=grade_for(total),
                     hits=hits, misses=misses)


def score_leads(leads: list[dict], rules: list[dict]) -> list[LeadScore]:
    """All leads scored + prioritized. Ties are broken deterministically:
    higher total first, then rule order (earlier rules win), then the
    lead's place in the table — reproducible outreach ordering."""
    scores = [score_lead(l, rules) for l in (leads or [])]
    order = {(_clip(r.get("name"), 60)): i
             for i, r in enumerate(rules or [])}
    table = {_clip(l.get("lead_id"), 60): i
             for i, l in enumerate(leads or [])}
    scores.sort(key=lambda s: (-s.total,
                               order.get(min(s.hits, key=lambda h:
                                             order.get(h.rule, 10 ** 9)
                                             ).rule, 10 ** 9)
                               if s.hits else 10 ** 9,
                               table.get(s.lead_id, 10 ** 9)))
    return scores


def format_league(scores: list[LeadScore], cap: int = 10) -> str:
    lines = [f"Outreach priority ({len(scores)} lead(s) scored):"]
    for i, s in enumerate(scores[:cap], 1):
        lines.append(f"  {i:>2}. [{s.grade:>4}] {s.total:>4} pts — "
                     f"{s.name or s.company or s.lead_id}")
    if len(scores) > cap:
        lines.append(f"  … {len(scores) - cap} more")
    return "\n".join(lines)
