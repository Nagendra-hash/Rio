"""
core/vedic_kundali.py — deterministic Vedic (Jyotisha) kundali engine.

Everything here is computed from the Swiss Ephemeris and classical Parashari
tables — the language model never calculates a position, a dasha date or a
koota score. The module is deliberately a pure function of the birth data:

    chart = compute_kundali(1993, 4, 26, "20:15", "Khaga, Uttar Pradesh")

What is included
----------------
  * Sidereal (Lahiri) longitudes for Sun, Moon, Mars, Mercury, Jupiter,
    Venus, Saturn, Rahu and Ketu, with speed/retrograde and combustion.
  * Lagna (ascendant), whole-sign bhavas, bhava-chalit cusps (Porphyry /
    Sripati-style), and the full Panchanga at birth.
  * Shodashavarga: D1, D2, D3, D4, D7, D9, D10, D12, D16, D20, D24, D27,
    D30, D40, D45 and D60, each with its own lagna.
  * Vimshottari dasha to three levels (maha / antar / pratyantar) plus the
    balance at birth and the running period for any target date.
  * Classical yogas (Gajakesari, Panchamahapurusha, Raja, Dhana, Vipreet,
    Neecha Bhanga, Kala Sarpa, Mangal dosha, Sade Sati, …) with the reason
    recorded, so the reading can explain *why* a yoga is present.
  * Ashtakavarga (Bhinnashtakavarga + Sarvashtakavarga) with the classical
    totals asserted by the tests.
  * Graha bala: a transparent 0-100 strength index (dignity, digbala,
    paksha/day-night, cheshta, naisargika, drishti). It is not presented as
    classical Shadbala in rupas — the label says "index" everywhere.
  * Gochara transits and Sade Sati for a target date.
  * Ashtakoota (36-guna) matching for two charts.

House system: whole-sign (rashi) bhavas are the Vedic default and are what the
charts and yoga rules use. Porphyry cusps are supplied separately as
`bhava_chalit` for the finer degree-based reading.

Ephemeris: `pyswisseph` is optional and has no CPython 3.12 wheel, so the
project uses the maintained fork `pysweph`, which exposes the same `swisseph`
module. Both are accepted; if neither is installed the module returns a clear
error instead of silently inventing positions.
"""

from __future__ import annotations

import datetime as _dt
import re
from functools import lru_cache
from zoneinfo import ZoneInfo

# ────────────────────────────────────────────────────────────────────────────
# Classical tables
# ────────────────────────────────────────────────────────────────────────────

SIGNS = [
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
SIGNS_SA = [
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
SIGN_LORDS = [
    "Mars",
    "Venus",
    "Mercury",
    "Moon",
    "Sun",
    "Mercury",
    "Venus",
    "Mars",
    "Jupiter",
    "Saturn",
    "Saturn",
    "Jupiter",
]
SIGN_ELEMENT = ["Fire", "Earth", "Air", "Water"] * 3
SIGN_MODALITY = ["Movable", "Fixed", "Dual"] * 4

PLANETS = [
    "Sun",
    "Moon",
    "Mars",
    "Mercury",
    "Jupiter",
    "Venus",
    "Saturn",
    "Rahu",
    "Ketu",
]
PLANETS_SA = {
    "Sun": "Surya",
    "Moon": "Chandra",
    "Mars": "Mangala",
    "Mercury": "Budha",
    "Jupiter": "Guru",
    "Venus": "Shukra",
    "Saturn": "Shani",
    "Rahu": "Rahu",
    "Ketu": "Ketu",
}
SEVEN = PLANETS[:7]

EXALTATION = {
    "Sun": ("Aries", 10.0),
    "Moon": ("Taurus", 3.0),
    "Mars": ("Capricorn", 28.0),
    "Mercury": ("Virgo", 15.0),
    "Jupiter": ("Cancer", 5.0),
    "Venus": ("Pisces", 27.0),
    "Saturn": ("Libra", 20.0),
    "Rahu": ("Taurus", 20.0),
    "Ketu": ("Scorpio", 20.0),
}
DEBILITATION = {
    p: (SIGNS[(SIGNS.index(s) + 6) % 12], d) for p, (s, d) in EXALTATION.items()
}

OWN_SIGNS = {
    "Sun": ["Leo"],
    "Moon": ["Cancer"],
    "Mars": ["Aries", "Scorpio"],
    "Mercury": ["Gemini", "Virgo"],
    "Jupiter": ["Sagittarius", "Pisces"],
    "Venus": ["Taurus", "Libra"],
    "Saturn": ["Capricorn", "Aquarius"],
    "Rahu": ["Aquarius"],
    "Ketu": ["Scorpio"],
}
# (sign, start°, end°) — classical moolatrikona spans.
MOOLATRIKONA = {
    "Sun": ("Leo", 0.0, 20.0),
    "Moon": ("Taurus", 3.0, 30.0),
    "Mars": ("Aries", 0.0, 12.0),
    "Mercury": ("Virgo", 15.0, 20.0),
    "Jupiter": ("Sagittarius", 0.0, 10.0),
    "Venus": ("Libra", 0.0, 15.0),
    "Saturn": ("Aquarius", 0.0, 20.0),
}

# Naisargika (natural) friendship. Rahu/Ketu follow the common nodal set.
FRIENDS = {
    "Sun": {"Moon", "Mars", "Jupiter"},
    "Moon": {"Sun", "Mercury"},
    "Mars": {"Sun", "Moon", "Jupiter"},
    "Mercury": {"Sun", "Venus"},
    "Jupiter": {"Sun", "Moon", "Mars"},
    "Venus": {"Mercury", "Saturn"},
    "Saturn": {"Mercury", "Venus"},
    "Rahu": {"Venus", "Saturn", "Mercury"},
    "Ketu": {"Mars", "Venus", "Saturn"},
}
ENEMIES = {
    "Sun": {"Venus", "Saturn"},
    "Moon": set(),
    "Mars": {"Mercury"},
    "Mercury": {"Moon"},
    "Jupiter": {"Mercury", "Venus"},
    "Venus": {"Sun", "Moon"},
    "Saturn": {"Sun", "Moon", "Mars"},
    "Rahu": {"Sun", "Moon", "Mars"},
    "Ketu": {"Sun", "Moon"},
}

NAKSHATRAS = [
    # name, lord, deity, gana, yoni, nadi
    ("Ashwini", "Ketu", "Ashwini Kumaras", "Deva", "Horse", "Adi"),
    ("Bharani", "Venus", "Yama", "Manushya", "Elephant", "Madhya"),
    ("Krittika", "Sun", "Agni", "Rakshasa", "Sheep", "Antya"),
    ("Rohini", "Moon", "Brahma", "Manushya", "Serpent", "Antya"),
    ("Mrigashira", "Mars", "Soma", "Deva", "Serpent", "Madhya"),
    ("Ardra", "Rahu", "Rudra", "Manushya", "Dog", "Adi"),
    ("Punarvasu", "Jupiter", "Aditi", "Deva", "Cat", "Adi"),
    ("Pushya", "Saturn", "Brihaspati", "Deva", "Sheep", "Madhya"),
    ("Ashlesha", "Mercury", "Nagas", "Rakshasa", "Cat", "Antya"),
    ("Magha", "Ketu", "Pitris", "Rakshasa", "Rat", "Antya"),
    ("Purva Phalguni", "Venus", "Bhaga", "Manushya", "Rat", "Madhya"),
    ("Uttara Phalguni", "Sun", "Aryaman", "Manushya", "Cow", "Adi"),
    ("Hasta", "Moon", "Savitar", "Deva", "Buffalo", "Adi"),
    ("Chitra", "Mars", "Tvashtar", "Rakshasa", "Tiger", "Madhya"),
    ("Swati", "Rahu", "Vayu", "Deva", "Buffalo", "Antya"),
    ("Vishakha", "Jupiter", "Indragni", "Rakshasa", "Tiger", "Antya"),
    ("Anuradha", "Saturn", "Mitra", "Deva", "Deer", "Madhya"),
    ("Jyeshtha", "Mercury", "Indra", "Rakshasa", "Deer", "Adi"),
    ("Mula", "Ketu", "Nirriti", "Rakshasa", "Dog", "Adi"),
    ("Purva Ashadha", "Venus", "Apah", "Manushya", "Monkey", "Madhya"),
    ("Uttara Ashadha", "Sun", "Vishvedevas", "Manushya", "Mongoose", "Antya"),
    ("Shravana", "Moon", "Vishnu", "Deva", "Monkey", "Antya"),
    ("Dhanishta", "Mars", "Vasus", "Rakshasa", "Lion", "Madhya"),
    ("Shatabhisha", "Rahu", "Varuna", "Rakshasa", "Horse", "Adi"),
    ("Purva Bhadrapada", "Jupiter", "Aja Ekapada", "Manushya", "Lion", "Adi"),
    ("Uttara Bhadrapada", "Saturn", "Ahirbudhnya", "Manushya", "Cow", "Madhya"),
    ("Revati", "Mercury", "Pushan", "Deva", "Elephant", "Antya"),
]

TITHIS = [
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
YOGAS = [
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
WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]
WEEK_LORDS = ["Moon", "Mars", "Mercury", "Jupiter", "Venus", "Saturn", "Sun"]
RAHU_SEGMENT = {0: 2, 1: 3, 2: 5, 3: 7, 4: 6, 5: 4, 6: 8}

# Vimshottari: order and years.
DASHA_SEQ = [
    ("Ketu", 7),
    ("Venus", 20),
    ("Sun", 6),
    ("Moon", 10),
    ("Mars", 7),
    ("Rahu", 18),
    ("Jupiter", 16),
    ("Saturn", 19),
    ("Mercury", 17),
]
DASHA_YEARS = dict(DASHA_SEQ)
YEAR_DAYS = 365.2425

# ────────────────────────────────────────────────────────────────────────────
# Ashtakavarga — Parashara's benefic-place tables
# ────────────────────────────────────────────────────────────────────────────
# Row = the planet whose varga is being built (the "native").
# Key = contributor; values = houses counted from the contributor's sign.
# The classical row totals are 48, 49, 39, 54, 56, 52, 39 (sum 337) — asserted
# in tests/test_vedic_kundali.py, which catches a mistyped place immediately.
ASHTAKA_TABLES = {
    "Sun": {
        "Sun": [1, 2, 4, 7, 8, 9, 10, 11],
        "Moon": [3, 6, 10, 11],
        "Mars": [1, 2, 4, 7, 8, 9, 10, 11],
        "Mercury": [3, 5, 6, 9, 10, 11, 12],
        "Jupiter": [5, 6, 9, 11],
        "Venus": [6, 7, 12],
        "Saturn": [1, 2, 4, 7, 8, 9, 10, 11],
        "Asc": [3, 4, 6, 10, 11, 12],
    },
    "Moon": {
        "Sun": [3, 6, 7, 8, 10, 11],
        "Moon": [1, 3, 6, 7, 10, 11],
        "Mars": [2, 3, 5, 6, 9, 10, 11],
        "Mercury": [1, 3, 4, 5, 7, 8, 10, 11],
        "Jupiter": [1, 4, 7, 8, 10, 11, 12],
        "Venus": [3, 4, 5, 7, 9, 10, 11],
        "Saturn": [3, 5, 6, 11],
        "Asc": [3, 6, 10, 11],
    },
    "Mars": {
        "Sun": [3, 5, 6, 10, 11],
        "Moon": [3, 6, 11],
        "Mars": [1, 2, 4, 7, 8, 10, 11],
        "Mercury": [3, 5, 6, 11],
        "Jupiter": [6, 10, 11, 12],
        "Venus": [6, 8, 11, 12],
        "Saturn": [1, 4, 7, 8, 9, 10, 11],
        "Asc": [1, 3, 6, 10, 11],
    },
    "Mercury": {
        "Sun": [5, 6, 9, 11, 12],
        "Moon": [2, 4, 6, 8, 10, 11],
        "Mars": [1, 2, 4, 7, 8, 9, 10, 11],
        "Mercury": [1, 3, 5, 6, 9, 10, 11, 12],
        "Jupiter": [6, 8, 11, 12],
        "Venus": [1, 2, 3, 4, 5, 8, 9, 11],
        "Saturn": [1, 2, 4, 7, 8, 9, 10, 11],
        "Asc": [1, 2, 4, 6, 8, 10, 11],
    },
    "Jupiter": {
        "Sun": [1, 2, 3, 4, 7, 8, 9, 10, 11],
        "Moon": [2, 5, 7, 9, 11],
        "Mars": [1, 2, 4, 7, 8, 10, 11],
        "Mercury": [1, 2, 4, 5, 6, 9, 10, 11],
        "Jupiter": [1, 2, 3, 4, 7, 8, 10, 11],
        "Venus": [2, 5, 6, 9, 10, 11],
        "Saturn": [3, 5, 6, 12],
        "Asc": [1, 2, 4, 5, 6, 7, 9, 10, 11],
    },
    "Venus": {
        "Sun": [8, 11, 12],
        "Moon": [1, 2, 3, 4, 5, 8, 9, 11, 12],
        "Mars": [3, 4, 6, 9, 11, 12],
        "Mercury": [3, 5, 6, 9, 11],
        "Jupiter": [5, 8, 9, 10, 11],
        "Venus": [1, 2, 3, 4, 5, 8, 9, 10, 11],
        "Saturn": [3, 4, 5, 8, 9, 10, 11],
        "Asc": [1, 2, 3, 4, 5, 8, 9, 11],
    },
    "Saturn": {
        "Sun": [1, 2, 4, 7, 8, 10, 11],
        "Moon": [3, 6, 11],
        "Mars": [3, 5, 6, 10, 11, 12],
        "Mercury": [6, 8, 9, 10, 11, 12],
        "Jupiter": [5, 6, 11, 12],
        "Venus": [6, 11, 12],
        "Saturn": [3, 5, 6, 11],
        "Asc": [1, 3, 4, 6, 10, 11],
    },
}

# ────────────────────────────────────────────────────────────────────────────
# Place lookup (offline) — enough for the owner's profile and major cities
# ────────────────────────────────────────────────────────────────────────────
# Coordinates are city-centre (≈1-2 km). For a village, pass latitude/longitude
# explicitly or the nearest city: the lagna moves 1° per ~4 minutes of time, so
# a 0.1° longitude error is about 24 seconds of birth time.
_PLACES = {
    # India
    "agra": (27.1767, 78.0081, "Asia/Kolkata"),
    "ahmedabad": (23.0225, 72.5714, "Asia/Kolkata"),
    "ajmer": (26.4499, 74.6399, "Asia/Kolkata"),
    "aligarh": (27.8974, 78.0880, "Asia/Kolkata"),
    "allahabad": (25.4358, 81.8463, "Asia/Kolkata"),
    "prayagraj": (25.4358, 81.8463, "Asia/Kolkata"),
    "alwar": (27.5530, 76.6346, "Asia/Kolkata"),
    "ambala": (30.3752, 76.7821, "Asia/Kolkata"),
    "amritsar": (31.6340, 74.8723, "Asia/Kolkata"),
    "anand": (22.5645, 72.9289, "Asia/Kolkata"),
    "aurangabad": (19.8762, 75.3433, "Asia/Kolkata"),
    "bareilly": (28.3670, 79.4304, "Asia/Kolkata"),
    "belgaum": (15.8497, 74.4977, "Asia/Kolkata"),
    "bengaluru": (12.9716, 77.5946, "Asia/Kolkata"),
    "bangalore": (12.9716, 77.5946, "Asia/Kolkata"),
    "bhopal": (23.2599, 77.4126, "Asia/Kolkata"),
    "bhubaneswar": (20.2961, 85.8245, "Asia/Kolkata"),
    "bikaner": (28.0229, 73.3119, "Asia/Kolkata"),
    "chandigarh": (30.7333, 76.7794, "Asia/Kolkata"),
    "chennai": (13.0827, 80.2707, "Asia/Kolkata"),
    "coimbatore": (11.0168, 76.9558, "Asia/Kolkata"),
    "cuttack": (20.4625, 85.8830, "Asia/Kolkata"),
    "dehradun": (30.3165, 78.0322, "Asia/Kolkata"),
    "delhi": (28.6139, 77.2090, "Asia/Kolkata"),
    "new delhi": (28.6139, 77.2090, "Asia/Kolkata"),
    "dhanbad": (23.7957, 86.4304, "Asia/Kolkata"),
    "dharwad": (15.4589, 75.0078, "Asia/Kolkata"),
    "durgapur": (23.5204, 87.3119, "Asia/Kolkata"),
    "faridabad": (28.4089, 77.3178, "Asia/Kolkata"),
    "fatehpur": (25.9333, 80.8000, "Asia/Kolkata"),
    "firozabad": (27.1592, 78.3957, "Asia/Kolkata"),
    "gandhinagar": (23.2156, 72.6369, "Asia/Kolkata"),
    "gaya": (24.7914, 85.0002, "Asia/Kolkata"),
    "ghaziabad": (28.6692, 77.4538, "Asia/Kolkata"),
    "gorakhpur": (26.7606, 83.3732, "Asia/Kolkata"),
    "gurugram": (28.4595, 77.0266, "Asia/Kolkata"),
    "gurgaon": (28.4595, 77.0266, "Asia/Kolkata"),
    "guwahati": (26.1445, 91.7362, "Asia/Kolkata"),
    "gwalior": (26.2183, 78.1828, "Asia/Kolkata"),
    "hisar": (29.1492, 75.7217, "Asia/Kolkata"),
    "hubli": (15.3647, 75.1240, "Asia/Kolkata"),
    "hyderabad": (17.3850, 78.4867, "Asia/Kolkata"),
    "indore": (22.7196, 75.8577, "Asia/Kolkata"),
    "jabalpur": (23.1815, 79.9864, "Asia/Kolkata"),
    "jaipur": (26.9124, 75.7873, "Asia/Kolkata"),
    "jalandhar": (31.3260, 75.5762, "Asia/Kolkata"),
    "jammu": (32.7266, 74.8570, "Asia/Kolkata"),
    "jamnagar": (22.4707, 70.0577, "Asia/Kolkata"),
    "jhansi": (25.4484, 78.5685, "Asia/Kolkata"),
    "jodhpur": (26.2389, 73.0243, "Asia/Kolkata"),
    "kakinada": (16.9891, 82.2475, "Asia/Kolkata"),
    "kalyan": (19.2437, 73.1355, "Asia/Kolkata"),
    "kanpur": (26.4499, 80.3319, "Asia/Kolkata"),
    "khaga": (25.7833, 81.1167, "Asia/Kolkata"),
    "khagaul": (25.7833, 81.1167, "Asia/Kolkata"),
    "kochi": (9.9312, 76.2673, "Asia/Kolkata"),
    "cochin": (9.9312, 76.2673, "Asia/Kolkata"),
    "kolhapur": (16.7050, 74.2433, "Asia/Kolkata"),
    "kolkata": (22.5726, 88.3639, "Asia/Kolkata"),
    "calcutta": (22.5726, 88.3639, "Asia/Kolkata"),
    "kollam": (8.8932, 76.6141, "Asia/Kolkata"),
    "kota": (25.2138, 75.8648, "Asia/Kolkata"),
    "kozhikode": (11.2588, 75.7804, "Asia/Kolkata"),
    "kurnool": (15.8281, 78.0373, "Asia/Kolkata"),
    "lucknow": (26.8467, 80.9462, "Asia/Kolkata"),
    "ludhiana": (30.9010, 75.8573, "Asia/Kolkata"),
    "madurai": (9.9252, 78.1198, "Asia/Kolkata"),
    "mangaluru": (12.9141, 74.8560, "Asia/Kolkata"),
    "mangalore": (12.9141, 74.8560, "Asia/Kolkata"),
    "meerut": (28.9845, 77.7064, "Asia/Kolkata"),
    "moradabad": (28.8386, 78.7733, "Asia/Kolkata"),
    "mumbai": (19.0760, 72.8777, "Asia/Kolkata"),
    "bombay": (19.0760, 72.8777, "Asia/Kolkata"),
    "mysuru": (12.2958, 76.6394, "Asia/Kolkata"),
    "mysore": (12.2958, 76.6394, "Asia/Kolkata"),
    "nagpur": (21.1458, 79.0882, "Asia/Kolkata"),
    "nanded": (19.1383, 77.3210, "Asia/Kolkata"),
    "nashik": (19.9975, 73.7898, "Asia/Kolkata"),
    "noida": (28.5355, 77.3910, "Asia/Kolkata"),
    "patna": (25.5941, 85.1376, "Asia/Kolkata"),
    "pimpri": (18.6298, 73.7997, "Asia/Kolkata"),
    "pune": (18.5204, 73.8567, "Asia/Kolkata"),
    "raipur": (21.2514, 81.6296, "Asia/Kolkata"),
    "rajahmundry": (17.0005, 81.8040, "Asia/Kolkata"),
    "rajkot": (22.3039, 70.8022, "Asia/Kolkata"),
    "ranchi": (23.3441, 85.3096, "Asia/Kolkata"),
    "rourkela": (22.2604, 84.8536, "Asia/Kolkata"),
    "salem": (11.6643, 78.1460, "Asia/Kolkata"),
    "sangli": (16.8524, 74.5815, "Asia/Kolkata"),
    "siliguri": (26.7271, 88.3953, "Asia/Kolkata"),
    "solapur": (17.6599, 75.9064, "Asia/Kolkata"),
    "srinagar": (34.0837, 74.7973, "Asia/Kolkata"),
    "surat": (21.1702, 72.8311, "Asia/Kolkata"),
    "thane": (19.2183, 72.9781, "Asia/Kolkata"),
    "thiruvananthapuram": (8.5241, 76.9366, "Asia/Kolkata"),
    "trivandrum": (8.5241, 76.9366, "Asia/Kolkata"),
    "thrissur": (10.5276, 76.2144, "Asia/Kolkata"),
    "tiruchirappalli": (10.7905, 78.7047, "Asia/Kolkata"),
    "trichy": (10.7905, 78.7047, "Asia/Kolkata"),
    "tirunelveli": (8.7139, 77.7567, "Asia/Kolkata"),
    "tirupati": (13.6288, 79.4192, "Asia/Kolkata"),
    "udaipur": (24.5854, 73.7125, "Asia/Kolkata"),
    "ujjain": (23.1765, 75.7885, "Asia/Kolkata"),
    "vadodara": (22.3072, 73.1812, "Asia/Kolkata"),
    "varanasi": (25.3176, 82.9739, "Asia/Kolkata"),
    "banaras": (25.3176, 82.9739, "Asia/Kolkata"),
    "vijayawada": (16.5062, 80.6480, "Asia/Kolkata"),
    "visakhapatnam": (17.6868, 83.2185, "Asia/Kolkata"),
    "vizag": (17.6868, 83.2185, "Asia/Kolkata"),
    "warangal": (17.9689, 79.5941, "Asia/Kolkata"),
    # Neighbourhood / world
    "kathmandu": (27.7172, 85.3240, "Asia/Kathmandu"),
    "dubai": (25.2048, 55.2708, "Asia/Dubai"),
    "abu dhabi": (24.4539, 54.3773, "Asia/Dubai"),
    "doha": (25.2854, 51.5310, "Asia/Qatar"),
    "riyadh": (24.7136, 46.6753, "Asia/Riyadh"),
    "muscat": (23.5880, 58.3829, "Asia/Muscat"),
    "kuwait city": (29.3759, 47.9774, "Asia/Kuwait"),
    "london": (51.5074, -0.1278, "Europe/London"),
    "paris": (48.8566, 2.3522, "Europe/Paris"),
    "berlin": (52.5200, 13.4050, "Europe/Berlin"),
    "frankfurt": (50.1109, 8.6821, "Europe/Berlin"),
    "amsterdam": (52.3676, 4.9041, "Europe/Amsterdam"),
    "zurich": (47.3769, 8.5417, "Europe/Zurich"),
    "new york": (40.7128, -74.0060, "America/New_York"),
    "washington": (38.9072, -77.0369, "America/New_York"),
    "toronto": (43.6532, -79.3832, "America/Toronto"),
    "chicago": (41.8781, -87.6298, "America/Chicago"),
    "houston": (29.7604, -95.3698, "America/Chicago"),
    "san francisco": (37.7749, -122.4194, "America/Los_Angeles"),
    "los angeles": (34.0522, -118.2437, "America/Los_Angeles"),
    "sydney": (-33.8688, 151.2093, "Australia/Sydney"),
    "melbourne": (-37.8136, 144.9631, "Australia/Melbourne"),
    "singapore": (1.3521, 103.8198, "Asia/Singapore"),
    "kuala lumpur": (3.1390, 101.6869, "Asia/Kuala_Lumpur"),
    "bangkok": (13.7563, 100.5018, "Asia/Bangkok"),
    "tokyo": (35.6762, 139.6503, "Asia/Tokyo"),
    "hong kong": (22.3193, 114.1694, "Asia/Hong_Kong"),
    "johannesburg": (-26.2041, 28.0473, "Africa/Johannesburg"),
    "nairobi": (-1.2921, 36.8219, "Africa/Nairobi"),
}

# ────────────────────────────────────────────────────────────────────────────
# Small helpers
# ────────────────────────────────────────────────────────────────────────────


def _swisseph():
    """Import the Swiss Ephemeris binding, or return a usable error message."""
    try:
        import swisseph as swe

        return swe, None
    except ImportError:
        return None, (
            "Swiss Ephemeris is not installed. Install the maintained fork:\n"
            "  .venv/bin/python -m pip install pysweph\n"
            "(pyswisseph has no CPython 3.12 wheel and needs a C compiler; "
            "pysweph ships one and exposes the same `swisseph` module.)"
        )


def ephemeris_status() -> dict:
    """Health check for the assistant / diagnostics."""
    swe, err = _swisseph()
    if err:
        return {"available": False, "error": err}
    return {
        "available": True,
        "module": getattr(swe, "__file__", "swisseph"),
        "version": getattr(swe, "version", "unknown"),
        "ayanamsa": "Lahiri",
    }


def dms(deg: float) -> str:
    """Decimal degrees → 24°21'33\" (always non-negative)."""
    deg = abs(float(deg))
    d = int(deg)
    m_f = (deg - d) * 60
    m = int(m_f)
    s = round((m_f - m) * 60)
    if s == 60:
        s = 0
        m += 1
    if m == 60:
        m = 0
        d += 1
    return f"{d}°{m:02d}'{s:02d}\""


def _sign_index(longitude: float) -> int:
    return int((longitude % 360) // 30)


def _deg_in_sign(longitude: float) -> float:
    return (longitude % 360) % 30


def _nak_info(longitude: float) -> dict:
    span = 360 / 27
    idx = int((longitude % 360) // span)
    frac = (longitude % span) / span
    pada = int(frac * 4) + 1
    name, lord, deity, gana, yoni, nadi = NAKSHATRAS[idx]
    return {
        "index": idx,
        "name": name,
        "lord": lord,
        "deity": deity,
        "gana": gana,
        "yoni": yoni,
        "nadi": nadi,
        "pada": min(4, max(1, pada)),
        "fraction": round(frac, 6),
    }


def _rashi(longitude: float) -> dict:
    si = _sign_index(longitude)
    return {
        "sign": SIGNS[si],
        "sign_sanskrit": SIGNS_SA[si],
        "sign_index": si,
        "degree_in_sign": round(_deg_in_sign(longitude), 4),
        "dms": dms(_deg_in_sign(longitude)),
        "element": SIGN_ELEMENT[si],
        "modality": SIGN_MODALITY[si],
        "lord": SIGN_LORDS[si],
    }


def _relation(planet: str, other: str) -> str:
    if planet == other:
        return "Self"
    if other in FRIENDS.get(planet, set()):
        return "Friend"
    if other in ENEMIES.get(planet, set()):
        return "Enemy"
    return "Neutral"


def _dignity(planet: str, sign: str, deg_in_sign: float) -> str:
    ex = EXALTATION.get(planet)
    if ex and sign == ex[0]:
        return "Exalted"
    de = DEBILITATION.get(planet)
    if de and sign == de[0]:
        return "Debilitated"
    mt = MOOLATRIKONA.get(planet)
    if mt and sign == mt[0] and mt[1] <= deg_in_sign <= mt[2]:
        return "Moolatrikona"
    if sign in OWN_SIGNS.get(planet, []):
        return "Own sign"
    rel = _relation(planet, SIGN_LORDS[SIGNS.index(sign)])
    return {
        "Friend": "Friendly sign",
        "Enemy": "Enemy sign",
        "Self": "Own sign",
    }.get(rel, "Neutral sign")


def _fmt_date(d: _dt.datetime | _dt.date) -> str:
    return d.strftime("%d-%b-%Y")


def _duration_text(years: float) -> str:
    """0.23 years → '2 months 23 days' (for the dasha balance line)."""
    total_days = round(years * YEAR_DAYS)
    y, rem = divmod(total_days, 365)
    m, d = divmod(rem, 30)
    parts = []
    if y:
        parts.append(f"{y}y")
    if m:
        parts.append(f"{m}m")
    if d or not parts:
        parts.append(f"{d}d")
    return " ".join(parts)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def parse_birth_time(text: str) -> tuple[int, int, int]:
    """Parse '20:15', '8:15 PM', '20.15' → (hour, minute, second)."""
    t = (text or "").strip().lower()
    if not t:
        raise ValueError("birth time is required for a kundali")
    ampm = None
    m = re.search(r"\b(am|pm|a\.m\.|p\.m\.)\b", t)
    if m:
        ampm = m.group(1)[0]
        t = re.sub(r"\b(am|pm|a\.m\.|p\.m\.)\b", "", t)
    nums = re.findall(r"\d+", t)
    if not nums:
        raise ValueError(f"could not read birth time: {text!r}")
    hh = int(nums[0])
    mm = int(nums[1]) if len(nums) > 1 else 0
    ss = int(nums[2]) if len(nums) > 2 else 0
    if ampm == "p" and hh < 12:
        hh += 12
    elif ampm == "a" and hh == 12:
        hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        raise ValueError(f"invalid birth time: {text!r}")
    return hh, mm, ss


def parse_place(place: str) -> dict | None:
    """Offline place → (lat, lon, timezone). Also accepts 'lat,lon'.

    Returns None when unknown; callers should ask for coordinates rather than
    silently defaulting to a random city.
    """
    raw = (place or "").strip()
    if not raw:
        return None
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*", raw)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return {
                "label": f"{lat:.4f}, {lon:.4f}",
                "latitude": lat,
                "longitude": lon,
                "timezone": "Asia/Kolkata",
                "source": "coordinates",
            }
        return None

    key = re.sub(r"[^a-z0-9 ]+", " ", raw.lower())
    key = re.sub(r"\s+", " ", key).strip()
    # Drop common suffixes so "Khaga, Uttar Pradesh, India" matches "khaga".
    candidates = [key]
    for sep in (" uttar pradesh", " india", " up", " district"):
        if key.endswith(sep):
            candidates.append(key[: -len(sep)].strip())
    candidates.extend(part.strip() for part in key.split(","))
    for cand in candidates:
        if cand in _PLACES:
            lat, lon, tz = _PLACES[cand]
            return {
                "label": raw,
                "latitude": lat,
                "longitude": lon,
                "timezone": tz,
                "source": "offline",
            }
    # Last resort: unambiguous prefix ("khaga uttar pradesh india" → khaga).
    words = key.split()
    for n in range(len(words), 0, -1):
        cand = " ".join(words[:n])
        if cand in _PLACES:
            lat, lon, tz = _PLACES[cand]
            return {
                "label": raw,
                "latitude": lat,
                "longitude": lon,
                "timezone": tz,
                "source": "offline-prefix",
            }
    return None


def parse_date_text(text: str) -> _dt.date | None:
    """Parse the common date spellings used for dasha/transit queries."""
    raw = (text or "").strip()
    if not raw or raw.lower() in ("today", "now"):
        return _dt.date.today()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %B %Y", "%d %b %Y", "%B %d %Y"):
        try:
            return _dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _jd_from_local(
    y: int,
    m: int,
    d: int,
    hh: int,
    mm: int,
    ss: int,
    tz: str | None,
    tz_offset: float | None,
) -> tuple[float, float]:
    """Return (julian_day_ut, utc_offset_hours) with DST-aware timezone."""
    swe, err = _swisseph()
    if err:
        raise RuntimeError(err)
    if tz:
        try:
            local = _dt.datetime(y, m, d, hh, mm, ss, tzinfo=ZoneInfo(tz))
        except Exception:  # noqa: BLE001 - unknown tz name falls back to offset
            local = _dt.datetime(y, m, d, hh, mm, ss)
            offset = 5.5 if tz_offset is None else float(tz_offset)
            return swe.julday(y, m, d, hh + mm / 60 + ss / 3600 - offset), offset
        utc = local.astimezone(_dt.UTC)
        offset = local.utcoffset().total_seconds() / 3600
        jd = swe.julday(
            utc.year,
            utc.month,
            utc.day,
            utc.hour + utc.minute / 60 + utc.second / 3600,
        )
        return jd, offset
    offset = 5.5 if tz_offset is None else float(tz_offset)
    jd = swe.julday(y, m, d, hh + mm / 60 + ss / 3600 - offset)
    return jd, offset


def _calc(jd: float, body: int, flags: int) -> tuple[float, float, float, float]:
    """Swiss Ephemeris calc_ut tolerant of old/new return shapes."""
    swe, err = _swisseph()
    if err:
        raise RuntimeError(err)
    res = swe.calc_ut(jd, body, flags)
    vals = res[0]
    return float(vals[0]), float(vals[1]), float(vals[2]), float(vals[3])


def _house_from_cusps(lon: float, cusps) -> int:
    """Which of 12 Porphyry cusps a longitude falls in (wrap-safe)."""
    lon %= 360
    for i in range(1, 13):
        a = cusps[i] % 360
        b = cusps[i + 1] % 360 if i < 12 else cusps[1] % 360
        span = (b - a) % 360
        pos = (lon - a) % 360
        if pos < span or (span == 0 and pos == 0):
            return i
    return 1


def _aspect_houses(planet: str) -> set[int]:
    """Vedic graha drishti: every planet aspects the 7th; Mars 4/8, Jupiter
    5/9, Saturn 3/10, Rahu/Ketu 5/9 (nodal drishti per common practice)."""
    base = {7}
    if planet == "Mars":
        base |= {4, 8}
    elif planet == "Jupiter":
        base |= {5, 9}
    elif planet == "Saturn":
        base |= {3, 10}
    elif planet in ("Rahu", "Ketu"):
        base |= {5, 9}
    return base


def _aspects(p1: dict, p2: dict) -> bool:
    """True when p1 aspects p2 by sign (Vedic drishti)."""
    dist = (p2["sign_index"] - p1["sign_index"]) % 12 + 1
    return dist in _aspect_houses(p1["name"])


# ────────────────────────────────────────────────────────────────────────────
# Vargas (divisional charts)
# ────────────────────────────────────────────────────────────────────────────

VARGA_NAMES = {
    1: "Rashi (D1)",
    2: "Hora (D2)",
    3: "Drekkana (D3)",
    4: "Chaturthamsha (D4)",
    7: "Saptamsha (D7)",
    9: "Navamsa (D9)",
    10: "Dashamsha (D10)",
    12: "Dwadashamsha (D12)",
    16: "Shodashamsha (D16)",
    20: "Vimshamsha (D20)",
    24: "Chaturvimshamsha (D24)",
    27: "Saptavimshamsha (D27)",
    30: "Trimshamsha (D30)",
    40: "Khavedamsha (D40)",
    45: "Akshavedamsha (D45)",
    60: "Shashtiamsha (D60)",
}


def varga_sign(longitude: float, division: int) -> int:
    """Return the sign index (0=Aries) of a longitude in the given varga."""
    lon = longitude % 360
    sign = int(lon // 30)
    deg = lon % 30
    part = int(deg / (30.0 / division)) if division else 0
    part = min(part, division - 1)
    modality = SIGN_MODALITY[sign]
    odd = sign % 2 == 0  # Aries is the first (odd) sign

    if division == 1:
        return sign
    if division == 2:
        # Odd: Leo then Cancer; even: Cancer then Leo.
        return 4 if (odd and part == 0) or (not odd and part == 1) else 3
    if division == 3:
        return (sign + part * 4) % 12
    if division == 4:
        return (sign + part * 3) % 12
    if division == 7:
        return (sign + (0 if odd else 6) + part) % 12
    if division == 9:
        return (sign * 9 + part) % 12
    if division == 10:
        return (sign + (0 if odd else 8) + part) % 12
    if division == 12:
        return (sign + part) % 12
    if division in (16, 45):
        start = {"Movable": 0, "Fixed": 4, "Dual": 8}[modality]
        return (start + part) % 12
    if division == 20:
        start = {"Movable": 0, "Fixed": 8, "Dual": 4}[modality]
        return (start + part) % 12
    if division == 24:
        return ((4 if odd else 3) + part) % 12
    if division == 27:
        start = {"Fire": 0, "Earth": 3, "Air": 6, "Water": 9}[SIGN_ELEMENT[sign]]
        return (start + part) % 12
    if division == 30:
        # Trimshamsha is unequal and lord-based, not a uniform part.
        if odd:
            if deg < 5:
                return 0  # Mars — Aries
            if deg < 10:
                return 10  # Saturn — Aquarius
            if deg < 18:
                return 8  # Jupiter — Sagittarius
            if deg < 25:
                return 2  # Mercury — Gemini
            return 6  # Venus — Libra
        if deg < 5:
            return 1  # Venus — Taurus
        if deg < 12:
            return 5  # Mercury — Virgo
        if deg < 20:
            return 11  # Jupiter — Pisces
        if deg < 25:
            return 9  # Saturn — Capricorn
        return 7  # Mars — Scorpio
    if division == 40:
        return ((0 if odd else 6) + part) % 12
    if division == 60:
        return (sign + part) % 12
    raise ValueError(f"unsupported varga D{division}")


def _varga_chart(longitudes: dict[str, float], asc: float, division: int) -> dict:
    asc_sign = varga_sign(asc, division)
    planets = {}
    for name, lon in longitudes.items():
        si = varga_sign(lon, division)
        planets[name] = {
            "sign": SIGNS[si],
            "sign_sanskrit": SIGNS_SA[si],
            "sign_index": si,
            "house": (si - asc_sign) % 12 + 1,
        }
    houses = {h: [] for h in range(1, 13)}
    for name, p in planets.items():
        houses[p["house"]].append(name)
    return {
        "division": division,
        "name": VARGA_NAMES[division],
        "lagna_sign": SIGNS[asc_sign],
        "lagna_sign_sanskrit": SIGNS_SA[asc_sign],
        "planets": planets,
        "houses": houses,
        "vargottama": [
            n
            for n, p in planets.items()
            if p["sign_index"] == _sign_index(longitudes[n]) and division == 9
        ],
    }


# ────────────────────────────────────────────────────────────────────────────
# Panchanga at birth
# ────────────────────────────────────────────────────────────────────────────

KARANA_MOVABLE = ["Bava", "Balava", "Kaulava", "Taitila", "Gara", "Vanija", "Vishti"]


def _karana(elong: float) -> str:
    idx = int(elong // 6)  # 0..59
    if idx == 0:
        return "Kimstughna"
    if idx >= 57:
        return ["Shakuni", "Chatushpada", "Naga", "Kimstughna"][idx - 57]
    return KARANA_MOVABLE[(idx - 1) % 7]


def _panchanga(
    y: int,
    m: int,
    d: int,
    jd: float,
    sun_lon: float,
    moon_lon: float,
    lat: float,
    lon: float,
    tz_offset: float,
) -> dict:
    swe, err = _swisseph()
    if err:
        return {"error": err}

    elong = (moon_lon - sun_lon) % 360
    tithi_num = int(elong // 12) + 1
    t_in = ((tithi_num - 1) % 15) + 1
    tithi_name = (
        "Purnima"
        if tithi_num == 15
        else ("Amavasya" if tithi_num == 30 else TITHIS[t_in - 1])
    )
    paksha = "Shukla" if tithi_num <= 15 else "Krishna"
    nak = _nak_info(moon_lon)
    yoga_idx = int(((sun_lon + moon_lon) % 360) // (360 / 27)) % 27
    weekday = _dt.date(y, m, d).weekday()

    sunrise = sunset = None
    try:
        jd_local_midnight = swe.julday(y, m, d, -tz_offset)
        geo = (lon, lat, 300.0)
        rise = swe.rise_trans(jd_local_midnight, swe.SUN, swe.CALC_RISE, geo)
        sset = swe.rise_trans(jd_local_midnight, swe.SUN, swe.CALC_SET, geo)
        jrise = rise[1][0]
        jset = sset[1][0]

        def _local_clock(jd_ut: float) -> str:
            _, _, _, hh = swe.revjul(jd_ut + tz_offset / 24.0)
            h = int(hh) % 24
            mi = int((hh % 1) * 60)
            return f"{h:02d}:{mi:02d}"

        sunrise, sunset = _local_clock(jrise), _local_clock(jset)
        _, _, _, h_r = swe.revjul(jrise + tz_offset / 24.0)
        sunrise_h = h_r
        day_len_h = (jset - jrise) * 24.0
        seg = day_len_h / 8.0
        rahu_idx = RAHU_SEGMENT[weekday] - 1
        rahu_start = sunrise_h + rahu_idx * seg
        abh_start = sunrise_h + (7 / 15) * day_len_h
        abh_end = sunrise_h + (8 / 15) * day_len_h

        def _clock(h: float) -> str:
            total = round((h % 24) * 60)
            return f"{total // 60:02d}:{total % 60:02d}"

        rahu_kaal = f"{_clock(rahu_start)}–{_clock(rahu_start + seg)}"
        abhijit = f"{_clock(abh_start)}–{_clock(abh_end)}"
    except Exception:  # noqa: BLE001 - sunrise is optional garnish
        rahu_kaal = abhijit = None

    return {
        "tithi": f"{tithi_name} ({paksha}, #{tithi_num}/30)",
        "tithi_number": tithi_num,
        "paksha": paksha,
        "nakshatra": nak["name"],
        "nakshatra_lord": nak["lord"],
        "nakshatra_pada": nak["pada"],
        "yoga": YOGAS[yoga_idx],
        "karana": _karana(elong),
        "vara": WEEKDAYS[weekday],
        "vara_lord": WEEK_LORDS[weekday],
        "sunrise": sunrise,
        "sunset": sunset,
        "rahu_kaal": rahu_kaal,
        "abhijit_muhurta": abhijit,
    }


# ────────────────────────────────────────────────────────────────────────────
# Vimshottari dasha
# ────────────────────────────────────────────────────────────────────────────


def _dasha_periods(
    start_lord: str, start: _dt.datetime, total_years: float, depth: int = 1
) -> list[dict]:
    """Nine sub-periods of `total_years`, beginning with `start_lord`."""
    idx = [n for n, _ in DASHA_SEQ].index(start_lord)
    out = []
    cursor = start
    for i in range(9):
        lord = DASHA_SEQ[(idx + i) % 9][0]
        years = total_years * DASHA_YEARS[lord] / 120.0
        end = cursor + _dt.timedelta(days=years * YEAR_DAYS)
        item = {
            "lord": lord,
            "start": cursor,
            "end": end,
            "years": round(years, 4),
        }
        if depth > 1:
            item["sub"] = _dasha_periods(lord, cursor, years, depth - 1)
        out.append(item)
        cursor = end
    return out


def _find_period(periods: list[dict], when: _dt.datetime) -> dict | None:
    for p in periods:
        if p["start"] <= when < p["end"]:
            return p
    return None


def _dasha(
    moon_lon: float, birth_local: _dt.datetime, target: _dt.datetime | None = None
) -> dict:
    span = 360 / 27
    nak_idx = int((moon_lon % 360) // span)
    frac_elapsed = ((moon_lon % 360) % span) / span
    start_lord = DASHA_SEQ[nak_idx % 9][0]
    start_years = DASHA_YEARS[start_lord]
    balance_years = (1 - frac_elapsed) * start_years

    # First mahadasha began before birth by the elapsed fraction.
    first_start = birth_local - _dt.timedelta(
        days=frac_elapsed * start_years * YEAR_DAYS
    )
    timeline = []
    cursor = first_start
    idx = [n for n, _ in DASHA_SEQ].index(start_lord)
    # Three full cycles = 360 years, more than any human life.
    for cycle in range(3):
        for i in range(9):
            lord = DASHA_SEQ[(idx + i) % 9][0]
            years = DASHA_YEARS[lord]
            end = cursor + _dt.timedelta(days=years * YEAR_DAYS)
            timeline.append(
                {
                    "lord": lord,
                    "start": cursor,
                    "end": end,
                    "years": years,
                    "cycle": cycle + 1,
                    "age_start": round((cursor - birth_local).days / YEAR_DAYS, 2),
                    "age_end": round((end - birth_local).days / YEAR_DAYS, 2),
                }
            )
            cursor = end

    when = target or _dt.datetime.now()
    current_md = _find_period(timeline, when)
    result = {
        "birth_balance": {
            "lord": start_lord,
            "years": round(balance_years, 4),
            "note": "remaining portion of the first mahadasha at birth",
        },
        "mahadashas": timeline,
        "current": None,
    }
    if not current_md:
        return result

    antars = _dasha_periods(
        current_md["lord"], current_md["start"], current_md["years"], depth=3
    )
    current_ad = _find_period(antars, when)
    current_pd = _find_period(current_ad["sub"], when) if current_ad else None

    def _strip(p: dict) -> dict:
        return {
            "lord": p["lord"],
            "start": _fmt_date(p["start"]),
            "end": _fmt_date(p["end"]),
            "years": p["years"],
        }

    result["current"] = {
        "as_of": _fmt_date(when),
        "mahadasha": _strip(current_md),
        "antardasha": _strip(current_ad) if current_ad else None,
        "pratyantardasha": _strip(current_pd) if current_pd else None,
        "mahadasha_antardashas": [_strip(p) for p in antars],
        "next_mahadashas": [
            {**_strip(p), "age_start": p["age_start"]}
            for p in timeline
            if p["start"] > when
        ][:2],
    }
    # A readable date-friendly alias (JSON cannot carry datetime).
    for p in timeline:
        p["start"] = _fmt_date(p["start"])
        p["end"] = _fmt_date(p["end"])
    result["mahadashas"] = timeline
    return result


# ────────────────────────────────────────────────────────────────────────────
# Strength (transparent 0-100 index, not classical rupas)
# ────────────────────────────────────────────────────────────────────────────

_DIGNITY_SCORE = {
    "Exalted": 1.0,
    "Moolatrikona": 0.92,
    "Own sign": 0.85,
    "Friendly sign": 0.68,
    "Neutral sign": 0.5,
    "Enemy sign": 0.3,
    "Debilitated": 0.12,
}
_DIGBALA_HOUSE = {
    "Sun": 10,
    "Mars": 10,
    "Jupiter": 1,
    "Mercury": 1,
    "Moon": 4,
    "Venus": 4,
    "Saturn": 7,
}
_NAISARGIKA = {
    "Sun": 1.0,
    "Moon": 0.86,
    "Venus": 0.71,
    "Jupiter": 0.64,
    "Mercury": 0.57,
    "Mars": 0.43,
    "Saturn": 0.29,
}
_COMBUST_LIMIT = {
    "Moon": 12.0,
    "Mars": 17.0,
    "Mercury": 14.0,
    "Jupiter": 11.0,
    "Venus": 10.0,
    "Saturn": 15.0,
}


def _graha_bala(planets: dict[str, dict], sun_lon: float) -> dict:
    out = {}
    for name in SEVEN:
        p = planets[name]
        sthana = _DIGNITY_SCORE.get(p["dignity"], 0.5)

        ideal_house = _DIGBALA_HOUSE.get(name, 1)
        ideal_lon = (planets["_asc"] + (ideal_house - 1) * 30 + 15) % 360
        dist = abs((p["longitude"] - ideal_lon + 180) % 360 - 180)
        dig = 1 - dist / 180

        # Paksha / day-night: waxing Moon and day births favour the Sun group.
        elong = (p["longitude"] - sun_lon) % 360
        paksha = 1 - abs(180 - elong) / 180  # 1 near full moon
        if name == "Moon":
            kala = paksha
        elif name in ("Sun", "Jupiter", "Venus"):
            kala = 0.5 + 0.5 * paksha
        else:
            kala = 0.5 + 0.5 * (1 - paksha)

        if name in ("Sun", "Moon"):
            cheshta = 0.5
        else:
            speed = abs(p.get("speed", 0.0))
            typical = {
                "Mars": 0.52,
                "Mercury": 1.2,
                "Jupiter": 0.083,
                "Venus": 1.2,
                "Saturn": 0.033,
            }.get(name, 1.0)
            cheshta = 1.0 if p.get("retrograde") else min(1.0, speed / typical) * 0.8
            cheshta = max(0.15, cheshta)

        nais = _NAISARGIKA.get(name, 0.5)

        # Drik: benefic aspects add, malefic aspects subtract.
        benefics = {"Jupiter", "Venus", "Mercury", "Moon"}
        drik = 0.5
        for other in SEVEN:
            if other == name:
                continue
            if _aspects(planets[other], p):
                drik += 0.08 if other in benefics else -0.08
        drik = min(1.0, max(0.0, drik))

        index = 100 * (
            0.30 * sthana
            + 0.15 * dig
            + 0.15 * kala
            + 0.15 * cheshta
            + 0.15 * nais
            + 0.10 * drik
        )
        out[name] = {
            "index": round(index, 1),
            "label": (
                "Strong" if index >= 65 else "Moderate" if index >= 45 else "Weak"
            ),
            "factors": {
                "sthana_dignity": round(sthana, 2),
                "digbala": round(dig, 2),
                "kala_paksha": round(kala, 2),
                "cheshta": round(cheshta, 2),
                "naisargika": round(nais, 2),
                "drik": round(drik, 2),
            },
            "combust": (
                name in _COMBUST_LIMIT
                and abs((p["longitude"] - sun_lon + 180) % 360 - 180)
                <= _COMBUST_LIMIT[name]
            ),
        }
    return dict(sorted(out.items(), key=lambda kv: kv[1]["index"], reverse=True))


# ────────────────────────────────────────────────────────────────────────────
# Yogas
# ────────────────────────────────────────────────────────────────────────────


def _yogas(planets: dict[str, dict], lagna_sign: int, moon_sign: int) -> list[dict]:
    out: list[dict] = []

    def add(name: str, detail: str) -> None:
        out.append({"name": name, "formed": True, "detail": detail})

    def h(planet: str) -> int:
        return (planets[planet]["sign_index"] - lagna_sign) % 12 + 1

    def h_from(planet: str, ref_sign: int) -> int:
        return (planets[planet]["sign_index"] - ref_sign) % 12 + 1

    def lord_of(house: int) -> str:
        return SIGN_LORDS[(lagna_sign + house - 1) % 12]

    def conj(a: str, b: str) -> bool:
        return planets[a]["sign_index"] == planets[b]["sign_index"]

    def mutual(a: str, b: str) -> bool:
        return _aspects(planets[a], planets[b]) or _aspects(planets[b], planets[a])

    kendras = {1, 4, 7, 10}
    trikonas = {1, 5, 9}
    dusthanas = {6, 8, 12}

    # ── Gajakesari ─────────────────────────────────────────────────────────
    if h_from("Jupiter", moon_sign) in kendras:
        add(
            "Gajakesari Yoga",
            "Jupiter is in a kendra (1/4/7/10) from the Moon — "
            "wisdom, reputation and lasting support.",
        )

    # ── Budhaditya ─────────────────────────────────────────────────────────
    if conj("Sun", "Mercury"):
        add(
            "Budhaditya Yoga",
            f"Sun and Mercury are together in {planets['Sun']['sign']} — "
            "sharp intellect, analysis and communication.",
        )

    # ── Chandra-Mangala ────────────────────────────────────────────────────
    if conj("Moon", "Mars") or mutual("Moon", "Mars"):
        add(
            "Chandra-Mangala Yoga",
            "Moon and Mars are joined or in mutual aspect — drive to earn, "
            "emotional intensity; needs a calm outlet.",
        )

    # ── Panchamahapurusha ──────────────────────────────────────────────────
    for planet, yoga, theme in (
        ("Mars", "Ruchaka Yoga", "courage, leadership, physical energy"),
        ("Mercury", "Bhadra Yoga", "intelligence, commerce, speech"),
        ("Jupiter", "Hamsa Yoga", "wisdom, ethics, respect"),
        ("Venus", "Malavya Yoga", "beauty, comfort, artistic grace"),
        ("Saturn", "Sasa Yoga", "discipline, authority, endurance"),
    ):
        p = planets[planet]
        if h(planet) in kendras and p["dignity"] in (
            "Exalted",
            "Moolatrikona",
            "Own sign",
        ):
            add(
                f"{yoga} (Panchamahapurusha)",
                f"{planet} is {p['dignity']} in a kendra — {theme}.",
            )

    # ── Raja yoga: kendra lord + trikona lord ──────────────────────────────
    raja_pairs = []
    for kh in kendras:
        for th in trikonas:
            a, b = lord_of(kh), lord_of(th)
            if a == b:
                continue
            if conj(a, b) or mutual(a, b):
                raja_pairs.append((kh, th, a, b))
    if raja_pairs:
        kh, th, a, b = raja_pairs[0]
        add(
            "Raja Yoga",
            f"{_ordinal(kh)} lord {a} and {_ordinal(th)} lord {b} are connected "
            f"({len(raja_pairs)} such combination(s)) — authority and rise "
            "through responsibility.",
        )

    # ── Dhana yoga ─────────────────────────────────────────────────────────
    for h1, h2 in ((2, 11), (2, 5), (5, 9), (9, 11)):
        a, b = lord_of(h1), lord_of(h2)
        if a != b and (conj(a, b) or mutual(a, b)):
            add(
                "Dhana Yoga",
                f"{_ordinal(h1)} lord {a} and {_ordinal(h2)} lord {b} are connected — "
                "wealth through effort and networks.",
            )
            break

    # ── Lakshmi ────────────────────────────────────────────────────────────
    p9 = lord_of(9)
    if planets[p9]["dignity"] in ("Exalted", "Moolatrikona", "Own sign") and h(p9) in (
        kendras | trikonas
    ):
        add(
            "Lakshmi Yoga",
            f"9th lord {p9} is strong ({planets[p9]['dignity']}) in house "
            f"{h(p9)} — fortune, grace and support from mentors.",
        )

    # ── Saraswati ──────────────────────────────────────────────────────────
    if all(
        h(x) in ({1, 2, 4, 5, 7, 9, 10}) for x in ("Jupiter", "Venus", "Mercury")
    ) and planets["Jupiter"]["dignity"] in (
        "Exalted",
        "Moolatrikona",
        "Own sign",
        "Friendly sign",
    ):
        add(
            "Saraswati Yoga",
            "Jupiter, Venus and Mercury occupy kendra/trikona/2nd, with a "
            "well-placed Jupiter — learning, arts and eloquence.",
        )

    # ── Amala ──────────────────────────────────────────────────────────────
    for ref_name, ref_sign in (("Lagna", lagna_sign), ("Moon", moon_sign)):
        tenth = (ref_sign + 9) % 12
        benefic = [
            p
            for p in ("Jupiter", "Venus", "Mercury")
            if planets[p]["sign_index"] == tenth
        ]
        if benefic:
            add(
                "Amala Yoga",
                f"{', '.join(benefic)} in the 10th from {ref_name} — clean "
                "reputation and ethical work.",
            )
            break

    # ── Adhi ───────────────────────────────────────────────────────────────
    adhi = [
        p for p in ("Mercury", "Jupiter", "Venus") if h_from(p, moon_sign) in (6, 7, 8)
    ]
    if len(adhi) >= 2:
        add(
            "Adhi Yoga",
            f"{', '.join(adhi)} in the 6th/7th/8th from the Moon — leadership, "
            "comfort and the ability to recover from setbacks.",
        )

    # ── Sunapha / Anapha / Durudhara ───────────────────────────────────────
    second = [p for p in SEVEN if p != "Sun" and h_from(p, moon_sign) == 2]
    twelfth = [p for p in SEVEN if p != "Sun" and h_from(p, moon_sign) == 12]
    if second and twelfth:
        add(
            "Durudhara Yoga",
            "Planets on both sides of the Moon — resources and support.",
        )
    elif second:
        add("Sunapha Yoga", "A planet in the 2nd from the Moon — self-earned comfort.")
    elif twelfth:
        add("Anapha Yoga", "A planet in the 12th from the Moon — refinement and poise.")

    # ── Kemadruma ──────────────────────────────────────────────────────────
    if not second and not twelfth:
        with_moon = [p for p in SEVEN if p not in ("Sun", "Moon") and conj(p, "Moon")]
        if not with_moon:
            add(
                "Kemadruma Yoga",
                "No planet in the 2nd/12th from the Moon and none with it — "
                "emotional self-reliance; periods of solitude. Check for "
                "cancellation by a strong Moon/Jupiter.",
            )

    # ── Sakata ─────────────────────────────────────────────────────────────
    if h_from("Moon", planets["Jupiter"]["sign_index"]) in (6, 8, 12):
        add(
            "Sakata Yoga",
            "Moon is in the 6th/8th/12th from Jupiter — fortunes can swing; "
            "steady routines and patience help.",
        )

    # ── Kala Sarpa ─────────────────────────────────────────────────────────
    rahu = planets["Rahu"]["sign_index"]
    ketu = planets["Ketu"]["sign_index"]
    in_rahu_arc = all(
        (planets[p]["sign_index"] - rahu) % 12 < (ketu - rahu) % 12 for p in SEVEN
    )
    in_ketu_arc = all(
        (planets[p]["sign_index"] - ketu) % 12 < (rahu - ketu) % 12 for p in SEVEN
    )
    if in_rahu_arc or in_ketu_arc:
        arc = "Rahu → Ketu" if in_rahu_arc else "Ketu → Rahu"
        add(
            "Kala Sarpa Yoga",
            f"All seven planets lie inside the {arc} arc — an intense, "
            "karmic life pattern; progress comes in concentrated bursts.",
        )

    # ── Vipreet Raja ───────────────────────────────────────────────────────
    for house, yoga, theme in (
        (6, "Harsha Yoga", "victory over enemies and health setbacks"),
        (8, "Sarala Yoga", "resilience and benefit from crises"),
        (12, "Vimala Yoga", "frugality, independence and clean conduct"),
    ):
        lord = lord_of(house)
        if h(lord) in dusthanas and h(lord) != house:
            add(
                f"{yoga} (Vipreet Raja)",
                f"{_ordinal(house)} lord {lord} sits in house {h(lord)} — {theme}.",
            )

    # ── Parivartana ────────────────────────────────────────────────────────
    for ha in range(1, 13):
        for hb in range(ha + 1, 13):
            la, lb = lord_of(ha), lord_of(hb)
            if la == lb:
                continue
            if (
                planets[la]["sign_index"] == (lagna_sign + hb - 1) % 12
                and planets[lb]["sign_index"] == (lagna_sign + ha - 1) % 12
            ):
                if ha in dusthanas or hb in dusthanas:
                    kind = "Dainya Parivartana"
                elif 3 in (ha, hb):
                    kind = "Khala Parivartana"
                else:
                    kind = "Maha Parivartana"
                add(
                    f"Parivartana Yoga ({kind})",
                    f"{_ordinal(ha)} lord {la} and {_ordinal(hb)} lord {lb} "
                    "exchange signs — the two houses act as one.",
                )

    # ── Neecha Bhanga ──────────────────────────────────────────────────────
    for planet in SEVEN:
        p = planets[planet]
        if p["dignity"] != "Debilitated":
            continue
        dispositor = SIGN_LORDS[p["sign_index"]]
        exalt_lord = SIGN_LORDS[SIGNS.index(EXALTATION[planet][0])]

        def in_kendra(name: str) -> bool:
            """Kendra from the lagna, or from the Moon — but a planet is
            trivially 1st from itself, so the Moon cannot count as its own
            kendra reference."""
            if h(name) in kendras:
                return True
            return name != "Moon" and h_from(name, moon_sign) in kendras

        cancels = []
        if in_kendra(dispositor):
            cancels.append(f"dispositor {dispositor} in a kendra")
        if in_kendra(exalt_lord):
            cancels.append(f"exaltation lord {exalt_lord} in a kendra")
        if mutual(planet, dispositor):
            cancels.append(f"aspected by dispositor {dispositor}")
        if cancels:
            add(
                f"Neecha Bhanga Raja Yoga ({planet})",
                f"{planet} is debilitated in {p['sign']} but cancellation "
                f"applies: {'; '.join(cancels)}.",
            )

    # ── Nodal / affliction yogas ───────────────────────────────────────────
    if conj("Sun", "Rahu") or conj("Sun", "Ketu"):
        add(
            "Grahan Yoga (Sun)",
            "Sun is with Rahu/Ketu — ego and identity can feel eclipsed at "
            "times; father/authority themes need care.",
        )
    if conj("Moon", "Rahu") or conj("Moon", "Ketu"):
        add(
            "Grahan Yoga (Moon)",
            "Moon is with Rahu/Ketu — a vivid inner life; anxiety and "
            "over-imagination are the shadow to manage.",
        )
    if conj("Jupiter", "Rahu"):
        add(
            "Guru-Chandala Yoga",
            "Jupiter is with Rahu — unconventional beliefs; discernment "
            "matters more than borrowed authority.",
        )
    if conj("Mars", "Rahu") or conj("Mars", "Ketu"):
        add(
            "Angaraka Yoga",
            "Mars is with Rahu/Ketu — sudden bursts of energy; channel it "
            "into sport, engineering or decisive action.",
        )
    if conj("Saturn", "Rahu"):
        add(
            "Shrapit Yoga",
            "Saturn is with Rahu — slow, karmic lessons around duty; "
            "patience and clean commitments dissolve the knot.",
        )
    if conj("Sun", "Saturn"):
        add(
            "Pitru Dosha indication",
            "Sun and Saturn are together — a classic father/authority "
            "karmic theme; respect and boundaries heal it.",
        )

    # ── Mangal dosha ───────────────────────────────────────────────────────
    mangal_houses = {1, 2, 4, 7, 8, 12}
    mangal_from = [
        ref
        for ref, sign in (
            ("Lagna", lagna_sign),
            ("Moon", moon_sign),
            ("Venus", planets["Venus"]["sign_index"]),
        )
        if h_from("Mars", sign) in mangal_houses
    ]
    if mangal_from:
        add(
            "Mangal Dosha (Kuja Dosha)",
            f"Mars is in a dosha house from {', '.join(mangal_from)} — "
            "assertiveness in partnership; matching and a calm Mars remedy "
            "are traditionally advised.",
        )

    # ── Vargottama ─────────────────────────────────────────────────────────
    vargottama = [
        p
        for p in SEVEN
        if varga_sign(planets[p]["longitude"], 9) == planets[p]["sign_index"]
    ]
    if vargottama:
        add(
            "Vargottama",
            f"{', '.join(vargottama)} occupy the same sign in D1 and D9 — "
            "the planet's promise is unusually stable.",
        )

    return out


# ────────────────────────────────────────────────────────────────────────────
# Jaimini: Chara Karakas and Arudha Lagna
# ────────────────────────────────────────────────────────────────────────────

KARAKA_NAMES = [
    "Atmakaraka",
    "Amatyakaraka",
    "Bhratrikaraka",
    "Matrikaraka",
    "Pitrikaraka",
    "Putrakaraka",
    "Gnatikaraka",
    "Darakaraka",
]


def _chara_karakas(planets: dict[str, dict]) -> dict:
    """Eight Chara Karakas: highest degree in sign is Atmakaraka. Rahu's
    degree is measured backwards (30° − degree), as the tradition requires."""
    scored = []
    for name in (
        "Sun",
        "Moon",
        "Mars",
        "Mercury",
        "Jupiter",
        "Venus",
        "Saturn",
        "Rahu",
    ):
        deg = planets[name]["degree_in_sign"]
        if name == "Rahu":
            deg = 30.0 - deg
        scored.append((deg, name))
    scored.sort(key=lambda item: item[0], reverse=True)
    return {
        karaka: {
            "planet": name,
            "degree_in_sign": round(deg, 4),
            "sign": planets[name]["sign"],
        }
        for karaka, (deg, name) in zip(KARAKA_NAMES, scored)
    }


def _arudha_lagna(lagna_sign: int, planets: dict[str, dict]) -> dict:
    """Arudha Lagna: count from the lagna lord to the lord's sign, then the
    same count from that sign. A result in the 1st/7th from the lagna is moved
    10 signs on (Jaimini rule)."""
    lord = SIGN_LORDS[lagna_sign]
    lord_sign = planets[lord]["sign_index"]
    distance = (lord_sign - lagna_sign) % 12
    arudha = (lord_sign + distance) % 12
    if (arudha - lagna_sign) % 12 in (0, 6):
        arudha = (arudha + 9) % 12
    return {
        "sign": SIGNS[arudha],
        "sign_sanskrit": SIGNS_SA[arudha],
        "sign_index": arudha,
        "house_from_lagna": (arudha - lagna_sign) % 12 + 1,
        "lord": SIGN_LORDS[arudha],
        "computed_from": lord,
    }


# ────────────────────────────────────────────────────────────────────────────
# Ashtakavarga
# ────────────────────────────────────────────────────────────────────────────


def _ashtakavarga(planets: dict[str, dict], lagna_sign: int) -> dict:
    signs = {p: planets[p]["sign_index"] for p in SEVEN}
    signs["Asc"] = lagna_sign
    bav = {}
    for native, table in ASHTAKA_TABLES.items():
        points = [0] * 12
        for contributor, places in table.items():
            base = signs[contributor]
            for place in places:
                points[(base + place - 1) % 12] += 1
        bav[native] = {
            "by_sign": {SIGNS[i]: points[i] for i in range(12)},
            "by_house": {
                str(h): points[(lagna_sign + h - 1) % 12] for h in range(1, 13)
            },
            "total": sum(points),
        }
    sav_points = [0] * 12
    for native in SEVEN:
        for i in range(12):
            sav_points[i] += bav[native]["by_sign"][SIGNS[i]]
    return {
        "bav": bav,
        "sav": {
            "by_sign": {SIGNS[i]: sav_points[i] for i in range(12)},
            "by_house": {
                str(h): sav_points[(lagna_sign + h - 1) % 12] for h in range(1, 13)
            },
            "total": sum(sav_points),
        },
    }


# ────────────────────────────────────────────────────────────────────────────
# Transits / Gochara
# ────────────────────────────────────────────────────────────────────────────


def _transits(
    natal_moon_sign: int, lat: float, lon: float, tz: str, when: _dt.date
) -> dict:
    swe, err = _swisseph()
    if err:
        return {"error": err}
    jd, _ = _jd_from_local(when.year, when.month, when.day, 12, 0, 0, tz, None)
    swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
    flags = swe.FLG_SWIEPH | swe.FLG_SIDEREAL | swe.FLG_SPEED
    bodies = {
        "Sun": swe.SUN,
        "Moon": swe.MOON,
        "Mars": swe.MARS,
        "Mercury": swe.MERCURY,
        "Jupiter": swe.JUPITER,
        "Venus": swe.VENUS,
        "Saturn": swe.SATURN,
        "Rahu": swe.MEAN_NODE,
    }
    out = {}
    for name, body in bodies.items():
        lonp, _, _, speed = _calc(jd, body, flags)
        si = _sign_index(lonp)
        out[name] = {
            "sign": SIGNS[si],
            "degree": round(_deg_in_sign(lonp), 2),
            "retrograde": speed < 0,
            "house_from_moon": (si - natal_moon_sign) % 12 + 1,
        }
    # Ketu is exactly opposite Rahu.
    rahu_si = SIGNS.index(out["Rahu"]["sign"])
    ketu_si = (rahu_si + 6) % 12
    out["Ketu"] = {
        "sign": SIGNS[ketu_si],
        "degree": out["Rahu"]["degree"],
        "retrograde": True,
        "house_from_moon": (ketu_si - natal_moon_sign) % 12 + 1,
    }

    sat = out["Saturn"]["house_from_moon"]
    if sat in (12, 1, 2):
        phase = {
            12: "rising (first phase)",
            1: "peak (second phase)",
            2: "setting (third phase)",
        }[sat]
        sade = {"active": True, "phase": phase, "house_from_moon": sat}
    else:
        sade = {"active": False, "house_from_moon": sat}
    return {"as_of": when.isoformat(), "planets": out, "sade_sati": sade}


# ────────────────────────────────────────────────────────────────────────────
# Main chart
# ────────────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=128)
def _compute_cached(
    y: int,
    m: int,
    d: int,
    hh: int,
    mm: int,
    ss: int,
    lat: float,
    lon: float,
    tz: str,
    tz_offset: float | None,
    name: str,
    place_label: str,
    true_node: bool,
    as_of: _dt.date | None,
) -> dict:
    swe, err = _swisseph()
    if err:
        return {"error": err}

    swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
    jd, offset = _jd_from_local(y, m, d, hh, mm, ss, tz, tz_offset)
    flags = swe.FLG_SWIEPH | swe.FLG_SIDEREAL | swe.FLG_SPEED

    bodies = {
        "Sun": swe.SUN,
        "Moon": swe.MOON,
        "Mars": swe.MARS,
        "Mercury": swe.MERCURY,
        "Jupiter": swe.JUPITER,
        "Venus": swe.VENUS,
        "Saturn": swe.SATURN,
        "Rahu": swe.TRUE_NODE if true_node else swe.MEAN_NODE,
    }
    longitudes: dict[str, float] = {}
    raw: dict[str, tuple[float, float]] = {}
    for pname, body in bodies.items():
        try:
            lonp, _, _, speed = _calc(jd, body, flags)
        except Exception as exc:  # noqa: BLE001 - ephemeris failure
            # pragma: no cover
            return {"error": f"ephemeris failed for {pname}: {exc}"}
        longitudes[pname] = lonp % 360
        raw[pname] = (speed, lonp)
    rahu_lon = longitudes["Rahu"]
    longitudes["Ketu"] = (rahu_lon + 180) % 360
    raw["Ketu"] = (raw["Rahu"][0], longitudes["Ketu"])

    # Porphyry cusps are the usual textual stand-in for Sripati bhava chalit;
    # the rashi chart itself remains whole-sign.
    cusps, ascmc = swe.houses_ex(jd, lat, lon, b"O", swe.FLG_SIDEREAL)
    asc = ascmc[0] % 360
    mc = ascmc[1] % 360
    lagna_sign = _sign_index(asc)
    moon_lon = longitudes["Moon"]
    moon_sign = _sign_index(moon_lon)

    # ── planets ────────────────────────────────────────────────────────────
    planets: dict[str, dict] = {}
    for pname in PLANETS:
        lonp = longitudes[pname]
        si = _sign_index(lonp)
        speed = raw[pname][0]
        sun_distance = abs((lonp - longitudes["Sun"] + 180) % 360 - 180)
        info = {
            "name": pname,
            "sanskrit": PLANETS_SA[pname],
            **(_rashi(lonp)),
            "longitude": round(lonp, 6),
            "house": (si - lagna_sign) % 12 + 1,
            "bhava_chalit": _house_from_cusps(lonp, cusps),
            "combust": (
                pname in _COMBUST_LIMIT and sun_distance <= _COMBUST_LIMIT[pname]
            ),
            "nakshatra": _nak_info(lonp)["name"],
            "nakshatra_lord": _nak_info(lonp)["lord"],
            "pada": _nak_info(lonp)["pada"],
            "retrograde": speed < 0,
            "speed": round(speed, 6),
            "dignity": _dignity(pname, SIGNS[si], _deg_in_sign(lonp)),
            "sign_lord": SIGN_LORDS[si],
            "aspects_houses": sorted(_aspect_houses(pname)),
        }
        planets[pname] = info
    planets["_asc"] = asc  # internal, stripped before returning

    # ── houses (whole sign) ────────────────────────────────────────────────
    houses = {}
    for hnum in range(1, 13):
        si = (lagna_sign + hnum - 1) % 12
        occupants = [p for p in PLANETS if planets[p]["sign_index"] == si]
        houses[hnum] = {
            "sign": SIGNS[si],
            "sign_sanskrit": SIGNS_SA[si],
            "lord": SIGN_LORDS[si],
            "lord_house": (planets[SIGN_LORDS[si]]["sign_index"] - lagna_sign) % 12 + 1,
            "planets": occupants,
            "cusp_degree": round(cusps[hnum] % 360, 4),
            "cusp_dms": dms(cusps[hnum] % 360),
        }

    # ── panchanga ──────────────────────────────────────────────────────────
    pan = _panchanga(y, m, d, jd, longitudes["Sun"], moon_lon, lat, lon, offset)

    # ── vargas ─────────────────────────────────────────────────────────────
    vargas = {}
    for division in (1, 2, 3, 4, 7, 9, 10, 12, 16, 20, 24, 27, 30, 40, 45, 60):
        chart = _varga_chart(longitudes, asc, division)
        # Keep the payload readable: full planet map only for D1/D9/D10.
        if division not in (1, 9, 10):
            chart.pop("planets", None)
        vargas[f"D{division}"] = chart

    # ── dasha ──────────────────────────────────────────────────────────────
    birth_local = _dt.datetime(y, m, d, hh, mm, ss)
    target_dt = (
        _dt.datetime.combine(as_of, _dt.time(12, 0)) if as_of else _dt.datetime.now()
    )
    dasha = _dasha(moon_lon, birth_local, target_dt)

    # ── strength ───────────────────────────────────────────────────────────
    bala = _graha_bala(planets, longitudes["Sun"])

    # ── yogas ──────────────────────────────────────────────────────────────
    yogas = _yogas(planets, lagna_sign, moon_sign)

    # ── ashtakavarga ───────────────────────────────────────────────────────
    av = _ashtakavarga(planets, lagna_sign)

    # ── Jaimini layer ──────────────────────────────────────────────────────
    karakas = _chara_karakas(planets)
    arudha = _arudha_lagna(lagna_sign, planets)

    planets.pop("_asc", None)

    lagna_nak = _nak_info(asc)
    moon_nak = _nak_info(moon_lon)
    ayanamsa = swe.get_ayanamsa_ut(jd)

    chart = {
        "engine": {
            "name": "vedic_kundali",
            "version": "2.0",
            "ephemeris": "Swiss Ephemeris " + str(getattr(swe, "version", "")),
            "ayanamsa": "Lahiri",
            "ayanamsa_deg": round(ayanamsa, 6),
            "house_system": "whole-sign bhavas + Porphyry (Sripati-style) bhava-chalit",
            "node": "true" if true_node else "mean",
        },
        "birth": {
            "name": name,
            "date": f"{d:02d}-{m:02d}-{y:04d}",
            "time": f"{hh:02d}:{mm:02d}:{ss:02d}",
            "place": place_label,
            "latitude": lat,
            "longitude": lon,
            "timezone": tz,
            "utc_offset_hours": round(offset, 2),
            "julian_day_ut": round(jd, 8),
        },
        "lagna": {
            **_rashi(asc),
            "longitude": round(asc, 6),
            "nakshatra": lagna_nak["name"],
            "pada": lagna_nak["pada"],
            "nakshatra_lord": lagna_nak["lord"],
            "mc": round(mc, 4),
        },
        "rashi": {
            **_rashi(moon_lon),
            "longitude": round(moon_lon, 6),
            "nakshatra": moon_nak["name"],
            "pada": moon_nak["pada"],
            "nakshatra_lord": moon_nak["lord"],
            "deity": moon_nak["deity"],
            "gana": moon_nak["gana"],
            "yoni": moon_nak["yoni"],
            "nadi": moon_nak["nadi"],
        },
        "planets": planets,
        "houses": {str(k): v for k, v in houses.items()},
        "panchanga": pan,
        "vargas": vargas,
        "dasha": dasha,
        "yogas": yogas,
        "bala": bala,
        "ashtakavarga": av,
        "karakas": karakas,
        "arudha_lagna": arudha,
        "transits": _transits(moon_sign, lat, lon, tz, as_of or _dt.date.today()),
    }
    chart["report_markdown"] = render_markdown(chart)
    return chart


def compute_kundali(
    year: int,
    month: int,
    day: int,
    birth_time: str,
    birth_place: str = "",
    *,
    latitude: float | None = None,
    longitude: float | None = None,
    timezone: str | None = None,
    tz_offset: float | None = None,
    name: str = "",
    true_node: bool = False,
    as_of: str = "",
) -> dict:
    """Compute a complete kundali. Raises ValueError with a user-facing
    message when birth data is missing or a place cannot be resolved.

    `as_of` (YYYY-MM-DD / DD-MM-YYYY, or 'today') selects the date used for
    the running dasha and the gochara transits.
    """
    hh, mm, ss = parse_birth_time(birth_time)
    as_of_date = None
    if as_of:
        as_of_date = parse_date_text(as_of)
        if as_of_date is None:
            raise ValueError(f"could not read the as_of date: {as_of!r}")

    place = None
    if latitude is not None and longitude is not None:
        place = {
            "label": birth_place or f"{latitude:.4f}, {longitude:.4f}",
            "latitude": float(latitude),
            "longitude": float(longitude),
            "timezone": timezone or "Asia/Kolkata",
        }
    else:
        place = parse_place(birth_place)
        if not place and birth_place:
            raise ValueError(
                f"I don't have coordinates for '{birth_place}'. Give the nearest "
                "major city, or latitude/longitude (e.g. 25.78, 81.12)."
            )
        if not place:
            raise ValueError(
                "Birth place is required for the lagna and houses. Give a city "
                "(e.g. Khaga, Uttar Pradesh) or latitude/longitude."
            )
    if timezone is None and tz_offset is not None:
        tz = ""  # fixed offset supplied explicitly (e.g. historical LMT)
    else:
        tz = timezone or place.get("timezone") or "Asia/Kolkata"
    lat, lon = float(place["latitude"]), float(place["longitude"])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("latitude/longitude out of range")

    chart = _compute_cached(
        int(year),
        int(month),
        int(day),
        hh,
        mm,
        ss,
        lat,
        lon,
        tz,
        tz_offset,
        name.strip(),
        place["label"],
        bool(true_node),
        as_of_date,
    )
    return chart


# ────────────────────────────────────────────────────────────────────────────
# Rendering
# ────────────────────────────────────────────────────────────────────────────

_SOUTH_LAYOUT = [
    ["Pisces", "Aries", "Taurus", "Gemini"],
    ["Aquarius", None, None, "Cancer"],
    ["Capricorn", None, None, "Leo"],
    ["Sagittarius", "Scorpio", "Libra", "Virgo"],
]


def _south_chart(signs: dict[str, list[str]], lagna: str, title: str) -> str:
    """South-Indian fixed-sign chart in a readable 4×4 grid."""
    abbr = {
        "Aries": "Ar",
        "Taurus": "Ta",
        "Gemini": "Ge",
        "Cancer": "Cn",
        "Leo": "Le",
        "Virgo": "Vi",
        "Libra": "Li",
        "Scorpio": "Sc",
        "Sagittarius": "Sg",
        "Capricorn": "Cp",
        "Aquarius": "Aq",
        "Pisces": "Pi",
    }
    pabbr = {
        "Sun": "Su",
        "Moon": "Mo",
        "Mars": "Ma",
        "Mercury": "Me",
        "Jupiter": "Ju",
        "Venus": "Ve",
        "Saturn": "Sa",
        "Rahu": "Ra",
        "Ketu": "Ke",
    }
    width = 12

    def cell(sign: str) -> list[str]:
        names = [pabbr.get(n, n[:2]) for n in signs.get(sign, [])]
        chunks, current = [], ""
        for token in names:
            candidate = f"{current} {token}".strip()
            if len(candidate) > width and current:
                chunks.append(current)
                current = token
            else:
                current = candidate
        chunks.append(current or "-")
        return [abbr[sign] + (" *" if sign == lagna else "")] + chunks

    out = [f"{title}:", "+" + "+".join(["-" * width] * 4) + "+"]
    for row in _SOUTH_LAYOUT:
        cells = {c: cell(c) for c in row if c}
        height = max((len(v) for v in cells.values()), default=1)
        for line_no in range(height):
            line = "|"
            for c in row:
                if not c:
                    line += " " * width + "|"
                    continue
                value = cells[c][line_no] if line_no < len(cells[c]) else ""
                line += f"{value:^{width}}|"
            out.append(line)
        out.append("+" + "+".join(["-" * width] * 4) + "+")
    out.append("* = Lagna. South-Indian layout (signs fixed).")
    return "\n".join(out)


def render_markdown(chart: dict) -> str:
    """A compact, chat-friendly report. The assistant can quote or translate
    it; every number comes from the computed chart."""
    if "error" in chart:
        return f"Kundali error: {chart['error']}"

    b = chart["birth"]
    lg = chart["lagna"]
    lines = [
        f"## Vedic Kundali — {b['name'] or 'native'}",
        "",
        (
            f"**Birth:** {b['date']} at {b['time']} · {b['place']} "
            f"({b['latitude']:.4f}°, {b['longitude']:.4f}°, {b['timezone']}, "
            f"UTC{b['utc_offset_hours']:+.2f})"
        ),
        (
            f"**Lagna:** {lg['sign']} ({lg['sign_sanskrit']}) {lg['dms']} · "
            f"{lg['nakshatra']} pada {lg['pada']} · lord {lg['lord']}"
        ),
        (
            f"**Rashi (Moon):** {chart['rashi']['sign']} · "
            f"{chart['rashi']['nakshatra']} pada {chart['rashi']['pada']} · "
            f"lord {chart['rashi']['nakshatra_lord']}"
        ),
        (
            f"**Ayanamsa:** Lahiri {chart['engine']['ayanamsa_deg']:.4f}° · "
            f"nodes: {chart['engine']['node']}"
        ),
        "",
        "### Grahas",
        "",
        "| Graha | Sign | Degree | Nakshatra (pada) | House | Dignity | R | C |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for pname in PLANETS:
        p = chart["planets"][pname]
        lines.append(
            f"| {pname} | {p['sign']} | {p['dms']} | "
            f"{p['nakshatra']} ({p['pada']}) | {p['house']} | "
            f"{p['dignity']} | {'R' if p['retrograde'] else '—'} | "
            f"{'C' if p.get('combust') else '—'} |"
        )

    karakas = chart.get("karakas", {})
    arudha = chart.get("arudha_lagna", {})
    if karakas:
        lines += ["", "### Jaimini karakas", ""]
        for karaka, info in karakas.items():
            lines.append(
                f"- **{karaka}:** {info['planet']} in {info['sign']} "
                f"({info['degree_in_sign']:.2f}°)"
            )
        if arudha:
            lines.append(
                f"- **Arudha Lagna:** {arudha['sign']} "
                f"(house {arudha['house_from_lagna']} from lagna)"
            )

    sign_planets: dict[str, list[str]] = {s: [] for s in SIGNS}
    for pname in PLANETS:
        sign_planets[chart["planets"][pname]["sign"]].append(pname)
    lines += [
        "",
        "### Rashi chart (D1)",
        "",
        "```",
        _south_chart(sign_planets, lg["sign"], "D1 Rashi"),
        "```",
    ]

    d9 = chart["vargas"]["D9"]
    d9_signs: dict[str, list[str]] = {s: [] for s in SIGNS}
    for pname, p in d9.get("planets", {}).items():
        d9_signs[p["sign"]].append(pname)
    lines += [
        "",
        f"### Navamsa (D9) — lagna {d9['lagna_sign']}",
        "",
        "```",
        _south_chart(d9_signs, d9["lagna_sign"], "D9 Navamsa"),
        "```",
    ]

    pan = chart["panchanga"]
    lines += [
        "",
        "### Panchanga at birth",
        "",
        f"- **Tithi:** {pan.get('tithi')}",
        (
            f"- **Nakshatra:** {pan.get('nakshatra')} (pada {pan.get('nakshatra_pada')}, "
            f"lord {pan.get('nakshatra_lord')})"
        ),
        f"- **Yoga:** {pan.get('yoga')} · **Karana:** {pan.get('karana')}",
        f"- **Vara:** {pan.get('vara')} (lord {pan.get('vara_lord')})",
        f"- **Sunrise/Sunset:** {pan.get('sunrise')} / {pan.get('sunset')}",
        (
            f"- **Rahu Kaal:** {pan.get('rahu_kaal')} · "
            f"**Abhijit:** {pan.get('abhijit_muhurta')}"
        ),
        "",
        "### Vimshottari dasha",
        "",
    ]
    bal = chart["dasha"]["birth_balance"]
    lines.append(
        f"**Balance at birth:** {bal['lord']} "
        f"{bal['years']:.2f} years ({_duration_text(bal['years'])}) remaining"
    )
    cur = chart["dasha"].get("current")
    if cur:
        md, ad, pd = cur["mahadasha"], cur["antardasha"], cur["pratyantardasha"]
        lines += [
            "",
            (
                f"**Running (as of {cur['as_of']}):** "
                f"{md['lord']} mahadasha {md['start']} → {md['end']}"
            ),
            f"- Antardasha: {ad['lord']} {ad['start']} → {ad['end']}"
            if ad
            else "- Antardasha: —",
            f"- Pratyantardasha: {pd['lord']} {pd['start']} → {pd['end']}"
            if pd
            else "- Pratyantardasha: —",
        ]

    lines += ["", "### Yogas", ""]
    if chart["yogas"]:
        for y in chart["yogas"]:
            lines.append(f"- **{y['name']}** — {y['detail']}")
    else:
        lines.append("- No major classical yoga met the strict conditions.")

    lines += ["", "### Graha bala (0–100 index)", ""]
    for name, b_ in chart["bala"].items():
        lines.append(
            f"- {name}: **{b_['index']}** ({b_['label']})"
            + (" · combust" if b_["combust"] else "")
        )

    sav = chart["ashtakavarga"]["sav"]
    lines += [
        "",
        "### Sarvashtakavarga (by house)",
        "",
        " · ".join(f"H{h}: {sav['by_house'][str(h)]}" for h in range(1, 13)),
    ]

    tr = chart.get("transits", {})
    if "sade_sati" in tr:
        ss = tr["sade_sati"]
        if ss.get("active"):
            lines += [
                "",
                (
                    f"### Sade Sati — **active** ({ss['phase']}, Saturn in "
                    f"house {ss['house_from_moon']} from the Moon)"
                ),
            ]
        else:
            lines += [
                "",
                (
                    f"### Sade Sati — not active (Saturn is in house "
                    f"{ss['house_from_moon']} from the Moon)"
                ),
            ]

    lines += [
        "",
        "---",
        (
            "*Whole-sign bhavas; Lahiri ayanamsa; Vimshottari year = 365.2425 days. "
            "Graha bala is a transparent index, not classical Shadbala rupas.*"
        ),
    ]
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# Ashtakoota matching (36 gunas)
# ────────────────────────────────────────────────────────────────────────────

VARNA_BY_SIGN = {
    "Cancer": "Brahmin",
    "Scorpio": "Brahmin",
    "Pisces": "Brahmin",
    "Aries": "Kshatriya",
    "Leo": "Kshatriya",
    "Sagittarius": "Kshatriya",
    "Taurus": "Vaishya",
    "Virgo": "Vaishya",
    "Capricorn": "Vaishya",
    "Gemini": "Shudra",
    "Libra": "Shudra",
    "Aquarius": "Shudra",
}
_VARNA_RANK = {"Brahmin": 4, "Kshatriya": 3, "Vaishya": 2, "Shudra": 1}

VASHYA_GROUP = {
    "Aries": "Chatushpada",
    "Taurus": "Chatushpada",
    "Gemini": "Manava",
    "Cancer": "Jalachara",
    "Leo": "Vanachara",
    "Virgo": "Manava",
    "Libra": "Manava",
    "Scorpio": "Keeta",
    "Sagittarius": "Manava",
    "Capricorn": "Chatushpada",
    "Aquarius": "Manava",
    "Pisces": "Jalachara",
}
_VASHYA_RANK = {
    "Manava": 4,
    "Chatushpada": 3,
    "Jalachara": 2,
    "Keeta": 1,
    "Vanachara": 0,
}

# Yoni sworn-enemy pairs (0 points); same yoni = 4; the rest 1-3.
YONI_ENEMY = {
    frozenset(("Cow", "Tiger")),
    frozenset(("Elephant", "Lion")),
    frozenset(("Horse", "Buffalo")),
    frozenset(("Dog", "Deer")),
    frozenset(("Serpent", "Mongoose")),
    frozenset(("Monkey", "Sheep")),
    frozenset(("Rat", "Cat")),
}
YONI_FRIEND = {
    frozenset(("Horse", "Deer")),
    frozenset(("Elephant", "Cow")),
    frozenset(("Lion", "Tiger")),
    frozenset(("Serpent", "Cat")),
    frozenset(("Dog", "Monkey")),
    frozenset(("Rat", "Sheep")),
    frozenset(("Buffalo", "Mongoose")),
}

GANA_MATRIX = {
    ("Deva", "Deva"): 6,
    ("Deva", "Manushya"): 6,
    ("Deva", "Rakshasa"): 1,
    ("Manushya", "Deva"): 5,
    ("Manushya", "Manushya"): 6,
    ("Manushya", "Rakshasa"): 0,
    ("Rakshasa", "Deva"): 1,
    ("Rakshasa", "Manushya"): 0,
    ("Rakshasa", "Rakshasa"): 6,
}
GANA_ORDER = ["Deva", "Manushya", "Rakshasa"]


def _moon_facts(chart: dict) -> dict:
    r = chart["rashi"]
    return {
        "sign": r["sign"],
        "sign_index": r["sign_index"],
        "sign_lord": r["lord"],
        "nakshatra": r["nakshatra"],
        "nak_index": [n[0] for n in NAKSHATRAS].index(r["nakshatra"]),
        "gana": r["gana"],
        "yoni": r["yoni"],
        "nadi": r["nadi"],
        "varna": VARNA_BY_SIGN[r["sign"]],
        "vashya": VASHYA_GROUP[r["sign"]],
    }


def _graha_maitri(lord_a: str, lord_b: str) -> int:
    if lord_a == lord_b:
        return 5
    a_to_b = _relation(lord_a, lord_b)
    b_to_a = _relation(lord_b, lord_a)
    pair = {a_to_b, b_to_a}
    if pair == {"Friend"}:
        return 5
    if pair == {"Friend", "Neutral"}:
        return 4
    if pair == {"Neutral"}:
        return 3
    if pair == {"Friend", "Enemy"}:
        return 1
    if pair == {"Neutral", "Enemy"}:
        return 1
    if pair == {"Enemy"}:
        return 0
    return 2


def ashtakoota(chart_a: dict, chart_b: dict) -> dict:
    """Classical 36-guna matching. Points follow the standard tables; Vashya
    uses the common grouped rule and is labelled as such."""
    a, b = _moon_facts(chart_a), _moon_facts(chart_b)

    # Varna (1): groom's varna should be >= bride's.
    varna = 1 if _VARNA_RANK[a["varna"]] >= _VARNA_RANK[b["varna"]] else 0

    # Vashya (2): same group = 2; otherwise higher group gets 1, else 0.
    if a["vashya"] == b["vashya"]:
        vashya = 2
    elif _VASHYA_RANK[a["vashya"]] > _VASHYA_RANK[b["vashya"]]:
        vashya = 1
    else:
        vashya = 0

    # Tara (3): count both ways; inauspicious taras (3,5,7) score 0.
    def tara_points(src: int, dst: int) -> float:
        n = (dst - src) % 27 + 1
        tara = (n - 1) % 9 + 1
        return 0.0 if tara in (3, 5, 7) else 1.5

    tara = tara_points(a["nak_index"], b["nak_index"]) + tara_points(
        b["nak_index"], a["nak_index"]
    )

    # Yoni (4)
    if a["yoni"] == b["yoni"]:
        yoni = 4
    elif frozenset((a["yoni"], b["yoni"])) in YONI_ENEMY:
        yoni = 0
    elif frozenset((a["yoni"], b["yoni"])) in YONI_FRIEND:
        yoni = 3
    else:
        yoni = 2

    # Graha Maitri (5)
    maitri = _graha_maitri(a["sign_lord"], b["sign_lord"])

    # Gana (6)
    gana = GANA_MATRIX[(a["gana"], b["gana"])]

    # Bhakoot (7)
    dist_ab = (b["sign_index"] - a["sign_index"]) % 12 + 1
    dist_ba = (a["sign_index"] - b["sign_index"]) % 12 + 1
    if (
        {dist_ab, dist_ba} & {6, 8}
        or {dist_ab, dist_ba} & {5, 9}
        or {
            dist_ab,
            dist_ba,
        }
        & {2, 12}
    ):
        bhakoot = 0
    else:
        bhakoot = 7

    # Nadi (8)
    nadi = 0 if a["nadi"] == b["nadi"] else 8

    total = varna + vashya + tara + yoni + maitri + gana + bhakoot + nadi
    return {
        "person_a": a,
        "person_b": b,
        "kootas": {
            "varna": {"points": varna, "max": 1},
            "vashya": {"points": vashya, "max": 2},
            "tara": {"points": tara, "max": 3},
            "yoni": {"points": yoni, "max": 4},
            "graha_maitri": {"points": maitri, "max": 5},
            "gana": {"points": gana, "max": 6},
            "bhakoot": {"points": bhakoot, "max": 7},
            "nadi": {"points": nadi, "max": 8},
        },
        "total": round(total, 2),
        "max": 36,
        "verdict": (
            "Excellent match"
            if total >= 28
            else "Good match"
            if total >= 21
            else "Average — needs deeper chart review"
            if total >= 18
            else "Below the usual 18-guna threshold — review the full charts"
        ),
        "doshas": {
            "mangal_dosha_a": any(
                y["name"].startswith("Mangal Dosha") for y in chart_a.get("yogas", [])
            ),
            "mangal_dosha_b": any(
                y["name"].startswith("Mangal Dosha") for y in chart_b.get("yogas", [])
            ),
            "nadi_dosha": nadi == 0,
            "bhakoot_dosha": bhakoot == 0,
        },
    }


def render_matching(result: dict) -> str:
    """Human-readable 36-guna report for two charts."""
    if "error" in result:
        return f"Matching error: {result['error']}"
    a, b = result["person_a"], result["person_b"]
    lines = [
        "## Kundali Matching (Ashtakoota)",
        "",
        (
            f"**A:** {a['sign']} Moon · {a['nakshatra']} · {a['gana']} gana · "
            f"{a['yoni']} yoni · {a['nadi']} nadi"
        ),
        (
            f"**B:** {b['sign']} Moon · {b['nakshatra']} · {b['gana']} gana · "
            f"{b['yoni']} yoni · {b['nadi']} nadi"
        ),
        "",
        "| Koota | Points | Max |",
        "|---|---|---|",
    ]
    nice = {
        "varna": "Varna",
        "vashya": "Vashya",
        "tara": "Tara",
        "yoni": "Yoni",
        "graha_maitri": "Graha Maitri",
        "gana": "Gana",
        "bhakoot": "Bhakoot",
        "nadi": "Nadi",
    }
    for key, label in nice.items():
        k = result["kootas"][key]
        lines.append(f"| {label} | {k['points']} | {k['max']} |")
    lines += [
        f"| **Total** | **{result['total']}** | **36** |",
        "",
        f"**Verdict:** {result['verdict']}",
        "",
        "### Doshas",
    ]
    d = result["doshas"]
    lines += [
        (
            f"- Mangal dosha — A: {'yes' if d['mangal_dosha_a'] else 'no'} · "
            f"B: {'yes' if d['mangal_dosha_b'] else 'no'}"
        ),
        (
            f"- Nadi dosha: {'yes' if d['nadi_dosha'] else 'no'} · "
            f"Bhakoot dosha: {'yes' if d['bhakoot_dosha'] else 'no'}"
        ),
        "",
        (
            "*A is treated as the groom for the Varna rule; Vashya uses the "
            "common grouped convention. Ashtakoota is a compatibility screen, "
            "not a verdict — read both full charts, dashas and Mangal dosha "
            "before any decision.*"
        ),
    ]
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# One-shot entry point used by actions/astro_reading.py
# ────────────────────────────────────────────────────────────────────────────


def kundali_from_profile(profile: dict, target_date: str = "") -> dict:
    """Convenience wrapper: saved profile dict → chart, with optional target
    date for the running dasha/transits."""
    from core.astro_engine import parse_dob

    parsed = parse_dob(profile.get("dob", ""))
    if not parsed:
        return {"error": "saved date of birth is missing or malformed"}
    y, m, d = parsed
    return compute_kundali(
        y,
        m,
        d,
        profile.get("birth_time") or "12:00",
        profile.get("birth_place") or "",
        name=profile.get("name") or "",
        as_of=target_date,
    )


def _cli() -> int:
    """python -m core.vedic_kundali "26-04-1993" "20:15" "Khaga, UP" --name X"""
    import argparse
    import sys

    from core.astro_engine import parse_dob

    parser = argparse.ArgumentParser(description="Print a Vedic kundali.")
    parser.add_argument("dob", help="date of birth (26-04-1993)")
    parser.add_argument("time", help="birth time (20:15)")
    parser.add_argument("place", help="birth place, or 'lat,lon'")
    parser.add_argument("--name", default="")
    parser.add_argument("--as-of", default="", help="dasha/transit date")
    parser.add_argument("--json", action="store_true", help="raw JSON")
    args = parser.parse_args()

    parsed = parse_dob(args.dob)
    if not parsed:
        print(f"could not read the date: {args.dob}", file=sys.stderr)
        return 2
    y, m, d = parsed
    try:
        chart = compute_kundali(
            y, m, d, args.time, args.place, name=args.name, as_of=args.as_of
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if "error" in chart:
        print(chart["error"], file=sys.stderr)
        return 1
    if args.json:
        import json

        print(json.dumps(chart, ensure_ascii=False, indent=2))
    else:
        print(chart["report_markdown"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
