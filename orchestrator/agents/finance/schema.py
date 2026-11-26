"""
orchestrator/agents/finance/schema.py — the expense record contract.

One shape for every expense in the vault, whether it came from a parsed
receipt, a statement line, or a manual entry. Validation is deterministic
and strict at the edges (types, ranges, dates) so the engine never has to
defend against a malformed row mid-analysis.

Amounts are stored as positive floats in the record's own currency —
no FX conversion is ever invented here.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Optional

MAX_VENDORS = 80
MAX_NOTE_CHARS = 300
MAX_ITEMS = 50

CURRENCY_SYMBOLS = {"₹": "INR", "rs": "INR", "inr": "INR", "$": "USD",
                    "usd": "USD", "€": "EUR", "£": "GBP", "gbp": "GBP",
                    "¥": "JPY"}
KNOWN_CURRENCIES = set(CURRENCY_SYMBOLS.values())

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _num(value) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def normalize_date(value, today=None) -> str:
    """Anything date-shaped → 'YYYY-MM-DD', else ''. Two-digit years are
    20xx; ambiguous DD/MM vs MM/DD resolves as DD/MM (house convention,
    documented — an unambiguous ISO date always wins)."""
    today = today or date.today()
    raw = str(value or "").strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y",
                "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})$", raw)
    if m:
        d_, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        if 1 <= mo <= 12 and 1 <= d_ <= 31:
            try:
                return date(y, mo, d_).isoformat()
            except ValueError:
                return ""
    m = re.match(r"^(\d{1,2})[/.-](\d{1,2})$", raw)   # no year → this year
    if m:
        d_, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d_ <= 31:
            try:
                return date(today.year, mo, d_).isoformat()
            except ValueError:
                return ""
    return ""


def normalize_currency(value) -> str:
    """Symbol/code → ISO code; unknown → '' (the caller decides whether
    that's an error or a default)."""
    s = str(value or "").strip()
    if not s:
        return ""
    if s in CURRENCY_SYMBOLS:                 # symbols: ₹ $ € £ ¥
        return CURRENCY_SYMBOLS[s]
    code = re.sub(r"[^a-z]", "", s.lower())    # 'Rs.' → 'rs', 'US D' → 'usd'
    if code in CURRENCY_SYMBOLS:
        return CURRENCY_SYMBOLS[code]
    up = code.upper()
    return up if up in KNOWN_CURRENCIES else ""


def new_expense_id() -> str:
    return f"exp-{uuid.uuid4().hex[:10]}"


def validate_expense(row, today=None) -> tuple[Optional[dict], list[str]]:
    """Validate one candidate expense. Returns (clean_row, errors).

    clean_row is None when the row is unusable; otherwise it carries the
    normalized fields plus an id and imported_at. Amounts must be > 0
    (records are money OUT; refunds are recorded with a note, not a
    negative amount — keeping the engine simple and honest).
    """
    today = today or date.today()
    errors: list[str] = []
    if not isinstance(row, dict):
        return None, ["row is not a record"]
    out = dict(row)
    d = normalize_date(out.get("date"), today)
    if not d:
        errors.append("missing or unreadable date (YYYY-MM-DD)")
    amount = _num(out.get("amount"))
    if amount is None or amount <= 0 or amount > 1e9:
        errors.append("amount must be a positive number")
    vendor = str(out.get("vendor") or "").strip()[:MAX_VENDORS]
    if not vendor:
        errors.append("missing vendor")
    currency_raw = str(out.get("currency") or "").strip()
    if currency_raw:
        currency = normalize_currency(currency_raw)
        if currency not in KNOWN_CURRENCIES:
            errors.append(f"unknown currency {currency_raw!r}")
    else:
        currency = "INR"                      # house default, documented
    if errors:
        return None, errors
    out.update({"id": str(out.get("id") or new_expense_id()),
                "date": d,
                "amount": round(amount, 2),
                "vendor": vendor,
                "currency": currency,
                "category": str(out.get("category") or "").strip().lower()
                [:30] or "other",
                "note": str(out.get("note") or "").strip()[:MAX_NOTE_CHARS],
                "source": str(out.get("source") or "manual")[:12],
                "imported_at": out.get("imported_at")
                or datetime.now().astimezone().isoformat(timespec="seconds")})
    items = out.get("items")
    if isinstance(items, list) and items:
        clean_items = []
        for it in items[:MAX_ITEMS]:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "").strip()[:60]
            price = _num(it.get("price"))
            qty = _num(it.get("qty")) or 1.0
            if name and price is not None and price >= 0:
                clean_items.append({"name": name, "qty": round(qty, 3),
                                    "price": round(price, 2)})
        out["items"] = clean_items
    else:
        out.pop("items", None)
    return out, []


def dedupe_key(row: dict) -> tuple:
    """What makes two records 'the same purchase' for import dedupe:
    same date + vendor + amount + currency."""
    return (row.get("date", ""), str(row.get("vendor", "")).lower(),
            round(float(row.get("amount") or 0), 2),
            row.get("currency", ""))


# ── categories (built-ins; the user's own list overrides these) ──────────────

DEFAULT_CATEGORIES: list[dict] = [
    {"name": "food", "keywords": ["restaurant", "cafe", "coffee", "zomato",
                                  "swiggy", "pizza", "bakery", "dhaba",
                                  "grocery", "supermarket", "kirana"]},
    {"name": "transport", "keywords": ["uber", "ola", "metro", "petrol",
                                       "fuel", "taxi", "bus", "train",
                                       "parking", "toll"]},
    {"name": "utilities", "keywords": ["electricity", "water bill", "gas",
                                       "broadband", "wifi", "mobile bill",
                                       "recharge"]},
    {"name": "rent", "keywords": ["rent", "landlord", "maintenance bill"]},
    {"name": "shopping", "keywords": ["amazon", "flipkart", "myntra",
                                      "shop", "store", "mall"]},
    {"name": "health", "keywords": ["pharmacy", "hospital", "clinic",
                                    "doctor", "medical", "lab test"]},
    {"name": "entertainment", "keywords": ["netflix", "spotify", "cinema",
                                           "movie", "game", "book my show",
                                           "prime video"]},
    {"name": "subscriptions", "keywords": ["subscription", "membership",
                                           "saas", "monthly plan"]},
    {"name": "other", "keywords": []},
]
CATEGORY_NAMES = [c["name"] for c in DEFAULT_CATEGORIES]
