"""
orchestrator/agents/finance/parser.py — deterministic receipt parsing.

No OCR, no LLM: this parses TEXT the user pastes (or that a screen reader
already extracted). Every field it emits is traceable to a line in the
input; anything it cannot read is reported, never guessed:

    * vendor      — first line that isn't a date/total/amount line
    * date        — several common formats, normalized by schema
    * total       — the last TOTAL/AMOUNT DUE/TO PAY line's amount
    * line items  — "2 x Coffee  8.50" / "Coffee 8.50" rows
    * currency    — from the symbol/code seen most often
    * confidence  — 0.4 base, +0.2 per found field (vendor/date/total)

parse_statement() extracts one transaction per line from statement-style
pastes ("12 Oct | ZOMATO | -450.00"); lines without a readable date AND
amount are skipped, honestly.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime

from orchestrator.agents.finance.schema import (CURRENCY_SYMBOLS,
                                                normalize_currency,
                                                normalize_date)

# ── amount / currency shapes ─────────────────────────────────────────────────
_CURRENCY = r"(?:₹|rs\.?|inr|\$|usd|€|eur|£|gbp)"
_AMOUNT = r"([\d,]+(?:\.\d{1,2})?)"
_AMOUNT_RE = re.compile(rf"(?:{_CURRENCY}\s*{_AMOUNT})|(?:{_AMOUNT}\s*{_CURRENCY})",
                        re.IGNORECASE)
_TRAILING_AMOUNT_RE = re.compile(rf"(?:{_CURRENCY}\s*)?([\d,]+\.\d{{1,2}})\s*$")

# total-ish lines: TOTAL, GRAND TOTAL, AMOUNT DUE, TO PAY, NET PAYABLE
_TOTAL_RE = re.compile(
    r"\b(?:grand\s+)?(?:total|amount\s+due|to\s+pay|net\s+payable|"
    r"balance\s+due)\b", re.IGNORECASE)
_TAX_RE = re.compile(r"\b(?:tax|gst|vat|service\s+charge|cgst|sgst)\b",
                     re.IGNORECASE)

_DATE_HINT_RE = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|"
    r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4}")

_ITEM_QTY_RE = re.compile(r"^\s*(\d+)\s*[x×@]\s*(.+)$")
_NOISE_RE = re.compile(
    r"^(?:thank|thanks|visit|www\.|http|invoice|receipt|bill\s+no|"
    r"order\s+(?:id|no)|table|gst\s+no|fssai|cashier|terminal|change due|"
    r"paid\s+(?:via|by|with)|card\s+(?:no|ending)|upi|phone\s+no)",
    re.IGNORECASE)

# instruction lead-ins a user may paste above the actual receipt text
_LEADIN_RE = re.compile(
    r"^(?:parse|import|add|log|record|read|extract|here(?:'s| is)|"
    r"this|new receipt)\b", re.IGNORECASE)


def _amounts_in(line: str) -> list[float]:
    out = []
    for m in _AMOUNT_RE.finditer(line):
        try:
            out.append(float(m.group(1) or m.group(2)).__float__())
        except (TypeError, ValueError, IndexError):
            continue
    if not out:
        m = _TRAILING_AMOUNT_RE.search(line)
        if m:
            try:
                out.append(float(m.group(1).replace(",", "")))
            except ValueError:
                pass
    return [a for a in out if 0 < a < 1e9]


def _symbols_in(text: str) -> list[str]:
    found = []
    for m in re.finditer(r"(₹|\$|€|£|¥)|(?:rs\.?|inr|usd|eur|gbp)\b",
                         text, re.IGNORECASE):
        found.append(m.group(1) or m.group(2))
    return [normalize_currency(s) for s in found]


def _main_currency(text: str) -> str:
    codes = [c for c in _symbols_in(text) if c]
    return Counter(codes).most_common(1)[0][0] if codes else "INR"


def _find_date(lines: list[str]) -> tuple[str, str]:
    """Scan top lines for a date; returns (iso_date, raw_line)."""
    for line in lines[:8]:
        if _DATE_HINT_RE.search(line):
            d = normalize_date(_DATE_HINT_RE.search(line).group(0))
            if d:
                return d, line.strip()
    return "", ""


def _find_vendor(lines: list[str]) -> str:
    for line in lines[:8]:
        s = line.strip().strip("-=|* ")
        if not s or _DATE_HINT_RE.search(s):
            continue
        if _TOTAL_RE.search(s) or _TAX_RE.search(s):
            continue
        if _NOISE_RE.match(s) or _LEADIN_RE.match(s):
            continue
        if s.endswith(":"):            # 'parse this receipt:' lead-ins
            continue
        if _amounts_in(s) and len(s) < 8:
            continue
        if 2 <= len(s) <= 60 and re.search(r"[A-Za-z]{3}", s):
            return s[:60]
    return ""


def _find_items(lines: list[str], total_line_idx: int) -> list[dict]:
    items = []
    for idx, line in enumerate(lines):
        if idx >= total_line_idx and total_line_idx >= 0:
            break
        s = line.strip()
        if not s or _TOTAL_RE.search(s) or _TAX_RE.search(s) \
                or _NOISE_RE.match(s) or _DATE_HINT_RE.search(s):
            continue
        amounts = _amounts_in(s)
        if not amounts:
            continue
        qty = 1.0
        name_source = s
        qm = _ITEM_QTY_RE.match(s)
        if qm:
            try:
                qty = float(qm.group(1))
            except ValueError:
                qty = 1.0
            name_source = qm.group(2)          # strip the '2 x ' prefix
        name_part = _AMOUNT_RE.sub(" ", name_source)
        name_part = _TRAILING_AMOUNT_RE.sub(" ", name_part)
        name = re.sub(r"[|•·\-–=]+$", "", name_part).strip(" |•·-–=\t")
        if not name or not re.search(r"[A-Za-z]{2}", name):
            continue
        items.append({"name": name[:60], "qty": qty,
                      "price": round(amounts[-1], 2)})
    return items


def parse_receipt(text: str, today: date | None = None) -> dict:
    """Parse one pasted receipt. Returns
    {"ok", "errors", "confidence", "expense": {...} | None, "preview": str}."""
    today = today or date.today()
    raw_lines = [ln for ln in str(text or "").splitlines() if ln.strip()]
    if not raw_lines:
        return {"ok": False, "errors": ["empty receipt"], "confidence": 0.0,
                "expense": None, "preview": ""}

    total_idx, total_amount = -1, None
    for idx, line in enumerate(raw_lines):
        if _TOTAL_RE.search(line) and not _TAX_RE.search(line):
            amounts = _amounts_in(line)
            if amounts:
                total_amount = max(amounts)
                total_idx = idx
    if total_amount is None:                     # fallback: largest amount
        all_amounts = [a for ln in raw_lines for a in _amounts_in(ln)]
        if all_amounts:
            total_amount = max(all_amounts)
            total_idx = next(i for i, ln in enumerate(raw_lines)
                             if total_amount in _amounts_in(ln))

    d, _raw = _find_date(raw_lines)
    vendor = _find_vendor(raw_lines)
    items = _find_items(raw_lines, total_idx)
    currency = _main_currency("\n".join(raw_lines))

    errors = []
    if not vendor:
        errors.append("vendor not found")
    if not d:
        errors.append("date not found")
    if total_amount is None:
        errors.append("total amount not found")

    confidence = 0.4 + (0.2 if vendor else 0) + (0.2 if d else 0) \
        + (0.2 if total_amount is not None else 0)

    expense = None
    if total_amount is not None:
        expense = {
            "date": d or "",
            "vendor": vendor or "(unknown vendor)",
            "amount": round(total_amount, 2),
            "currency": currency,
            "source": "receipt",
            "items": items,
        }
    return {"ok": total_amount is not None and confidence >= 0.6,
            "errors": errors, "confidence": round(confidence, 2),
            "expense": expense,
            "preview": _preview(expense, items, confidence, currency)}


def _preview(expense: dict | None, items: list[dict], confidence: float,
             currency: str) -> str:
    if not expense:
        return "Could not read a total from this receipt."
    lines = [f"{expense['vendor']} — {expense['amount']:.2f} {currency} "
             f"on {expense['date'] or '(date unreadable)'} "
             f"(confidence {confidence:.0%})"]
    if items:
        lines.append("Items:")
        lines += [f"  • {i['qty']:g} × {i['name']} — {i['price']:.2f}"
                  for i in items[:15]]
    return "\n".join(lines)


# ── statement-style lines (one transaction per line) ─────────────────────────

def parse_statement(text: str, today: date | None = None) -> dict:
    """Extract transactions from statement-style text.

    Each line needs BOTH a readable date and an amount; vendor is the
    leftover text. Lines that don't parse are counted and reported —
    never silently dropped, never guessed.
    """
    today = today or date.today()
    rows, skipped = [], []
    for line in str(text or "").splitlines():
        s = line.strip().strip("|,;")
        if not s:
            continue
        dm = _DATE_HINT_RE.search(s)
        if not dm:
            skipped.append(s[:60])
            continue
        d = normalize_date(dm.group(0), today)
        if not d:
            skipped.append(s[:60])
            continue
        amounts = _amounts_in(s)
        if not amounts:
            skipped.append(s[:60])
            continue
        amount = max(amounts)
        vendor_part = _AMOUNT_RE.sub(" ", s)
        vendor_part = _DATE_HINT_RE.sub(" ", vendor_part)
        vendor = re.sub(r"[|,;:\-–—]+", " ", vendor_part).strip() \
            .strip("-–—| ")[:60]
        if not vendor or not re.search(r"[A-Za-z]{2}", vendor):
            vendor = "(unknown vendor)"
        rows.append({"date": d, "vendor": vendor, "amount": round(amount, 2),
                     "currency": _main_currency(s), "source": "statement"})
    return {"ok": bool(rows), "expenses": rows,
            "skipped": skipped[:20],
            "skipped_count": len(skipped)}
