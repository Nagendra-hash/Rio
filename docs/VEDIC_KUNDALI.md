# Vedic Kundali engine

`core/vedic_kundali.py` is the deterministic Jyotisha engine behind the
`astrology_reading` tool's `kundali` and `matching` modes. The language model
explains the output; it never computes a longitude, a dasha boundary or a
koota score.

## Why the engine stopped working, and the fix

The old `core/astro_engine.py` asked for `pyswisseph`. Two things made that
fail on this machine:

1. the project runs CPython 3.12, and `pyswisseph` publishes wheels only up to
   CPython 3.11 — pip therefore fell back to the source tarball;
2. the source build needs a C compiler (`cc`), which is not installed, so the
   install died with `error: [Errno 2] No such file or directory: 'cc'`.

`pysweph` is the maintained community fork of `pyswisseph`. It ships a
`cp312-manylinux_2_17_x86_64` wheel and installs the **same** `swisseph`
module, so no code had to change:

```bash
.venv/bin/python -m pip install pysweph
```

It is now pinned in `requirements.txt` (`pysweph>=2.10.3.6,<2.11`). If the
module is ever missing, `ephemeris_status()` returns a precise install hint and
the chart call returns that message instead of inventing positions.

## What a chart contains

| Block | Details |
|---|---|
| `lagna` | sidereal ascendant, degree, nakshatra + pada, lord, MC |
| `rashi` | Moon sign, nakshatra + pada, deity, gana, yoni, nadi |
| `planets` | 9 grahas: longitude, sign, degree/DMS, house, nakshatra+pada, dignity, retrograde, speed, combustion, whole-sign house and bhava-chalit house |
| `houses` | 12 whole-sign bhavas with sign, lord, lord's house, occupants, Porphyry cusp |
| `panchanga` | tithi/paksha, nakshatra, yoga, karana, vara, sunrise/sunset, Rahu Kaal, Abhijit |
| `vargas` | D1, D2, D3, D4, D7, D9, D10, D12, D16, D20, D24, D27, D30, D40, D45, D60 — each with its own lagna and house map |
| `dasha` | Vimshottari balance at birth, three cycles of mahadashas, running maha/antar/pratyantar, the current mahadasha's nine antardashas, next mahadashas |
| `yogas` | Gajakesari, Budhaditya, Chandra-Mangala, Panchamahapurusha (5), Raja, Dhana, Lakshmi, Saraswati, Amala, Adhi, Sunapha/Anapha/Durudhara, Kemadruma, Sakata, Kala Sarpa, Vipreet (Harsha/Sarala/Vimala), Parivartana, Neecha Bhanga, Grahan, Guru-Chandala, Angaraka, Shrapit, Pitru indication, Mangal dosha, Vargottama — each with the reason |
| `bala` | 0–100 strength index per graha from dignity, digbala, paksha/day-night, cheshta, naisargika and drishti |
| `ashtakavarga` | Bhinnashtakavarga for 7 planets + Sarvashtakavarga, by sign and by house |
| `karakas` | eight Jaimini Chara Karakas (Atma … Dara), with the degree that decided each |
| `arudha_lagna` | Jaimini Arudha Lagna and its house from the lagna |
| `transits` | gochara positions from the natal Moon, Sade Sati phase |
| `report_markdown` | ready-to-quote report with South-Indian D1/D9 charts |

## Conventions (stated, not hidden)

- **Ayanamsa:** Lahiri (Chitrapaksha), Swiss Ephemeris sidereal mode.
- **Houses:** whole-sign bhavas for the rashi chart and yoga rules; Porphyry
  cusps as a Sripati-style `bhava_chalit` cross-check.
- **Nodes:** mean node by default (`true_node=true` switches to the true node).
- **Dasha year:** 365.2425 days, 120-year Vimshottari cycle.
- **Strength:** the number is an *index*, not classical Shadbala rupas. Every
  factor is shown so the reading can be honest about it.
- **Ashtakavarga:** Parashara's benefic-place tables; the engine asserts the
  classical row totals 48/49/39/54/56/52/39 (sum 337).
- **Ashtakoota:** standard 36-guna tables. Varna is scored groom-vs-bride, so
  person A is treated as the groom; Vashya uses the common grouped rule and is
  labelled as such.

## Tool usage

```python
# Full kundali from the saved profile (DOB/time/place already stored)
{"mode": "kundali"}

# Explicit birth data
{"mode": "kundali", "dob": "26 April 1993", "birth_time": "20:15",
 "birth_place": "Khaga, Uttar Pradesh, India"}

# "What will my dasha be in 2027?"
{"mode": "kundali", "as_of": "2027-07-21"}

# Full payload: all vargas + every Ashtakavarga table
{"mode": "kundali", "detail": "full"}

# 36-guna matching
{"mode": "matching", "partner_dob": "01-01-1995",
 "partner_birth_time": "10:30", "partner_birth_place": "Delhi"}
```

If the place is not in the offline table, pass `latitude`/`longitude` (or
`"lat,lon"` as `birth_place`). The offline table covers ~130 Indian cities and
common diaspora cities; the lookup never guesses a random city.

## Python API

```python
from core.vedic_kundali import compute_kundali, ashtakoota, render_matching

chart = compute_kundali(1993, 4, 26, "20:15", "Khaga, Uttar Pradesh, India",
                        name="Rajneesh")
print(chart["report_markdown"])

partner = compute_kundali(1995, 1, 1, "10:30", "Delhi")
match = ashtakoota(chart, partner)
print(match["total"], match["verdict"])
```

From a terminal:

```bash
.venv/bin/python -m core.vedic_kundali "26-04-1993" "20:15" \
    "Khaga, Uttar Pradesh" --name "Rajneesh"
```

## Tests

```bash
.venv/bin/python -m pytest tests/test_vedic_kundali.py -q
```

The suite pins the reference chart (Scorpio lagna 5°19'34", Gemini Moon in
Mrigashira pada 4, Mars dasha balance 0.23 years), all 16 vargas, every
Ashtakavarga row total, the dasha chain, DST handling for New York, and the
koota arithmetic.

## Limitations worth telling a user

- A kundali without a birth time is not a kundali: the lagna moves ~1° every
  4 minutes. The tool asks for the time instead of pretending.
- City-centre coordinates are used for named places. A village 30 km away can
  shift the lagna by a fraction of a degree; pass exact coordinates when it
  matters.
- Kala Sarpa, Mangal dosha and similar labels are *patterns*, not verdicts.
  The report appends the condition that produced them so they can be discussed
  rather than feared.
