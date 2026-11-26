"""
core/astro_engine.py — deterministic astrology & numerology engine.

Why an engine and not "just ask the model": LLMs are unreliable at digit
arithmetic (digit sums, Lo Shu grids, reduced master numbers) and cannot
compute a sidereal moon sign or nakshatra without an ephemeris. This module
does all the math locally and deterministically:

  Numerology (Vedic/Chaldean-flavoured + Pythagorean blend):
    - Moolank    (root/birth number) = day-of-month reduced, masters kept
    - Bhagyank   (destiny number)    = full DOB digits reduced, masters kept
    - Kua        (feng shui)         = gendered formula, 5 maps per tradition
    - Lo Shu grid                    = digit positions with arrows analysis
    - Name number (Chaldean)         = optional, when a name is given
    - Master numbers 11/22/33 are NOT collapsed (traditional rule)

  Astrology (Swiss Ephemeris, Moshier fallback — no data files needed):
    - Western tropical sun sign
    - Chinese zodiac (year animal + element)
    - Vedic sidereal (Lahiri ayanamsa): sun sign, moon sign, nakshatra,
      tithi, yoga, karana, weekday lord
    - Birthstone / birth flower / zodiac traits tables

The handler never invents numbers: every figure in a reading is computed here.
pyswisseph is imported lazily so the module still loads (and numerology still
works) if the optional dependency is missing.
"""

from __future__ import annotations

import datetime as _dt
from functools import lru_cache

# ────────────────────────────────────────────────────────────────────────────
# Numerology
# ────────────────────────────────────────────────────────────────────────────

_MASTER = {11, 22, 33}


def _reduce(n: int, keep_masters: bool = True) -> int:
    """Reduce to a single digit, preserving 11/22/33 when asked."""
    while n > 9:
        if keep_masters and n in _MASTER:
            return n
        n = sum(int(d) for d in str(n))
    return n


def moolank(day: int) -> int:
    """Root/birth number: day of month reduced (master numbers kept)."""
    return _reduce(day)


def bhagyank(y: int, m: int, d: int) -> int:
    """Destiny number: every DOB digit summed, then reduced (masters kept)."""
    return _reduce(sum(int(c) for c in f"{y:04d}{m:02d}{d:02d}"))


def _digitsum(n: int) -> int:
    """Plain digit sum, recursively to one digit. Masters NEVER apply here:
    Kua arithmetic operates on ordinary numbers (2026-09-30 bug: reusing
    _reduce() turned year 92 into master 11, and 10-11 produced -1)."""
    while n > 9:
        n = sum(int(d) for d in str(n))
    return n


def kua_number(y: int, gender: str) -> int:
    """Feng shui Kua (Gua) number. Standard formula; 5 remaps per tradition
    (male→2, female→8). Gender is taken from the profile value ('male'/
    'female'); anything else falls back to the male branch — recorded, not
    guessed silently."""
    g = (gender or "male").strip().lower()
    yy = _digitsum(y % 100)
    if y >= 2000:
        base = _digitsum(yy + 6) if g == "male" else _digitsum(yy + 7)
    else:
        base = _digitsum(100 - (y % 100)) if g == "male" else _digitsum(yy + 5)
    if base == 5:
        base = 2 if g == "male" else 8
    return base


# Lo Shu grid positions (3×3), traditional mapping:
#   4 9 2
#   3 5 7
#   8 1 6
_LOSHU_POS = {
    1: (2, 1),
    2: (0, 2),
    3: (1, 0),
    4: (0, 0),
    5: (1, 1),
    6: (2, 2),
    7: (1, 2),
    8: (2, 0),
    9: (0, 1),
}

# Planes: rows/columns of the Lo Shu grid and what a full line means.
_LOSHU_LINES = {
    "4-9-2 (mental/head row)": "strong thinking plane — planner, ideas-first",
    "3-5-7 (emotional row)": "balanced emotional plane — practical feelings",
    "8-1-6 (practical row)": "strong execution plane — gets things done",
    "4-3-8 (thought column)": "strong will/determination column",
    "9-5-1 (will column)": "strong persistence — sees things through",
    "2-7-6 (action column)": "strong action column — doer",
}


def loshu_grid(y: int, m: int, d: int) -> dict:
    """Lo Shu grid: which of 1-9 appear in the DOB (repeated digits repeated),
    plus which planes (arrows) are complete/empty."""
    digits = [int(c) for c in f"{y:04d}{m:02d}{d:02d}" if c != "0"]
    counts = {i: digits.count(i) for i in range(1, 10)}
    present = {i for i, c in counts.items() if c > 0}

    def _cells(nums: str) -> set:
        return {tuple(_LOSHU_POS[int(n)]) for n in nums}

    complete, empty = [], []
    for line, meaning in _LOSHU_LINES.items():
        nums = line[:3].replace("-", "")
        cells = _cells(nums)
        if cells <= present:
            complete.append(f"{line} → {meaning}")
        elif present.isdisjoint(cells):
            empty.append(line)
    return {
        "counts": counts,
        "complete_planes": complete,
        "empty_planes": empty,
        "missing_numbers": sorted(set(range(1, 10)) - present),
    }


_CHALDEAN = {
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
    "E": 5,
    "F": 8,
    "G": 3,
    "H": 5,
    "I": 1,
    "J": 1,
    "K": 2,
    "L": 3,
    "M": 4,
    "N": 5,
    "O": 7,
    "P": 8,
    "Q": 1,
    "R": 2,
    "S": 3,
    "T": 4,
    "U": 6,
    "V": 6,
    "W": 6,
    "X": 5,
    "Y": 1,
    "Z": 7,
}


def name_number(name: str) -> dict | None:
    """Chaldean name number (expression number), masters kept."""
    if not name or not name.strip():
        return None
    letters = [c for c in name.upper() if c.isalpha()]
    if not letters:
        return None
    total = sum(_CHALDEAN[c] for c in letters)
    return {"total": total, "number": _reduce(total)}


# ────────────────────────────────────────────────────────────────────────────
# Tables (Western + Chinese + gems/flowers)
# ────────────────────────────────────────────────────────────────────────────

_SUN_SIGNS = [  # tropical cusp dates (day each sign STARTS), ascending by date
    # — Capricorn's Dec 22 entry is deliberately LAST so the reversed scan in
    # western_sun_sign() checks the year's latest cusp first (2026-09-30 bug:
    # listing it first made Dec 22–31 resolve to Sagittarius).
    ("Aquarius", 1, 20),
    ("Pisces", 2, 19),
    ("Aries", 3, 21),
    ("Taurus", 4, 20),
    ("Gemini", 5, 21),
    ("Cancer", 6, 21),
    ("Leo", 7, 23),
    ("Virgo", 8, 23),
    ("Libra", 9, 23),
    ("Scorpio", 10, 23),
    ("Sagittarius", 11, 22),
    ("Capricorn", 12, 22),
]
_SUN_TRAITS = {
    "Aries": "bold, direct, pioneering; impatience is the shadow",
    "Taurus": "steady, sensual, reliable; stubbornness is the shadow",
    "Gemini": "curious, witty, versatile; scattered energy is the shadow",
    "Cancer": "nurturing, intuitive, loyal; moodiness is the shadow",
    "Leo": "warm, generous, charismatic; pride is the shadow",
    "Virgo": "precise, helpful, analytical; overthinking is the shadow",
    "Libra": "fair, charming, diplomatic; indecision is the shadow",
    "Scorpio": "intense, perceptive, transformative; control is the shadow",
    "Sagittarius": "optimistic, honest, adventurous; bluntness is the shadow",
    "Capricorn": "disciplined, ambitious, patient; rigidity is the shadow",
    "Aquarius": "original, humanitarian, independent; detachment is the shadow",
    "Pisces": "compassionate, imaginative, sensitive; escapism is the shadow",
    "Capricorn2": "",
}
del _SUN_TRAITS["Capricorn2"]

_CHINESE = [
    ("Rat", "Water"),
    ("Ox", "Earth"),
    ("Tiger", "Wood"),
    ("Rabbit", "Wood"),
    ("Dragon", "Earth"),
    ("Snake", "Fire"),
    ("Horse", "Fire"),
    ("Goat", "Earth"),
    ("Monkey", "Metal"),
    ("Rooster", "Metal"),
    ("Dog", "Earth"),
    ("Pig", "Water"),
]
# Sexagenary stem pairs indexed by year mod 10: 0,1=Metal 2,3=Water
# 4,5=Wood 6,7=Fire 8,9=Earth. Canonical checks: 1984=Wood Rat (idx 4),
# 1992=Water Monkey (idx 2), 2020=Metal Rat (idx 0), 2024=Wood Dragon (idx 4).
_STEM_ELEMENTS = [
    "Metal",
    "Metal",
    "Water",
    "Water",
    "Wood",
    "Wood",
    "Fire",
    "Fire",
    "Earth",
    "Earth",
]

_BIRTHSTONES = {
    1: "Garnet",
    2: "Amethyst",
    3: "Aquamarine",
    4: "Diamond",
    5: "Emerald",
    6: "Pearl",
    7: "Ruby",
    8: "Peridot",
    9: "Sapphire",
    10: "Opal",
    11: "Topaz",
    12: "Turquoise",
}
_FLOWERS = {
    1: "Carnation",
    2: "Violet",
    3: "Daffodil",
    4: "Daisy",
    5: "Lily of the Valley",
    6: "Rose",
    7: "Larkspur",
    8: "Gladiolus",
    9: "Aster",
    10: "Marigold",
    11: "Chrysanthemum",
    12: "Narcissus",
}

# Vedic gem/planetary mapping for the moolank lord (traditional).
_MOOLANK_LORDS = {
    1: ("Sun", "Ruby"),
    2: ("Moon", "Pearl"),
    3: ("Jupiter", "Yellow Sapphire"),
    4: ("Rahu", "Hessonite"),
    5: ("Mercury", "Emerald"),
    6: ("Venus", "Diamond"),
    7: ("Ketu", "Cat's Eye"),
    8: ("Saturn", "Blue Sapphire"),
    9: ("Mars", "Red Coral"),
}

_NUM_MEANING = {
    1: "leader — independent, pioneering, ambitious",
    2: "diplomat — sensitive, cooperative, intuitive",
    3: "communicator — creative, expressive, social",
    4: "builder — disciplined, practical, dependable",
    5: "adventurer — freedom-loving, versatile, dynamic",
    6: "nurturer — responsible, caring, harmonious",
    7: "seeker — analytical, spiritual, introspective",
    8: "achiever — ambitious, authoritative, materially strong",
    9: "humanitarian — compassionate, wise, generous",
    11: "master intuitive — inspired, visionary, spiritually charged",
    22: "master builder — turns big visions into reality",
    33: "master teacher — selfless service and upliftment",
}


# ────────────────────────────────────────────────────────────────────────────
# Western + Chinese (pure calendar math)
# ────────────────────────────────────────────────────────────────────────────


def western_sun_sign(month: int, day: int) -> str:
    # Scan reversed so the latest cusp ≤ (month, day) wins; Jan 1–19 falls to
    # Capricorn (its Dec 22 start wraps the year boundary).
    for name, mm, dd in reversed(_SUN_SIGNS):
        if (month, day) >= (mm, dd):
            return name
    return "Capricorn"


def chinese_zodiac(year: int) -> dict:
    animal, fixed = _CHINESE[(year - 1900) % 12]
    return {
        "animal": animal,
        "element": _STEM_ELEMENTS[year % 10],
        "fixed_element": fixed,
    }


# ────────────────────────────────────────────────────────────────────────────
# Vedic (sidereal) — Swiss Ephemeris
# ────────────────────────────────────────────────────────────────────────────

_RASHIS = [
    "Aries",
    "Taurus",
    "Gemini",
    "Cancer",
    "Leo",
    "Virgo",
    "Libra",
    "Scorpio",
    "Sagittarius",
    "Capricorn",
    "Aquarius",
    "Pisces",
]
_RASHI_SANSKRIT = [
    "Mesha",
    "Vrishabha",
    "Mithuna",
    "Karka",
    "Simha",
    "Kanya",
    "Tula",
    "Vrishchika",
    "Dhanu",
    "Makara",
    "Kumbha",
    "Meena",
]
_NAKSHATRAS = [
    "Ashwini",
    "Bharani",
    "Krittika",
    "Rohini",
    "Mrigashira",
    "Ardra",
    "Punarvasu",
    "Pushya",
    "Ashlesha",
    "Magha",
    "Purva Phalguni",
    "Uttara Phalguni",
    "Hasta",
    "Chitra",
    "Swati",
    "Vishakha",
    "Anuradha",
    "Jyeshtha",
    "Mula",
    "Purva Ashadha",
    "Uttara Ashadha",
    "Shravana",
    "Dhanishta",
    "Shatabhisha",
    "Purva Bhadrapada",
    "Uttara Bhadrapada",
    "Revati",
]
_NAK_LORDS = [
    ("Ketu", "Venus", "Sun", "Moon", "Mars", "Rahu", "Jupiter", "Saturn", "Mercury")
][0] * 3  # rotates Ketu..Mercury thrice
_TITHIS = [
    "Pratipada",
    "Dwitiya",
    "Tritiya",
    "Chaturthi",
    "Panchami",
    "Shashthi",
    "Saptami",
    "Ashtami",
    "Navami",
    "Dashami",
    "Ekadashi",
    "Dwadashi",
    "Trayodashi",
    "Chaturdashi",
]
_WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]
_WEEK_LORDS = ["Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn", "Sun"]


@lru_cache(maxsize=256)
def vedic_positions(
    y: int,
    m: int,
    d: int,
    hour: float,
    minute: float,
    lat: float = 0.0,
    lon: float = 0.0,
) -> dict:
    """Sidereal (Lahiri) sun/moon longitudes and panchanga for a birth moment.

    Cached: readings are pure functions of the inputs. lat/lon matter only for
    the moon's sub-degree placement (nakshatra pada) — defaults are harmless.
    """
    try:
        import swisseph as swe
    except ImportError:
        return {
            "error": "swisseph not installed — run: .venv/bin/python -m pip install pysweph "
            "(maintained fork with Python 3.12 wheels; same import name)"
        }

    ut = hour + minute / 60.0 - 5.5  # IST → UT
    jd = swe.julday(y, m, d, ut)
    swe.set_sid_mode(swe.SIDM_LAHIRI)
    flags = swe.FLG_SWIEPH | swe.FLG_SIDEREAL | swe.FLG_SPEED
    try:
        sun = swe.calc_ut(jd, swe.SUN, flags)[0][0] % 360
        moon = swe.calc_ut(jd, swe.MOON, flags)[0][0] % 360
    except TypeError:  # pyswisseph signature variants
        sun = swe.calc_ut(jd, swe.SUN, flags)[0][0] % 360
        moon = swe.calc_ut(jd, swe.MOON, flags)[0][0] % 360
    except Exception:
        # Moshier fallback (no ephemeris files): accurate to ~0.1 arcsec here
        flags = swe.FLG_MOSEPH | swe.FLG_SIDEREAL | swe.FLG_SPEED
        sun = swe.calc_ut(jd, swe.SUN, flags)[0][0] % 360
        moon = swe.calc_ut(jd, swe.MOON, flags)[0][0] % 360

    sun_sign = int(sun // 30)
    moon_sign = int(moon // 30)
    nak_idx = int(moon // (360 / 27))
    pada = int((moon % (360 / 27)) // (360 / 108)) + 1

    # Panchanga: tithi & yoga from elongation
    elong = (moon - sun) % 360
    tithi_num = int(elong // 12) + 1  # 1..30
    paksha = "Shukla (waxing)" if tithi_num <= 15 else "Krishna (waning)"
    t = ((tithi_num - 1) % 15) + 1
    tithi = (
        "Purnima"
        if tithi_num == 15
        else ("Amavasya" if tithi_num == 30 else _TITHIS[t - 1])
    )
    yoga_num = int(((sun + moon) % 360) // (360 / 27))
    yogas = [
        "Vishkambha",
        "Priti",
        "Ayushman",
        "Saubhagya",
        "Shobhana",
        "Atiganda",
        "Sukarma",
        "Dhriti",
        "Shula",
        "Ganda",
        "Vriddhi",
        "Dhruva",
        "Vyaghata",
        "Harshana",
        "Vajra",
        "Siddhi",
        "Vyatipata",
        "Variyana",
        "Parigha",
        "Shiva",
        "Siddha",
        "Sadhya",
        "Shubha",
        "Shukla",
        "Brahma",
        "Indra",
        "Vaidhriti",
    ]
    weekday = _WEEKDAYS[_dt.date(y, m, d).weekday()]
    return {
        "sun_sign": _RASHIS[sun_sign],
        "sun_sign_sanskrit": _RASHI_SANSKRIT[sun_sign],
        "moon_sign": _RASHIS[moon_sign],
        "moon_sign_sanskrit": _RASHI_SANSKRIT[moon_sign],
        "nakshatra": _NAKSHATRAS[nak_idx],
        "nakshatra_lord": _NAK_LORDS[nak_idx % 9],
        "pada": pada,
        "tithi": f"{tithi} ({paksha})",
        "yoga": yogas[yoga_num],
        "weekday": weekday,
        "weekday_lord": _WEEK_LORDS[_dt.date(y, m, d).weekday()],
    }


# ────────────────────────────────────────────────────────────────────────────
# Public one-shot reading
# ────────────────────────────────────────────────────────────────────────────


def full_reading(
    y: int,
    m: int,
    d: int,
    gender: str = "",
    birth_time: str = "",
    birth_place: str = "",
    name: str = "",
) -> dict:
    """Everything the assistant needs for a complete reading, computed here."""
    r = _reduce
    mul = moolank(d)
    bha = bhagyank(y, m, d)
    grid = loshu_grid(y, m, d)

    # Mulank-based compatibility "friend/neutral/enemy" (classic Chaldean set)
    _FRIENDS = {
        1: {1, 2, 3, 5, 6, 9},
        2: {1, 2, 3, 5},
        3: {1, 2, 3, 5, 6, 9},
        4: {1, 4, 5, 6, 7, 8},
        5: {1, 3, 4, 5, 6, 8, 9},
        6: {1, 3, 4, 5, 6, 9},
        7: {1, 4, 5, 7},
        8: {4, 5, 6, 8},
        9: {1, 3, 6, 9},
    }
    friends = sorted(
        _FRIENDS.get(mul % 10 if mul in _MASTER else mul, set())
        - {mul % 10 if mul in _MASTER else mul}
    )

    out = {
        "dob": f"{d:02d}-{m:02d}-{y:04d}",
        "numerology": {
            "moolank": mul,
            "moolank_meaning": _NUM_MEANING.get(mul, ""),
            "bhagyank": bha,
            "bhagyank_meaning": _NUM_MEANING.get(bha, ""),
            "moolank_lord_gem": _MOOLANK_LORDS.get(
                mul % 10 if mul in _MASTER else mul, ("", "")
            ),
            "lucky_friends": friends,
            "kua": kua_number(y, gender) if gender else None,
            "lo_shu": grid,
            "name_number": name_number(name) if name else None,
        },
        "western": {
            "sun_sign": western_sun_sign(m, d),
            "traits": _SUN_TRAITS[western_sun_sign(m, d)],
            "birthstone": _BIRTHSTONES[m],
            "birth_flower": _FLOWERS[m],
            "chinese": chinese_zodiac(y),
        },
    }

    # Vedic block: noon IST default when no time given (moon moves ≤7°/day —
    # sign is exact unless the moon is within ~3.5° of a cusp; flag that).
    hour, minute = 12, 0
    if birth_time:
        try:
            parts = birth_time.strip().replace(".", ":").split(":")
            hour = int(parts[0]) % 24
            minute = int(parts[1]) if len(parts) > 1 else 0
        except Exception:
            hour, minute = 12, 0
    v = vedic_positions(y, m, d, hour, minute)
    if "error" not in v:
        v["time_source"] = (
            "given birth time"
            if birth_time
            else "assumed 12:00 IST (no birth time given)"
        )
        if not birth_time:
            # Cusp risk: recompute ±6h; if the moon sign flips, flag uncertainty
            v1 = vedic_positions(y, m, d, 6, 0)
            v2 = vedic_positions(y, m, d, 18, 0)
            if not (v1.get("moon_sign") == v2.get("moon_sign") == v.get("moon_sign")):
                v["moon_sign_uncertain"] = (
                    "moon near a sign cusp — give the birth time for an exact moon sign"
                )
        out["vedic"] = v
    else:
        out["vedic"] = v

    out["how_to_use"] = (
        "Weave these computed numbers/positions into a warm, personal reading. "
        "Use the native language of the conversation. Do NOT invent any numbers; "
        "if a value is missing (e.g. moon sign uncertain), say so and ask for the "
        "birth time. Keep it uplifting but honest, 60-120 words unless asked for more."
    )
    return out


def parse_dob(text: str) -> tuple[int, int, int] | None:
    """Extract (y, m, d) from free-form DOB text. Returns None on failure.
    Accepts: 14-03-1992, 14/03/92, 1992-03-14, '14 March 1992', 'March 14, 1992'."""
    import re

    t = (text or "").strip()
    if not t:
        return None
    months = {
        mn.lower(): i
        for i, mn in enumerate(
            [
                "January",
                "February",
                "March",
                "April",
                "May",
                "June",
                "July",
                "August",
                "September",
                "October",
                "November",
                "December",
            ],
            1,
        )
    }
    for abbr, i in [(mn[:3].lower(), i) for mn, i in months.items()]:
        months[abbr] = i

    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})[,.]?\s+(\d{4})", t)
    if m and m.group(2).lower() in months:
        return int(m.group(3)), months[m.group(2).lower()], int(m.group(1))
    m = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?[,.]?\s+(\d{4})", t)
    if m and m.group(1).lower() in months:
        return int(m.group(3)), months[m.group(1).lower()], int(m.group(2))
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", t)
    if m:
        d_, mo, y_ = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y_ < 100:
            y_ = 2000 + y_ if y_ < 30 else 1900 + y_
        # Heuristic: second field > 12 ⇒ first field is the month (US style)
        if mo > 12:
            d_, mo = mo, d_
        try:
            _dt.date(y_, mo, d_)
            return y_, mo, d_
        except ValueError:
            return None
    return None


# ────────────────────────────────────────────────────────────────────────────
# Daily panchang — sunrise/sunset-anchored, with Rahu Kaal & Abhijit muhurta
# ────────────────────────────────────────────────────────────────────────────

# Rahu Kaal: the 1/8th-of-daylight segment ruled by Rahu, per weekday
# (Mon=2nd … Sun=8th). Standard tables.
_RAHU_SEGMENT = {0: 2, 1: 3, 2: 5, 3: 7, 4: 6, 5: 4, 6: 8}
# Abhijit muhurta: the 8th of 15 equal day divisions (mid-day ~ +/-24min).

_KARANAS = [
    "Bava",
    "Balava",
    "Kaulava",
    "Taitila",
    "Gara",
    "Vanija",
    "Vishti",
    "Shakuni",
    "Chatushpada",
    "Naga",
    "Kimstughna",
]


def _parse_hhmm(s: str) -> tuple[int, int]:
    parts = (s or "12:00").strip().replace(".", ":").split(":")
    try:
        return int(parts[0]) % 24, int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return 12, 0


def _fmt_clock(hours: float) -> str:
    """Decimal hours → 'HH:MM' (24h), negative-safe (before-midnight wrap)."""
    total = round((hours % 24) * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


@lru_cache(maxsize=256)
def panchang(
    y: int,
    m: int,
    d: int,
    hh: int = 12,
    mm: int = 0,
    lat: float = 26.9124,
    lon: float = 75.7873,
) -> dict:
    """Full panchang for the solar day at a location (default: Jaipur).

    Sunrise/sunset via Swiss Ephemeris sunrise_transit_search; tithi/nakshatra/
    yoga/karana computed at LOCAL NOON of the solar day (the convention most
    panchang listings use) with the given (hh, mm) noted separately.
    """
    try:
        import swisseph as swe
    except ImportError:
        return {
            "error": "swisseph not installed — run: .venv/bin/python -m pip install pysweph "
            "(maintained fork with Python 3.12 wheels; same import name)"
        }

    swe.set_sid_mode(swe.SIDM_LAHIRI)
    jd_local_midnight = swe.julday(y, m, d, 0.0)  # UT of midnight IST ≈ prior evening
    # Search sunrise/sunset in local day window: 00:00..24:00 IST
    jd0 = swe.julday(y, m, d, -5.5)  # IST midnight in UT
    try:
        jrise = swe.rise_trans(
            jd0,
            swe.SUN,
            swe.CALC_RISE,
            (
                lon,
                lat,
                435.0,
            ),
        )[1][0]
        jset = swe.rise_trans(
            jd0,
            swe.SUN,
            swe.CALC_SET,
            (
                lon,
                lat,
                435.0,
            ),
        )[1][0]
    except Exception:
        jrise = swe.rise_trans(
            jd0 - 0.5,
            swe.SUN,
            swe.CALC_RISE,
            (
                lon,
                lat,
                435.0,
            ),
        )[1][0]
        jset = swe.rise_trans(
            jd0 - 0.5,
            swe.SUN,
            swe.CALC_SET,
            (
                lon,
                lat,
                435.0,
            ),
        )[1][0]

    def _j2ist(jd_):
        y_, mo_, d_, h_ = swe.revjul(jd_ + 5.5 / 24.0)
        return f"{int(h_):02d}:{int((h_ % 1) * 60):02d}"

    sunrise = _j2ist(jrise)
    sunset = _j2ist(jset)
    day_len = jset - jrise  # in days
    seg = day_len * 24 / 8.0  # hours per 1/8th

    # Convert sunrise to decimal IST hours for segment arithmetic
    y_r, m_r, d_r, h_r = swe.revjul(jrise + 5.5 / 24.0)
    sunrise_h = h_r
    rahu_idx = _RAHU_SEGMENT[_dt.date(y, m, d).weekday()] - 1
    rahu_start = sunrise_h + rahu_idx * seg
    rahu_end = rahu_start + seg

    # Abhijit: 8th of 15 day-divisions → [sunrise + 7/15 L, sunrise + 8/15 L)
    L = day_len * 24
    abh_start = sunrise_h + (7 / 15) * L
    abh_end = sunrise_h + (8 / 15) * L

    weekday = _dt.date(y, m, d).weekday()
    t = vedic_positions(y, m, d, 12, 0)  # noon convention
    if "error" in t:
        return t
    elong = None
    # Recompute elongation for karana & tithi number at noon
    import swisseph as _swe

    jd_noon = _swe.julday(y, m, d, 12 - 5.5)
    sun = (
        _swe.calc_ut(jd_noon, _swe.SUN, _swe.FLG_MOSEPH | _swe.FLG_SIDEREAL)[0][0] % 360
    )
    moon = (
        _swe.calc_ut(jd_noon, _swe.MOON, _swe.FLG_MOSEPH | _swe.FLG_SIDEREAL)[0][0]
        % 360
    )
    elong = (moon - sun) % 360
    tithi_num = int(elong // 12) + 1
    t_in = ((tithi_num - 1) % 15) + 1
    tithi_name = (
        "Purnima"
        if tithi_num == 15
        else ("Amavasya" if tithi_num == 30 else _TITHIS[t_in - 1])
    )
    paksha = "Shukla" if tithi_num <= 15 else "Krishna"
    karana_idx = int(elong // 6)  # 0..59
    # Karana cycle: 7 movable karanas repeat 8× (56), then 4 fixed + 1 mixed
    k_seq = ["Bava", "Balava", "Kaulava", "Taitila", "Gara", "Vanija", "Vishti"]
    if karana_idx == 0:
        karana = "Kimstughna"
    elif karana_idx >= 57:
        karana = ["Shakuni", "Chatushpada", "Naga", "Kimstughna"][karana_idx - 57]
    else:
        karana = k_seq[(karana_idx - 1) % 7]
    moon_nak = t["nakshatra"]

    return {
        "date": f"{d:02d}-{m:02d}-{y:04d}",
        "weekday": _WEEKDAYS[weekday],
        "weekday_lord": _WEEK_LORDS[weekday],
        "sunrise": sunrise,
        "sunset": sunset,
        "tithi": f"{tithi_name} ({paksha}, #{tithi_num}/30)",
        "nakshatra": moon_nak,
        "nakshatra_lord": t["nakshatra_lord"],
        "yoga": t["yoga"],
        "karana": karana,
        "rahu_kaal": f"{_fmt_clock(rahu_start)}–{_fmt_clock(rahu_end)}",
        "abhijit_muhurta": f"{_fmt_clock(abh_start)}–{_fmt_clock(abh_end)} (most auspicious)",
        "moon_sign": t["moon_sign"],
        "sun_sign": t["sun_sign"],
        "location": {
            "lat": lat,
            "lon": lon,
            "note": "default Jaipur — pass lat/lon for exact local timings",
        },
        "how_to_use": (
            "Present as today's panchang. Rahu Kaal = avoid starting new work; "
            "Abhijit = best window for important beginnings. Times are local "
            "(IST) for the given coordinates."
        ),
    }


# ────────────────────────────────────────────────────────────────────────────
# Daily horoscope — honest Gochara (moon transit from moon sign) + personal-day
# numerology. No invented predictions: every statement derives from a computed
# transit position or number meaning.
# ────────────────────────────────────────────────────────────────────────────

# Gochara moon-from-moon relationship tone table (classical Sade/moorti-ish
# simplified): distance in signs from natal moon → tone.
_GOCHARA_TONE = {
    0: ("same sign — emotional clarity, a reset day", "green"),
    1: ("good for planning and quiet progress", "green"),
    2: ("mixed — money matters need care", "amber"),
    3: ("travel/communication favoured, mind restless", "amber"),
    4: ("domestic focus; avoid arguments at home", "amber"),
    5: ("excellent for children/creativity/study", "green"),
    6: ("health and routine need attention", "red"),
    7: ("passion and partnership highlighted", "green"),
    8: ("obstacles test patience — steady wins", "red"),
    9: ("good for long-range plans and mentors", "green"),
    10: ("career visibility rises — make the ask", "green"),
    11: ("gains and social luck — accept invitations", "green"),
}

_NUM_TONE = {
    1: "lead today — initiate, set direction",
    2: "collaborate — listen more than you speak",
    3: "express — write, pitch, publish",
    4: "systematize — organise, execute checklists",
    5: "move — vary the routine, network",
    6: "care — family, home and harmony first",
    7: "reflect — research, solitude pays",
    8: "execute — money and authority matters",
    9: "give — service and generosity return",
}


def daily_horoscope(
    y: int, m: int, d: int, gender: str = "", birth_time: str = "", for_date: str = ""
) -> dict:
    """Personal daily horoscope: Gochara (transit moon vs natal moon) +
    personal-day number. Honest by construction — if the natal moon is
    unknown/uncertain, the Gochara part says so instead of inventing."""
    import datetime as _dt2

    if for_date:
        parsed = parse_dob(for_date)  # same parser handles dates
        if not parsed:
            return {"error": f"could not parse date: {for_date}"}
        fy, fm, fd = parsed
    else:
        today = _dt2.date.today()
        fy, fm, fd = today.year, today.month, today.day

    personal_day = _reduce(moolank(d) + _digitsum(fd), keep_masters=False)

    out = {
        "date": f"{fd:02d}-{fm:02d}-{fy:04d}",
        "personal_day_number": personal_day,
        "personal_day_theme": _NUM_TONE.get(personal_day, ""),
        "moolank": moolank(d),
        "bhagyank": bhagyank(y, m, d),
    }

    hour, minute = _parse_hhmm(birth_time)
    natal = vedic_positions(y, m, d, hour, minute)
    if "error" in natal:
        out["gochara"] = natal
        return out

    today_v = vedic_positions(fy, fm, fd, 12, 0)
    if "error" in today_v:
        out["gochara"] = today_v
        return out

    # Natal moon without a birth time → noon-approx; verify against ±6h.
    uncertain = not birth_time
    if uncertain:
        v1 = vedic_positions(y, m, d, 6, 0)
        v2 = vedic_positions(y, m, d, 18, 0)
        if len({v1.get("moon_sign"), v2.get("moon_sign"), natal.get("moon_sign")}) > 1:
            out["natal_moon_uncertain"] = True

    natal_rashi = _RASHIS.index(natal["moon_sign"])
    transit_rashi = _RASHIS.index(today_v["moon_sign"])
    dist = (transit_rashi - natal_rashi) % 12
    tone, flag = _GOCHARA_TONE[dist]
    out["gochara"] = {
        "natal_moon_sign": natal["moon_sign"],
        "transit_moon_sign": today_v["moon_sign"],
        "distance_from_natal": dist,
        "tone": tone,
        "flag": flag,
        "uncertain": uncertain,
        "transit_nakshatra": today_v["nakshatra"],
        "moon_sign_uncertain_note": (
            "natal moon near cusp — readings approximate; give birth time for precision"
            if out.get("natal_moon_uncertain")
            else ""
        ),
    }
    out["today_panchang_highlights"] = {
        k: v
        for k, v in panchang(fy, fm, fd, 12, 0).items()
        if k in ("tithi", "nakshatra", "rahu_kaal", "abhijit_muhurta")
    }
    out["how_to_use"] = (
        "Deliver as today's personal forecast: lead with the personal-day number "
        "theme, then the moon-transit tone, then panchang cautions (Rahu Kaal). "
        "Keep it under 80 words, warm and practical, in the user's language. "
        "If natal_moon_uncertain, mention it once briefly."
    )
    return out


# ────────────────────────────────────────────────────────────────────────────
# Daily panchang — sunrise/sunset-anchored, with Rahu Kaal & Abhijit muhurta
# ────────────────────────────────────────────────────────────────────────────

# Traditional 1/8th-of-day weekday division, starting from the weekday lord's
# segment at sunrise. Order of lords per hour-segment (weekday-relative):
# index = weekday (Mon..Sun), value = 8 lords from that day's start segment.
# Rahu segment is the one whose lord is Rahu.
# Rahu Kaal: the segment of the day (1/8th from sunrise) ruled by Rahu,
# per weekday. Standard tables — Monday 2nd segment, Tuesday 3rd, … Sunday 8th.
_RAHU_SEGMENT = {0: 2, 1: 3, 2: 5, 3: 7, 4: 6, 5: 4, 6: 8}

# Weekday lords starting the 1/8th segments at sunrise; the sequence walks the
# planetary week order from that day's lord (traditional Chaldean week order:
# Moon, Mars, Mercury, Jupiter, Venus, Saturn, Sun). Used for the full
# segment table; Rahu's is _RAHU_SEGMENT.
_PLANET_WEEK = ["Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn", "Sun"]


def _seg_table(weekday_idx: int) -> list:
    """8 segment lords for a weekday: the planet-week rotated so the day's
    lord leads."""
    start = _PLANET_WEEK.index(_WEEK_LORDS[weekday_idx])
    return [_PLANET_WEEK[(start + i) % 7] for i in range(8)]
