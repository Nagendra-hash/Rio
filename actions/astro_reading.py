import json

from core import astro_engine
from memory.config_manager import get_birth_profile, save_birth_profile


def astro_action(parameters: dict, player=None, session_memory=None) -> str:
    """Compute a full reading, today's panchang, or a personal daily horoscope.
    If the user asks to REMEMBER their details ('remember my DOB as ...'),
    flip ``save`` and the profile is persisted for the system-prompt injection."""
    p = parameters or {}
    mode = (p.get("mode") or "reading").strip().lower()

    if mode == "panchang":
        from core.astro_engine import panchang, _parse_hhmm, parse_dob as _pd
        import datetime as _dt
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
                msg = ("For a personal horoscope I need a date of birth, sir — "
                       "tell me yours, or say 'remember my date of birth is ...' "
                       "once and I'll remember it.")
                _log(msg, player)
                return msg
            yy, mm2, dd = astro_engine.parse_dob(prof["dob"]) or (None, None, None)
            if not (yy and mm2 and dd):
                msg = "The saved birth date looks malformed, sir — tell me your date of birth again."
                _log(msg, player)
                return msg
        result = daily_horoscope(
            yy, mm2, dd, gender=(p.get("gender") or "").strip(),
            birth_time=(p.get("birth_time") or (get_birth_profile().get("birth_time") if not dob_raw else "") or "").strip(),
            for_date=(p.get("date") or "").strip())
        _log(f"horoscope for {dd:02d}-{mm2:02d}-{yy:04d}", player)
        return json.dumps(result, ensure_ascii=False)
    save = bool(p.get("save"))

    dob_raw = (p.get("dob") or "").strip()
    y = m = d = None
    if dob_raw:
        parsed = astro_engine.parse_dob(dob_raw)
        if not parsed:
            msg = ("I couldn't parse that date of birth, sir. "
                   "Try '14 March 1992' or '14-03-1992'.")
            _log(msg, player)
            return msg
        y, m, d = parsed
    else:
        # No DOB in the request → use the saved owner profile when present.
        prof = get_birth_profile()
        if not prof.get("dob"):
            msg = ("Whose birth details should I use, sir? "
                   "Give me a date of birth — for example, "
                   "'astrology for 14 March 1992' — or say "
                   "'remember my date of birth is ...' to make it permanent.")
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
        for k_src, k_dst in (("birth_time", "birth_time"), ("birth_place", "birth_place"),
                             ("gender", "gender"), ("name", "name")):
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
        y, m, d, gender=gender or "", birth_time=birth_time,
        birth_place=birth_place, name=name)

    if save:
        try:
            save_birth_profile({
                "dob": f"{y:04d}-{m:02d}-{d:02d}",
                "birth_time": birth_time, "birth_place": birth_place,
                "gender": gender or "", "name": name,
            })
            reading["profile_saved"] = True
        except Exception as e:
            reading["profile_saved"] = False
            reading["profile_save_error"] = str(e)

    _log(f"reading for {d:02d}-{m:02d}-{y:04d}"
         + (f" (saved profile)" if save else ""), player)
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
        "Astrology & numerology expert tool with three modes. "
        "mode='reading' (default): exact birth reading from a date of birth — "
        "Moolank, Bhagyank, Lo Shu grid, Kua, name number, Vedic sun/moon sign, "
        "nakshatra+pada, tithi, yoga, Western sun sign, Chinese zodiac, gems. "
        "mode='panchang': today's (or any day's) Vedic almanac — tithi, nakshatra, "
        "yoga, karana, sunrise/sunset, Rahu Kaal window, Abhijit muhurta. "
        "mode='horoscope': the user's personal daily forecast — personal-day "
        "number theme + moon-transit (Gochara) tone vs their natal moon + today's "
        "panchang cautions. ALL numbers are computed deterministically — never "
        "calculate or invent any yourself. "
        "Route here for ANY astrology/numerology/panchang/horoscope/kundali ask. "
        "Omit 'dob' in reading/horoscope modes to use the owner's saved profile. "
        "Set 'save'=true when asked to REMEMBER birth details. Afterwards, present "
        "the JSON conversationally and warmly in the user's language — you are the "
        "astrologer; this tool is your calculator."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mode": {
                "type": "STRING",
                "description": ("reading (birth chart/numerology, default) | "
                                "panchang (day almanac) | horoscope (personal daily)")
            },
            "dob": {
                "type": "STRING",
                "description": ("Date of birth in any common format ('14 March 1992', "
                                "'14-03-1992'). Omit to use the saved profile.")
            },
            "date": {
                "type": "STRING",
                "description": ("For panchang/horoscope: the target day ('today', "
                                "date string). Defaults to today.")
            },
            "birth_time": {"type": "STRING", "description": "Birth time HH:MM (24h), optional"},
            "birth_place": {"type": "STRING", "description": "Birth place, optional (recorded in profile)"},
            "gender": {"type": "STRING", "description": "'male' or 'female' — needed only for the Kua number"},
            "name": {"type": "STRING", "description": "Full name, optional — for the Chaldean name number"},
            "save": {"type": "BOOLEAN", "description": "true to remember these birth details as the owner profile"},
        },
        "required": [],
    },
    "handler": astro_action,
}
