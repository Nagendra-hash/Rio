"""
orchestrator/agents/dailylife/meal_logic.py — meals, pantry, grocery, budget.

Deterministic, offline, honest:

  * the weekly menu is built from a small BUILT-IN template library,
    filtered by the user's diet/allergies, rotated across weekdays, and
    pushed around what the pantry already holds;
  * items expiring soon are deliberately cooked first (use-up flags);
  * the grocery list is menu ingredients MINUS pantry availability;
  * budget awareness uses the user's OWN saved price hints only — it never
    quotes a live price, because no live price source was queried.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

# Menu slots per day, in serving order.
MEAL_SLOTS = ("breakfast", "lunch", "dinner")
EXPIRY_SOON_DAYS = 2
GROCERY_CAP = 60

# ── the built-in template library (small, editable, deterministic) ───────────
# ingredients: {item: qty_string_per_serving}; diet tags: veg / vegan / any.
RECIPES: tuple[dict, ...] = (
    {"name": "Poha", "slot": "breakfast", "diets": ("veg", "vegan"),
     "allergens": (), "minutes": 20,
     "ingredients": {"poha": "200 g", "onion": "1", "lemon": "1",
                     "peanuts": "30 g"}},
    {"name": "Oats bowl", "slot": "breakfast", "diets": ("veg",),
     "allergens": ("dairy",), "minutes": 10,
     "ingredients": {"oats": "80 g", "milk": "250 ml", "banana": "1"}},
    {"name": "Besan chilla", "slot": "breakfast", "diets": ("veg", "vegan"),
     "allergens": (), "minutes": 25,
     "ingredients": {"besan": "120 g", "onion": "1", "tomato": "1"}},
    {"name": "Dal + rice", "slot": "lunch", "diets": ("veg",),
     "allergens": ("dairy",), "minutes": 40,
     "ingredients": {"toor dal": "150 g", "rice": "200 g",
                     "ghee": "10 g", "tomato": "1"}},
    {"name": "Rajma chawal", "slot": "lunch", "diets": ("veg",),
     "allergens": ("dairy",), "minutes": 50,
     "ingredients": {"rajma": "150 g", "rice": "200 g", "onion": "1",
                     "tomato": "2"}},
    {"name": "Veg pulao", "slot": "lunch", "diets": ("veg",),
     "allergens": ("dairy",), "minutes": 45,
     "ingredients": {"rice": "200 g", "mixed vegetables": "200 g",
                     "onion": "1", "ghee": "15 g"}},
    {"name": "Paneer bhurji + roti", "slot": "lunch", "diets": ("veg",),
     "allergens": ("dairy", "gluten"), "minutes": 30,
     "ingredients": {"paneer": "150 g", "roti": "3", "onion": "1",
                     "tomato": "1"}},
    {"name": "Chicken curry + rice", "slot": "dinner", "diets": ("any",),
     "allergens": ("dairy",), "minutes": 55,
     "ingredients": {"chicken": "300 g", "rice": "200 g", "onion": "2",
                     "tomato": "2", "curry masala": "20 g"}},
    {"name": "Veg khichdi", "slot": "dinner", "diets": ("veg",),
     "allergens": ("dairy",), "minutes": 35,
     "ingredients": {"rice": "150 g", "moong dal": "100 g",
                     "mixed vegetables": "150 g", "ghee": "10 g"}},
    {"name": "Pasta in tomato sauce", "slot": "dinner",
     "diets": ("veg", "vegan"), "allergens": ("gluten",), "minutes": 30,
     "ingredients": {"pasta": "200 g", "tomato": "3", "garlic": "3 cloves",
                     "olive oil": "20 ml"}},
    {"name": "Egg fried rice", "slot": "dinner", "diets": ("any",),
     "allergens": ("egg", "soy"), "minutes": 25,
     "ingredients": {"rice": "200 g", "eggs": "2", "carrot": "1",
                     "soy sauce": "15 ml"}},
    {"name": "Vegetable soup + toast", "slot": "dinner",
     "diets": ("veg",), "allergens": ("gluten",), "minutes": 25,
     "ingredients": {"mixed vegetables": "250 g", "bread": "4 slices",
                     "butter": "15 g"}},
)


# ── pantry / expiry math ─────────────────────────────────────────────────────

def _parse_day(value: str) -> Optional[date]:
    try:
        return datetime.strptime(str(value or "").strip()[:10],
                                 "%Y-%m-%d").date()
    except ValueError:
        return None


def expiry_report(pantry: list[dict],
                  today: Optional[date] = None) -> dict:
    """Split the pantry into expired / expiring-soon / fine rows."""
    today = today or date.today()
    expired, soon, fine, unknown = [], [], [], []
    for row in pantry:
        exp = _parse_day(row.get("expires", ""))
        if exp is None:
            unknown.append(row)
        elif exp < today:
            expired.append(row)
        elif exp <= today + timedelta(days=EXPIRY_SOON_DAYS):
            soon.append(row)
        else:
            fine.append(row)
    soon.sort(key=lambda r: _parse_day(r.get("expires", "")) or today)
    return {"expired": expired, "soon": soon, "fine": fine,
            "no_date": unknown}


def _allergy_ok(recipe: dict, allergies: tuple | list) -> bool:
    bad = {str(a).strip().lower() for a in (allergies or []) if str(a).strip()}
    return not (bad & set(recipe.get("allergens") or ()))


def _candidates(slot: str, diet: str, allergies: tuple | list) -> list[dict]:
    """Recipes for one slot that satisfy the diet and avoid the allergens.

    `diets` on a recipe lists which preferences it SATISFIES: "veg" in the
    tags means a vegetarian can eat it; "any"-only tags mean only an
    unrestricted diet can.
    """
    d = str(diet or "any").strip().lower()
    if d == "vegan":
        pool = [r for r in RECIPES if "vegan" in r["diets"]]
    elif d == "veg":
        pool = [r for r in RECIPES if "veg" in r["diets"]]
    else:
        pool = list(RECIPES)
    return [r for r in pool if r["slot"] == slot
            and _allergy_ok(r, allergies)]


def _pantry_names(pantry: list[dict]) -> set[str]:
    return {str(r.get("name", "")).strip().lower() for r in pantry or []
            if str(r.get("name", "")).strip()}


# ── the weekly menu ──────────────────────────────────────────────────────────

def build_menu(prefs: dict, pantry: list[dict], start: Optional[date] = None,
               overrides: Optional[dict] = None,
               today: Optional[date] = None) -> dict:
    """Deterministic 7-day menu.

    prefs: {diet, allergies, household}   pantry: store rows
    overrides: {"YYYY-MM-DD" | "0".."6": {slot: recipe_name}}
    """
    start = start or date.today()
    today = today or date.today()
    diet = str((prefs or {}).get("diet") or "any")
    allergies = (prefs or {}).get("allergies") or ()
    household = max(1, int((prefs or {}).get("household") or 1))

    exp = expiry_report(pantry, today)
    use_up_names = [str(r.get("name", "")).lower()
                    for r in (exp["expired"] + exp["soon"])]
    in_pantry = _pantry_names(pantry)
    pools = {slot: _candidates(slot, diet, allergies)
             for slot in MEAL_SLOTS}

    days = []
    for offset in range(7):
        day = start + timedelta(days=offset)
        key = day.isoformat()
        meals = {}
        for slot_i, slot in enumerate(MEAL_SLOTS):
            pool = pools[slot]
            if not pool:
                meals[slot] = {
                    "name": f"(no {diet} {slot} recipe fits)",
                    "minutes": 0, "ingredients": {},
                    "use_up": [], "pantry_hits": []}
                continue
            recipe = dict(pool[(offset + slot_i) % len(pool)])
            ov = (overrides or {}).get(key) or (overrides or {}).get(str(offset))
            if isinstance(ov, dict) and ov.get(slot):
                want = str(ov[slot]).strip().lower()
                match = next((r for r in RECIPES
                              if r["name"].lower() == want), None)
                if match:
                    recipe = dict(match)
            ings = recipe.get("ingredients") or {}
            hits = sorted(n for n in ings if n.lower() in in_pantry)
            ups = sorted(n for n in ings if n.lower() in use_up_names)
            meals[slot] = {"name": recipe["name"],
                           "minutes": recipe.get("minutes", 0),
                           "ingredients": {k: f"{v}"
                                           for k, v in ings.items()},
                           "pantry_hits": hits, "use_up": ups}
        days.append({"date": key, "weekday": day.strftime("%a"),
                     "meals": meals})
    return {"start": start.isoformat(), "household": household,
            "diet": diet, "days": days,
            "use_up_first": use_up_names}


def render_menu(menu: dict) -> str:
    days = menu.get("days") or []
    if not days:
        return "No menu built yet."
    lines = [f"Weekly menu (from {menu.get('start', '?')}, "
             f"diet: {menu.get('diet', 'any')}, "
             f"household: {menu.get('household', 1)}):", ""]
    for day in days:
        lines.append(f"{day.get('weekday', '')} {day.get('date', '')}")
        for slot in MEAL_SLOTS:
            meal = (day.get("meals") or {}).get(slot) or {}
            flags = ""
            if meal.get("use_up"):
                flags += f"  → use up: {', '.join(meal['use_up'])}"
            if meal.get("pantry_hits"):
                flags += f"  (in pantry: {', '.join(meal['pantry_hits'])})"
            lines.append(f"  {slot:9s} {meal.get('name', '?')}"
                         f"  [{meal.get('minutes', 0)} min]{flags}")
        lines.append("")
    if menu.get("use_up_first"):
        lines.append("Expiring first (cook these early): "
                     + ", ".join(menu["use_up_first"]))
    return "\n".join(lines).strip()


# ── grocery list + budget ────────────────────────────────────────────────────

def _parse_qty(text: str) -> tuple[float, str]:
    """'200 g' → (200.0, 'g'); '1' → (1.0, ''); unparseable → (1.0, '')."""
    parts = str(text or "").strip().split(None, 1)
    try:
        amount = float(parts[0])
    except (ValueError, IndexError):
        return 1.0, ""
    unit = (parts[1] if len(parts) > 1 else "").strip()
    return amount, unit


def grocery_list(menu: dict, pantry: list[dict],
                 prices: Optional[dict] = None,
                 budget: Optional[float] = None) -> dict:
    """Menu ingredients MINUS what the pantry already holds.

    Prices and budget are the user's OWN saved hints; the estimate is
    labeled as such and never presented as a live price.
    """
    prices = {str(k).strip().lower(): float(v) for k, v in (prices or {}).items()
              if str(k).strip()}
    in_pantry = _pantry_names(pantry)
    need: dict[str, dict] = {}
    for day in menu.get("days") or []:
        for slot in MEAL_SLOTS:
            meal = (day.get("meals") or {}).get(slot) or {}
            for item, qty_text in (meal.get("ingredients") or {}).items():
                key = str(item).strip()
                low = key.lower()
                amount, unit = _parse_qty(qty_text)
                row = need.setdefault(low, {"name": key, "amount": 0.0,
                                            "unit": unit})
                row["amount"] += amount
                if unit and not row["unit"]:
                    row["unit"] = unit
    items = []
    for low in sorted(need):
        row = need[low]
        amount = round(row["amount"], 2)
        qty = f"{amount:g} {row['unit']}".strip()
        items.append({"name": row["name"], "qty": qty,
                      "in_pantry": low in in_pantry,
                      "est_price": prices.get(low)})
    buy = [i for i in items if not i["in_pantry"]][:GROCERY_CAP]
    est_cost = sum(i["est_price"] for i in buy
                   if isinstance(i.get("est_price"), (int, float)))
    over = None
    if budget is not None and budget > 0:
        over = est_cost > float(budget)
    return {"items": items, "buy": buy,
            "est_cost": round(est_cost, 2),
            "budget": budget, "over_budget": over,
            "priced_items": sum(1 for i in buy
                                if isinstance(i.get("est_price"),
                                              (int, float)))}


def render_grocery(grocery: dict) -> str:
    buy = grocery.get("buy") or []
    in_pantry = [i for i in (grocery.get("items") or []) if i.get("in_pantry")]
    lines = [f"Grocery list ({len(buy)} item(s) to buy):", ""]
    if buy:
        lines += [f"  • {i['name']} — {i['qty']}" for i in buy]
    else:
        lines.append("  Nothing to buy — the pantry covers this menu (check "
                     "stock levels).")
    if in_pantry:
        lines += ["", "Already in pantry (check stock):",
                  *[f"  • {i['name']} — {i['qty']}" for i in in_pantry]]
    est = grocery.get("est_cost")
    budget = grocery.get("budget")
    lines.append("")
    if est is not None and grocery.get("priced_items"):
        note = (f"Estimated cost: {est:g} "
                f"(from your saved price hints on "
                f"{grocery.get('priced_items')} of {len(buy)} items — "
                f"no live prices queried).")
        lines.append(note)
        if budget is not None and budget > 0:
            verdict = "OVER budget" if grocery.get("over_budget") \
                else "within budget"
            lines.append(f"Budget: {budget:g} — this plan is {verdict}.")
    elif budget is not None and budget > 0:
        lines.append(f"Budget: {budget:g} — add price hints (prefs_set) for "
                     "a cost estimate. No live prices were queried.")
    else:
        lines.append("No price hints set — no cost estimate (no live prices "
                     "queried).")
    return "\n".join(lines).strip()


def render_expiry(report: dict) -> str:
    expired, soon = report.get("expired") or [], report.get("soon") or []
    if not expired and not soon:
        return "Nothing expired or expiring in the next " \
               f"{EXPIRY_SOON_DAYS} days."
    lines = []
    if expired:
        lines.append("EXPIRED (use or toss):")
        lines += [f"  • {r.get('name')} — was {r.get('qty')} "
                  f"{r.get('unit', '')} (expired {r.get('expires', '?')})"
                  .rstrip() for r in expired]
    if soon:
        lines.append(f"Expiring within {EXPIRY_SOON_DAYS} days:")
        lines += [f"  • {r.get('name')} — {r.get('qty')} "
                  f"{r.get('unit', '')} (expires {r.get('expires', '?')})"
                  .rstrip() for r in soon]
    nodate = report.get("no_date") or []
    if nodate:
        lines.append("No expiry date on: "
                     + ", ".join(str(r.get("name")) for r in nodate))
    return "\n".join(lines)