"""Tests for core/vedic_kundali.py — the deterministic Jyotisha engine.

The reference chart is the owner's saved profile: 26-Apr-1993, 20:15 IST,
Khaga (25.7833 N, 81.1167 E). The expected values below were cross-checked
against the Lahiri/sidereal positions printed by Swiss Ephemeris itself, so a
regression in the wrapper (not in the ephemeris) is what these catch.
"""

from __future__ import annotations

import pytest

from core.vedic_kundali import (
    ASHTAKA_TABLES,
    DASHA_YEARS,
    SIGNS,
    ashtakoota,
    compute_kundali,
    dms,
    ephemeris_status,
    parse_birth_time,
    parse_place,
    render_markdown,
    render_matching,
    varga_sign,
)

BIRTH = {
    "year": 1993,
    "month": 4,
    "day": 26,
    "birth_time": "20:15",
    "birth_place": "Khaga, Uttar Pradesh, India",
    "name": "Test Native",
}


@pytest.fixture(scope="module")
def chart():
    return compute_kundali(**BIRTH)


def test_ephemeris_available():
    status = ephemeris_status()
    assert status["available"] is True, status
    assert status["ayanamsa"] == "Lahiri"


def test_lagna_and_moon(chart):
    assert chart["lagna"]["sign"] == "Scorpio"
    assert chart["lagna"]["nakshatra"] == "Anuradha"
    assert chart["lagna"]["pada"] == 1
    assert chart["rashi"]["sign"] == "Gemini"
    assert chart["rashi"]["nakshatra"] == "Mrigashira"
    assert chart["rashi"]["pada"] == 4


def test_nine_grahas_and_houses(chart):
    assert len(chart["planets"]) == 9
    assert set(chart["planets"]) == {
        "Sun",
        "Moon",
        "Mars",
        "Mercury",
        "Jupiter",
        "Venus",
        "Saturn",
        "Rahu",
        "Ketu",
    }
    assert len(chart["houses"]) == 12
    # Rahu and Ketu are always 180° apart and in opposite signs.
    rahu = chart["planets"]["Rahu"]
    ketu = chart["planets"]["Ketu"]
    assert (rahu["sign_index"] - ketu["sign_index"]) % 12 == 6
    # Every planet must sit in exactly one whole-sign house.
    for p in chart["planets"].values():
        assert 1 <= p["house"] <= 12
        assert p["sign"] == SIGNS[p["sign_index"]]


def test_expected_positions(chart):
    assert chart["planets"]["Sun"]["sign"] == "Aries"
    assert chart["planets"]["Sun"]["dignity"] == "Exalted"
    assert chart["planets"]["Venus"]["dignity"] == "Exalted"
    assert chart["planets"]["Saturn"]["dignity"] == "Moolatrikona"
    assert chart["planets"]["Jupiter"]["retrograde"] is True


def test_panchanga(chart):
    pan = chart["panchanga"]
    assert pan["tithi"].startswith("Panchami")
    assert pan["paksha"] == "Shukla"
    assert pan["nakshatra"] == "Mrigashira"
    assert pan["vara"] == "Monday"
    assert pan["sunrise"] and pan["sunset"] and pan["rahu_kaal"]


def test_all_sixteen_vargas(chart):
    expected = {
        f"D{n}" for n in (1, 2, 3, 4, 7, 9, 10, 12, 16, 20, 24, 27, 30, 40, 45, 60)
    }
    assert set(chart["vargas"]) == expected
    assert chart["vargas"]["D9"]["lagna_sign"] == "Leo"
    for varga in chart["vargas"].values():
        assert varga["lagna_sign"] in SIGNS
        assert set(varga["houses"]) == set(range(1, 13))


def test_navamsa_rule():
    # Aries 0° → Aries; Taurus 0° → Capricorn; Cancer 0° → Cancer.
    assert varga_sign(0.0, 9) == 0
    assert varga_sign(30.0, 9) == 9
    assert varga_sign(90.0, 9) == 3
    # D9 repeats every 3°20'.
    assert varga_sign(3.5, 9) == 1


def test_vimshottari_structure(chart):
    d = chart["dasha"]
    assert d["birth_balance"]["lord"] == "Mars"
    assert 0 < d["birth_balance"]["years"] < DASHA_YEARS["Mars"]
    # Three complete cycles, strictly sequential.
    for a, b in zip(d["mahadashas"], d["mahadashas"][1:]):
        assert a["lord"] != b["lord"]
        assert a["end"] == b["start"]
    current = d["current"]
    assert current["mahadasha"]["lord"] == "Jupiter"
    assert current["antardasha"] is not None
    assert current["pratyantardasha"] is not None


def test_ashtakavarga_totals(chart):
    av = chart["ashtakavarga"]
    expected = {
        "Sun": 48,
        "Moon": 49,
        "Mars": 39,
        "Mercury": 54,
        "Jupiter": 56,
        "Venus": 52,
        "Saturn": 39,
    }
    assert {k: v["total"] for k, v in av["bav"].items()} == expected
    assert av["sav"]["total"] == 337
    for native, table in ASHTAKA_TABLES.items():
        assert sum(len(v) for v in table.values()) == expected[native]


def test_yogas_present(chart):
    names = {y["name"] for y in chart["yogas"]}
    assert "Gajakesari Yoga" in names
    assert "Sasa Yoga (Panchamahapurusha)" in names
    assert any(n.startswith("Raja Yoga") for n in names)
    assert all(y["detail"] for y in chart["yogas"])


def test_neecha_bhanga_reason(chart):
    # Mars is debilitated in Cancer, whose lord IS the Moon. A planet is always
    # 1st from itself, so the Moon must not count as its own kendra — the
    # cancellation has to come from Saturn (lord of Capricorn, Mars' exaltation).
    mars = next(y for y in chart["yogas"] if y["name"].endswith("(Mars)"))
    assert "Saturn" in mars["detail"]
    assert "dispositor Moon" not in mars["detail"]


def test_jaimini_layer(chart):
    karakas = chart["karakas"]
    assert karakas["Atmakaraka"]["planet"] == "Mercury"
    assert karakas["Amatyakaraka"]["planet"] == "Jupiter"
    assert karakas["Darakaraka"]["planet"] == "Saturn"
    assert len(karakas) == 8
    arudha = chart["arudha_lagna"]
    assert arudha["sign"] == "Pisces"
    assert arudha["house_from_lagna"] == 5


def test_bala_index(chart):
    assert len(chart["bala"]) == 7
    for value in chart["bala"].values():
        assert 0 <= value["index"] <= 100
        assert value["label"] in ("Strong", "Moderate", "Weak")


def test_transits_and_sade_sati(chart):
    tr = chart["transits"]
    assert "planets" in tr and "sade_sati" in tr
    assert 1 <= tr["sade_sati"]["house_from_moon"] <= 12


def test_report_markdown(chart):
    report = chart["report_markdown"]
    for heading in (
        "Vedic Kundali",
        "Grahas",
        "Rashi chart",
        "Navamsa",
        "Panchanga",
        "Vimshottari",
        "Yogas",
        "Sarvashtakavarga",
    ):
        assert heading in report
    assert render_markdown(chart) == report


def test_parse_time_and_place():
    assert parse_birth_time("20:15") == (20, 15, 0)
    assert parse_birth_time("8:15 PM") == (20, 15, 0)
    assert parse_birth_time("12:00 am") == (0, 0, 0)
    place = parse_place("Khaga, Uttar Pradesh, India")
    assert place and place["latitude"] == pytest.approx(25.7833)
    coords = parse_place("28.61, 77.20")
    assert coords and coords["latitude"] == pytest.approx(28.61)
    assert parse_place("Atlantis") is None


def test_timezone_dst():
    winter = compute_kundali(2000, 1, 1, "12:00", "New York")
    summer = compute_kundali(2000, 7, 1, "12:00", "New York")
    assert winter["birth"]["utc_offset_hours"] == -5.0
    assert summer["birth"]["utc_offset_hours"] == -4.0


def test_unknown_place_asks_for_coordinates():
    with pytest.raises(ValueError, match="coordinates"):
        compute_kundali(1993, 4, 26, "20:15", "Atlantis")


def test_fixed_offset():
    chart = compute_kundali(
        1993, 4, 26, "20:15", "", latitude=25.7833, longitude=81.1167, tz_offset=5.5
    )
    assert chart["birth"]["utc_offset_hours"] == 5.5
    assert chart["lagna"]["sign"] == "Scorpio"


def test_ashtakoota(chart):
    same = ashtakoota(chart, chart)
    # Identical charts lose Nadi (same nadi), so 28/36 is the maximum.
    assert same["total"] == 28
    assert same["doshas"]["nadi_dosha"] is True
    assert "Kundali Matching" in render_matching(same)

    other = compute_kundali(1995, 1, 1, "10:30", "Delhi", name="Other")
    result = ashtakoota(chart, other)
    assert 0 <= result["total"] <= 36
    assert sum(k["points"] for k in result["kootas"].values()) == result["total"]
    for value in result["kootas"].values():
        assert 0 <= value["points"] <= value["max"]


def test_action_kundali_mode():
    import json

    from actions.astro_reading import astro_action

    payload = json.loads(
        astro_action(
            {
                "mode": "kundali",
                "dob": "26-04-1993",
                "birth_time": "20:15",
                "birth_place": "Khaga",
            }
        )
    )
    assert payload["lagna"]["sign"] == "Scorpio"
    assert payload["rashi"]["nakshatra"] == "Mrigashira"
    assert "report_markdown" in payload
    assert "vargas" in payload and "dasha" in payload


def test_action_asks_for_time():
    from actions.astro_reading import astro_action

    msg = astro_action({"mode": "kundali", "dob": "01-01-1990", "birth_place": "Delhi"})
    assert "birth time" in msg.lower()


def test_dms():
    assert dms(5.3262) == "5°19'34\""
    # Rounds cleanly at the 60" boundary rather than printing 60 seconds.
    assert dms(359.99999) == "360°00'00\""
