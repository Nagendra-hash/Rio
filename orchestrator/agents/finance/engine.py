"""
orchestrator/agents/finance/engine.py — the analysis engine.

Deterministic, read-only math over validated expense rows:

    categorize()        user-defined categories first (keyword match on
                        vendor/note/items), then built-ins, then "other".
    monthly_totals()    per-category and overall totals for a "YYYY-MM".
    budget_status()     spent vs limit per category + overall, with an
                        honest pace signal (spent vs pro-rata limit for
                        today's day-of-month) — flagged, never moralized.
    find_recurring()    vendors appearing ≥3 months with stable amounts
                        (±tolerance) and roughly monthly spacing →
                        recurring candidate with a next-due estimate.
    overspend_report()  categories over limit + categories ahead of pace.
    monthly_summary()   the human-readable summary the agent returns.

The engine NEVER: converts currencies, projects future spend as fact, or
ranks investments. Projections appear only as explicitly-labeled pace
signals.
"""
from __future__ import annotations

import calendar
import re
import statistics
from datetime import date
from typing import Optional

from orchestrator.agents.finance.schema import (DEFAULT_CATEGORIES,
                                                dedupe_key)

PACE_WARN = 1.10            # >10% ahead of pro-rata pace
RECURRENCE_TOLERANCE = 0.10
MIN_INTERVAL_DAYS, MAX_INTERVAL_DAYS = 20, 45


# ── categorization ───────────────────────────────────────────────────────────

def _haystack(row: dict) -> str:
    parts = [str(row.get("vendor") or ""), str(row.get("note") or "")]
    for it in row.get("items") or []:
        parts.append(str(it.get("name") or ""))
    return " ".join(parts).lower()


def _matches(cat: dict, haystack: str) -> bool:
    """Word-boundary keyword match — 'shop' hits 'corner shop', never
    'workshop'; deterministic and precision-first."""
    for kw in cat.get("keywords") or []:
        k = str(kw).strip().lower()
        if not k:
            continue
        if re.search(rf"\b{re.escape(k)}\b", haystack):
            return True
    return False


def category_index(user_categories: list[dict] | None) -> list[dict]:
    """User-defined categories first (in their order), then built-ins."""
    user = [c for c in (user_categories or [])
            if isinstance(c, dict) and str(c.get("name") or "").strip()]
    seen = {str(c["name"]).strip().lower() for c in user}
    builtins = [c for c in DEFAULT_CATEGORIES
                if c["name"] not in seen]
    return user + builtins


def categorize(row: dict, user_categories: list[dict] | None = None) -> tuple[str, str]:
    """→ (category_name, matched_by). Deterministic: first match wins."""
    hay = _haystack(row)
    if not hay.strip():
        return "other", "default"
    for cat in category_index(user_categories):
        if _matches(cat, hay):
            return str(cat["name"]).strip().lower(), "keyword"
    return "other", "default"


# ── totals & budgets ─────────────────────────────────────────────────────────

def month_of(row: dict) -> str:
    return str(row.get("date") or "")[:7]


def monthly_totals(rows: list[dict], month: str,
                   user_categories: list[dict] | None = None) -> dict:
    """Category → total for the month (rows are already categorized or
    get categorized here when the category is 'other' + uncategorized)."""
    totals: dict[str, float] = {}
    count = 0
    for r in rows:
        if month_of(r) != month:
            continue
        cat = str(r.get("category") or "other").lower()
        totals[cat] = round(totals.get(cat, 0.0)
                            + float(r.get("amount") or 0.0), 2)
        count += 1
    return {"month": month, "count": count, "totals": totals,
            "total": round(sum(totals.values()), 2)}


def budget_status(rows: list[dict], budgets: dict, month: str,
                  today: Optional[date] = None,
                  user_categories: list[dict] | None = None) -> dict:
    """Per-category spent vs limit + overall, with pace.

    pace = spent / (limit × day_fraction); >1.0 means spending ahead of
    the pro-rata budget. Labels: 'over' (limit crossed), 'ahead'
    (pace > PACE_WARN), 'ok'.
    """
    today = today or date.today()
    days_in_month = calendar.monthrange(*_parse_month(month))[1]
    day_frac = min(1.0, today.day / days_in_month) \
        if month == today.strftime("%Y-%m") else 1.0
    totals = monthly_totals(rows, month, user_categories)
    per = []
    categories = {**totals["totals"], **_clean_budgets(budgets)}
    categories.pop("monthly_total", None)   # the overall limit, not a category
    for cat in sorted(categories):
        spent = totals["totals"].get(cat, 0.0)
        limit = float(budgets.get(cat) or 0.0)
        entry = {"category": cat, "spent": spent, "limit": limit,
                 "pct": round(spent / limit * 100.0, 1) if limit > 0 else None,
                 "remaining": round(limit - spent, 2) if limit > 0 else None}
        if limit > 0 and spent > limit:
            entry["label"] = "over"
        elif limit > 0 and day_frac > 0:
            pace = spent / (limit * day_frac)
            entry["pace"] = round(pace, 2)
            entry["label"] = "ahead" if pace > PACE_WARN else "ok"
        else:
            entry["label"] = "unbudgeted" if limit <= 0 else "ok"
        per.append(entry)
    overall_limit = float(budgets.get("monthly_total") or 0.0)
    spent_all = totals["total"]
    overall = {"spent": spent_all, "limit": overall_limit,
               "pct": round(spent_all / overall_limit * 100.0, 1)
               if overall_limit > 0 else None}
    if overall_limit > 0:
        overall["label"] = "over" if spent_all > overall_limit else "ok"
        overall["pace"] = round(spent_all / (overall_limit * day_frac), 2) \
            if day_frac > 0 else None
    return {"month": month, "categories": per, "overall": overall,
            "expense_count": totals["count"]}


def _clean_budgets(budgets) -> dict:
    out = {}
    for k, v in (budgets or {}).items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv > 0:
            out[str(k).lower()] = fv
    return out


def _parse_month(month: str) -> tuple[int, int]:
    try:
        y, m = str(month).split("-")[:2]
        return int(y), int(m)
    except (ValueError, IndexError):
        today = date.today()
        return today.year, today.month


# ── recurring expenses ───────────────────────────────────────────────────────

def find_recurring(rows: list[dict], min_occurrences: int = 3,
                   tolerance: float = RECURRENCE_TOLERANCE,
                   today: Optional[date] = None) -> list[dict]:
    """Vendors paid repeatedly at stable amounts and roughly monthly
    spacing. Deterministic; 'next_due' is last_date + median interval —
    an estimate, labeled as such by the renderer."""
    today = today or date.today()
    by_vendor: dict[str, list[dict]] = {}
    for r in rows:
        v = str(r.get("vendor") or "").strip().lower()
        if v and v != "(unknown vendor)":
            by_vendor.setdefault(v, []).append(r)
    out = []
    for vendor, vrows in by_vendor.items():
        if len(vrows) < min_occurrences:
            continue
        vrows.sort(key=lambda r: r.get("date") or "")
        try:
            dates = [date.fromisoformat(r["date"][:10]) for r in vrows]
        except (KeyError, ValueError):
            continue
        amounts = [float(r.get("amount") or 0) for r in vrows]
        if min(amounts) <= 0:
            continue
        intervals = [(b - a).days for a, b in zip(dates, dates[1:])]
        if not intervals:
            continue
        med_interval = statistics.median(intervals)
        if not (MIN_INTERVAL_DAYS <= med_interval <= MAX_INTERVAL_DAYS):
            continue
        med_amount = statistics.median(amounts)
        if med_amount <= 0:
            continue
        spread = (max(amounts) - min(amounts)) / med_amount
        if spread > tolerance:
            continue
        months_span = len({d.strftime("%Y-%m") for d in dates})
        if months_span < 2:
            continue
        next_due = dates[-1].fromordinal(
            dates[-1].toordinal() + int(med_interval))
        out.append({
            "vendor": vrows[-1].get("vendor", vendor),
            "occurrences": len(vrows),
            "months_active": months_span,
            "median_amount": round(med_amount, 2),
            "interval_days": int(med_interval),
            "last_date": dates[-1].isoformat(),
            "next_due": next_due.isoformat(),
            "next_due_is_estimate": True,
            "category": str(vrows[-1].get("category") or "other"),
            "currency": vrows[-1].get("currency", "INR"),
        })
    out.sort(key=lambda r: -r["median_amount"])
    return out


# ── overspending ─────────────────────────────────────────────────────────────

def overspend_report(status: dict) -> list[str]:
    """Short honest callouts from a budget_status result."""
    callouts = []
    overall = status.get("overall") or {}
    if overall.get("label") == "over":
        callouts.append(f"Overall budget exceeded: "
                        f"{overall['spent']:.2f} of {overall['limit']:.2f} "
                        f"({overall['pct']:.0f}%).")
    elif overall.get("pace") and overall["pace"] > PACE_WARN:
        callouts.append(f"Overall spending is {overall['pace']:.1f}× the "
                        f"month-to-date pace.")
    for c in status.get("categories") or []:
        if c.get("label") == "over":
            callouts.append(f"'{c['category']}' is over budget: "
                            f"{c['spent']:.2f} of {c['limit']:.2f}.")
        elif c.get("label") == "ahead":
            callouts.append(f"'{c['category']}' is ahead of pace "
                            f"({c.get('pace', 0):.1f}× the month-to-date "
                            f"limit share).")
    return callouts


# ── summaries ────────────────────────────────────────────────────────────────

def monthly_summary(rows: list[dict], budgets: dict, month: str,
                    user_categories: list[dict] | None = None,
                    today: Optional[date] = None) -> str:
    totals = monthly_totals(rows, month, user_categories)
    status = budget_status(rows, budgets, month, today, user_categories)
    recurring = find_recurring(rows, today=today)
    lines = [f"Finance summary — {month}: {totals['count']} expense(s), "
             f"total {totals['total']:.2f}", ""]
    if totals["totals"]:
        lines.append("By category:")
        for cat, amt in sorted(totals["totals"].items(),
                               key=lambda kv: -kv[1]):
            share = amt / totals["total"] * 100 if totals["total"] else 0
            lines.append(f"  • {cat}: {amt:.2f} ({share:.0f}%)")
    else:
        lines.append("No expenses recorded for this month.")
    lines.append("")
    over = overspend_report(status)
    lines += (over if over else ["Budget: nothing over limit and no "
                                 "category is ahead of pace."])
    if recurring:
        lines.append("")
        lines.append(f"Recurring candidates ({len(recurring)}):")
        for r in recurring[:8]:
            lines.append(f"  • {r['vendor']} — ~{r['median_amount']:.2f} "
                         f"{r['currency']} every ~{r['interval_days']} days "
                         f"(next due ~{r['next_due']}, estimate)")
    lines.append("")
    lines.append("Reports only — no investment or financial recommendations.")
    return "\n".join(lines)
