import json

from core import astro_engine
from memory.config_manager import get_birth_profile, save_birth_profile

_KUNDALI_MODES = {
    "kundali",
    "kundli",
    "kundalini",
    "chart",
    "birth_chart",
    "janam_kundali",
    "janam",
    "vedic_chart",
}
_MATCH_MODES = {"matching", "match", "guna_milan", "kundali_matching", "compatibility"}


def _same_dob(a: str, b: str) -> bool:
    pa, pb = astro_engine.parse_dob(a or ""), astro_engine.parse_dob(b or "")
    return bool(pa and pb and pa == pb)


def _resolve_birth(p: dict) -> dict:
    """Merge request fields with the saved profile, but only use the saved
    time/place when the requested DOB is the owner's DOB."""
    prof = get_birth_profile() or {}
    dob_raw = (p.get("dob") or prof.get("dob") or "").strip()
    parsed = astro_engine.parse_dob(dob_raw)
    if not parsed:
        raise ValueError(
            "I need a valid date of birth, sir — for example '26 April 1993'."
        )
    y, m, d = parsed
    same = _same_dob(dob_raw, prof.get("dob", ""))
    return {
        "y": y,
        "m": m,
        "d": d,
        "dob": f"{d:02d}-{m:02d}-{y:04d}",
        "birth_time": (
            p.get("birth_time") or (prof.get("birth_time") if same else "") or ""
        ).strip(),
        "birth_place": (
            p.get("birth_place") or (prof.get("birth_place") if same else "") or ""
        ).strip(),
        "gender": (
            p.get("gender") or (prof.get("gender") if same else "") or ""
        ).strip(),
        "name": (p.get("name") or (prof.get("name") if same else "") or "").strip(),
        "profile": prof,
    }


def _kundali(p: dict, player=None) -> str:
    from core.vedic_kundali import compute_kundali

    try:
        b = _resolve_birth(p)
    except ValueError as exc:
        _log(str(exc), player)
        return str(exc)
    if not b["birth_time"]:
        msg = (
            "For a full kundali I need the birth time, sir — even ±15 minutes "
            "changes the lagna. Tell me the time (e.g. '20:15')."
        )
        _log(msg, player)
        return msg
    if not (
        (p.get("latitude") is not None and p.get("longitude") is not None)
        or b["birth_place"]
    ):
        msg = (
            "I also need the birth place, sir — the lagna and houses depend on "
            "latitude/longitude. A city name is enough."
        )
        _log(msg, player)
        return msg

    try:
        chart = compute_kundali(
            b["y"],
            b["m"],
            b["d"],
            b["birth_time"],
            b["birth_place"],
            latitude=p.get("latitude"),
            longitude=p.get("longitude"),
            timezone=(p.get("timezone") or "").strip() or None,
            tz_offset=p.get("tz_offset"),
            name=b["name"],
            true_node=bool(p.get("true_node")),
            as_of=(p.get("as_of") or "").strip(),
        )
    except ValueError as exc:
        _log(str(exc), player)
        return str(exc)

    if "error" in chart:
        _log(chart["error"], player)
        return chart["error"]

    if p.get("save"):
        try:
            save_birth_profile(
                {
                    "dob": f"{b['y']:04d}-{b['m']:02d}-{b['d']:02d}",
                    "birth_time": b["birth_time"],
                    "birth_place": b["birth_place"],
                    "gender": b["gender"],
                    "name": b["name"],
                }
            )
            chart["profile_saved"] = True
        except Exception as exc:
            chart["profile_saved"] = False
            chart["profile_save_error"] = str(exc)

    # Keep a voice turn light: the markdown report carries the story, the
    # structured JSON carries the parts a follow-up question is likely to need.
    if (p.get("detail") or "summary").strip().lower() != "full":
        chart = _summarize_kundali(chart)

    _log(f"kundali for {b['dob']} at {b['birth_time']} ({b['birth_place']})", player)
    return json.dumps(chart, ensure_ascii=False)


def _summarize_kundali(chart: dict) -> dict:
    out = {
        k: chart[k]
        for k in (
            "engine",
            "birth",
            "lagna",
            "rashi",
            "planets",
            "houses",
            "panchanga",
            "yogas",
            "bala",
            "transits",
            "karakas",
            "arudha_lagna",
            "report_markdown",
        )
        if k in chart
    }
    out["vargas"] = {
        k: chart["vargas"][k]
        for k in ("D1", "D9", "D10")
        if k in chart.get("vargas", {})
    }
    av = chart.get("ashtakavarga", {})
    out["ashtakavarga"] = {
        "sav_by_house": av.get("sav", {}).get("by_house", {}),
        "bav_totals": {k: v.get("total") for k, v in av.get("bav", {}).items()},
    }
    d = chart.get("dasha", {})
    out["dasha"] = {
        "birth_balance": d.get("birth_balance"),
        "current": d.get("current"),
    }
    return out


def _matching(p: dict, player=None) -> str:
    from core.vedic_kundali import ashtakoota, compute_kundali, render_matching

    if not (p.get("partner_dob") or "").strip():
        msg = "For matching I need the other person's date of birth, sir."
        _log(msg, player)
        return msg

    def build(src: dict, prefix: str) -> dict:
        local = dict(src)
        if prefix:
            for key in ("dob", "birth_time", "birth_place", "name"):
                if src.get(f"{prefix}_{key}"):
                    local[key] = src[f"{prefix}_{key}"]
            for key in ("latitude", "longitude", "timezone", "tz_offset"):
                if src.get(f"partner_{key}") is not None:
                    local[key] = src[f"partner_{key}"]
        b = _resolve_birth(local)
        if not b["birth_time"] or not b["birth_place"]:
            raise ValueError(
                f"Birth time and place are required for the {prefix or 'first'} "
                "chart — matching uses the Moon, and the Moon needs the moment."
            )
        return compute_kundali(
            b["y"],
            b["m"],
            b["d"],
            b["birth_time"],
            b["birth_place"],
            latitude=local.get("latitude"),
            longitude=local.get("longitude"),
            timezone=(local.get("timezone") or "").strip() or None,
            tz_offset=local.get("tz_offset"),
            name=b["name"],
        )

    try:
        chart_a = build(p, "")
        chart_b = build(p, "partner")
        if "error" in chart_a:
            return chart_a["error"]
        if "error" in chart_b:
            return chart_b["error"]
        result = ashtakoota(chart_a, chart_b)
        result["report_markdown"] = render_matching(result)
        result["chart_a"] = {
            "lagna": chart_a["lagna"]["sign"],
            "moon": chart_a["rashi"]["sign"],
            "nakshatra": chart_a["rashi"]["nakshatra"],
            "mangal_dosha": any(
                y["name"].startswith("Mangal Dosha") for y in chart_a["yogas"]
            ),
        }
        result["chart_b"] = {
            "lagna": chart_b["lagna"]["sign"],
            "moon": chart_b["rashi"]["sign"],
            "nakshatra": chart_b["rashi"]["nakshatra"],
            "mangal_dosha": any(
                y["name"].startswith("Mangal Dosha") for y in chart_b["yogas"]
            ),
        }
    except ValueError as exc:
        _log(str(exc), player)
        return str(exc)

    _log("ashtakoota matching", player)
    return json.dumps(result, ensure_ascii=False)


def astro_action(parameters: dict, player=None, session_memory=None) -> str:
    """Compute a full reading, today's panchang, a personal daily horoscope,
    a complete Vedic kundali, or a 36-guna match. If the user asks to REMEMBER
    their details ('remember my DOB as ...'), flip ``save`` and the profile is
    persisted for the system-prompt injection."""
    p = parameters or {}
    mode = (p.get("mode") or "reading").strip().lower()

    if mode in _KUNDALI_MODES:
        return _kundali(p, player)

    if mode in _MATCH_MODES:
        return _matching(p, player)

    if mode == "panchang":
        import datetime as _dt

        from core.astro_engine import panchang
        from core.astro_engine import parse_dob as _pd

        target = (p.get("date") or "").strip()
        if target:
            parsed = _pd(target)
            if not parsed:
                msg = "I couldn't parse that date, sir."
                _log(msg, player)
                return msg
            yy, mm2, dd = parsed
        else:
            t = _dt.date.today()
            yy, mm2, dd = t.year, t.month, t.day
        result = panchang(yy, mm2, dd)
        _log(f"panchang {dd:02d}-{mm2:02d}-{yy:04d}", player)
        return json.dumps(result, ensure_ascii=False)

    if mode in ("horoscope", "daily", "today"):
        from core.astro_engine import daily_horoscope

        dob_raw = (p.get("dob") or "").strip()
        yy = mm2 = dd = None
        if dob_raw:
            parsed = astro_engine.parse_dob(dob_raw)
            if not parsed:
                msg = "I couldn't parse that date of birth, sir."
                _log(msg, player)
                return msg
            yy, mm2, dd = parsed
        else:
            prof = get_birth_profile()
            if not prof.get("dob"):
                msg = (
                    "For a personal horoscope I need a date of birth, sir — "
                    "tell me yours, or say 'remember my date of birth is ...' "
                    "once and I'll remember it."
                )
                _log(msg, player)
                return msg
            yy, mm2, dd = astro_engine.parse_dob(prof["dob"]) or (None, None, None)
            if not (yy and mm2 and dd):
                msg = "The saved birth date looks malformed, sir — tell me your date of birth again."
                _log(msg, player)
                return msg
        result = daily_horoscope(
            yy,
            mm2,
            dd,
            gender=(p.get("gender") or "").strip(),
            birth_time=(
                p.get("birth_time")
                or (get_birth_profile().get("birth_time") if not dob_raw else "")
                or ""
            ).strip(),
            for_date=(p.get("date") or "").strip(),
        )
        _log(f"horoscope for {dd:02d}-{mm2:02d}-{yy:04d}", player)
        return json.dumps(result, ensure_ascii=False)
    save = bool(p.get("save"))

    dob_raw = (p.get("dob") or "").strip()
    y = m = d = None
    if dob_raw:
        parsed = astro_engine.parse_dob(dob_raw)
        if not parsed:
            msg = (
                "I couldn't parse that date of birth, sir. "
                "Try '14 March 1992' or '14-03-1992'."
            )
            _log(msg, player)
            return msg
        y, m, d = parsed
    else:
        # No DOB in the request → use the saved owner profile when present.
        prof = get_birth_profile()
        if not prof.get("dob"):
            msg = (
                "Whose birth details should I use, sir? "
                "Give me a date of birth — for example, "
                "'astrology for 14 March 1992' — or say "
                "'remember my date of birth is ...' to make it permanent."
            )
            _log(msg, player)
            return msg
        try:
            y, m, d = astro_engine.parse_dob(prof["dob"]) or (None, None, None)
        except Exception:
            y = m = d = None
        if not (y and m and d):
            msg = "The saved birth date looks malformed, sir — please tell me your date of birth again."
            _log(msg, player)
            return msg
        # Fill any gaps from the profile (time/place/gender/name).
        for k_src, k_dst in (
            ("birth_time", "birth_time"),
            ("birth_place", "birth_place"),
            ("gender", "gender"),
            ("name", "name"),
        ):
            if not p.get(k_dst) and prof.get(k_src):
                p[k_dst] = prof[k_src]

    if not (y and m and d):
        msg = "A valid date of birth is required, sir."
        _log(msg, player)
        return msg

    # ── validation (defensive: the model may pass junk) ─────────────────
    try:
        import datetime as _dt

        _dt.date(y, m, d)
    except ValueError:
        msg = f"That date doesn't exist, sir ({d:02d}-{m:02d}-{y:04d})."
        _log(msg, player)
        return msg
    if y < 1900 or y > 2100:
        msg = "Please give a year between 1900 and 2100, sir."
        _log(msg, player)
        return msg

    gender = (p.get("gender") or "").strip().lower() or None
    birth_time = (p.get("birth_time") or "").strip()
    birth_place = (p.get("birth_place") or "").strip()
    name = (p.get("name") or "").strip()

    reading = astro_engine.full_reading(
        y,
        m,
        d,
        gender=gender or "",
        birth_time=birth_time,
        birth_place=birth_place,
        name=name,
    )

    if save:
        try:
            save_birth_profile(
                {
                    "dob": f"{y:04d}-{m:02d}-{d:02d}",
                    "birth_time": birth_time,
                    "birth_place": birth_place,
                    "gender": gender or "",
                    "name": name,
                }
            )
            reading["profile_saved"] = True
        except Exception as e:
            reading["profile_saved"] = False
            reading["profile_save_error"] = str(e)

    _log(
        f"reading for {d:02d}-{m:02d}-{y:04d}" + (" (saved profile)" if save else ""),
        player,
    )
    return json.dumps(reading, ensure_ascii=False)


def _log(message: str, player=None) -> None:
    print(f"[Astro] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass


TOOL = {
    "name": "astrology_reading",
    "description": (
        "Vedic astrology & numerology expert tool. "
        "mode='kundali' (aliases kundli/chart/janam): the COMPLETE Vedic birth "
        "chart — lagna, all nine grahas with sign/degree/nakshatra-pada/house/"
        "dignity/retrograde, whole-sign bhavas, panchanga at birth, 16 vargas "
        "(D1..D60, including Navamsa and Dashamsha), Vimshottari dasha to three "
        "levels, classical yogas with reasons, Ashtakavarga, a transparent graha "
        "bala index, gochara transits and Sade Sati. The tool returns a ready "
        "report_markdown plus structured JSON. "
        "mode='matching': 36-guna Ashtakoota between the owner and partner_dob. "
        "mode='reading' (default): numerology + sun/moon summary (Moolank, "
        "Bhagyank, Lo Shu, Kua, name number, nakshatra, Western/Chinese signs). "
        "mode='panchang': Vedic almanac for any day — tithi, nakshatra, yoga, "
        "karana, sunrise/sunset, Rahu Kaal, Abhijit. "
        "mode='horoscope': personal daily forecast — personal-day number + "
        "moon-transit (Gochara) tone vs natal moon + panchang cautions. "
        "ALL positions, dates and scores are computed deterministically by Swiss "
        "Ephemeris and classical tables — never calculate or invent any yourself. "
        "Omit 'dob' to use the owner's saved profile. For kundali, birth_time and "
        "birth_place are required; if the saved profile has them, omit both. "
        "Set 'save'=true when asked to REMEMBER birth details. Afterwards, present "
        "the report/JSON conversationally and warmly in the user's language — you "
        "are the astrologer; this tool is your calculator."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mode": {
                "type": "STRING",
                "description": (
                    "kundali (full Vedic birth chart) | matching (36-guna) | "
                    "reading (numerology summary, default) | panchang (day "
                    "almanac) | horoscope (personal daily)"
                ),
            },
            "dob": {
                "type": "STRING",
                "description": (
                    "Date of birth in any common format ('14 March 1992', "
                    "'14-03-1992'). Omit to use the saved profile."
                ),
            },
            "date": {
                "type": "STRING",
                "description": (
                    "For panchang/horoscope: the target day ('today', "
                    "date string). Defaults to today."
                ),
            },
            "birth_time": {
                "type": "STRING",
                "description": (
                    "Birth time HH:MM (24h, '8:15 PM' also accepted). Required "
                    "for kundali/matching; optional for reading/horoscope."
                ),
            },
            "birth_place": {
                "type": "STRING",
                "description": (
                    "Birth city (e.g. 'Khaga, Uttar Pradesh'), or 'lat,lon'. "
                    "Required for kundali/matching; recorded in the profile."
                ),
            },
            "latitude": {
                "type": "NUMBER",
                "description": "Explicit birth latitude (overrides birth_place)",
            },
            "longitude": {
                "type": "NUMBER",
                "description": "Explicit birth longitude (overrides birth_place)",
            },
            "timezone": {
                "type": "STRING",
                "description": (
                    "IANA timezone (e.g. 'Asia/Kolkata'). Defaults to the place "
                    "lookup, else IST."
                ),
            },
            "tz_offset": {
                "type": "NUMBER",
                "description": (
                    "Fixed UTC offset in hours (e.g. 5.5) when no IANA timezone "
                    "is available."
                ),
            },
            "true_node": {
                "type": "BOOLEAN",
                "description": "Use the true lunar node instead of the mean node",
            },
            "detail": {
                "type": "STRING",
                "description": (
                    "kundali detail: 'summary' (default, light JSON + full report) "
                    "or 'full' (all 16 vargas and full Ashtakavarga tables)"
                ),
            },
            "as_of": {
                "type": "STRING",
                "description": (
                    "For kundali: date used for the running dasha and transits "
                    "(YYYY-MM-DD, DD-MM-YYYY, or 'today'). Defaults to today."
                ),
            },
            "partner_dob": {
                "type": "STRING",
                "description": "Matching mode: the other person's date of birth",
            },
            "partner_birth_time": {
                "type": "STRING",
                "description": "Matching mode: the other person's birth time",
            },
            "partner_birth_place": {
                "type": "STRING",
                "description": "Matching mode: the other person's birth place",
            },
            "partner_name": {
                "type": "STRING",
                "description": "Matching mode: the other person's name",
            },
            "gender": {
                "type": "STRING",
                "description": "'male' or 'female' — needed only for the Kua number",
            },
            "name": {
                "type": "STRING",
                "description": "Full name, optional — for the Chaldean name number",
            },
            "save": {
                "type": "BOOLEAN",
                "description": "true to remember these birth details as the owner profile",
            },
        },
        "required": [],
    },
    "handler": astro_action,
}
