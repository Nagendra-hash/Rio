"""
orchestrator/agents/study/learning.py — goals, the ladder, mastery, difficulty.

Deterministic, stdlib-only learning mechanics shared by plan / quiz /
progress / adapt:

  * parse_goal()      → (subject, target, level) from a one-line ask
  * mastery_score()   → weighted share of graded attempts the learner got
                        right, recency-weighted so stale success counts less
  * next_level()      → the adaptive-difficulty dial: score ≥ .8 promotes,
                        < .5 demotes, else holds; the decision is returned
                        with its reason so the reply can be audited
  * plan_for_goal()   → a dated study plan sized to the days-to-deadline,
                        splitting the subject into topic sessions with late
                        days reserved for revision (deterministic)
  * streak_days()     → consecutive days with at least one graded attempt

Nothing here reads or writes the store; pure functions + small data tables.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

LEVELS = ("beginner", "intermediate", "advanced")

# The five-box ladder — after a pass at box n, review in n days' time.
BOX_INTERVALS_DAYS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}
BOX_LABELS = {1: "just learned", 2: "familiar", 3: "comfortable",
              4: "nearly mastered", 5: "mastered"}

MASTERY_PASS = 0.60          # share of correct graded attempts = "on track"
ADAPT_PROMOTE = 0.80         # ≥ this → level up
ADAPT_DEMOTE = 0.50          # < this → level down
MIN_ATTEMPTS_BEFORE_ADAPT = 3   # a graded sample smaller than this is noise

_STOP = {"learn", "study", "revise", "revision", "help", "teach", "me", "i",
         "want", "to", "for", "the", "my", "please", "goal", "a", "and",
         "about", "in", "on", "at", "by", "with", "prepare", "write", "make",
         "build", "new", "set", "some", "more", "basic", "beginner",
         "intermediate", "advanced", "exam", "exams", "topic", "topics",
         "understand", "improve", "practice"}


def parse_goal(text: str) -> dict:
    """(subject, level) from a one-line ask like 'i want to learn linear
    algebra from basics for an exam in march'.

    Returns {subject, level, hint} — subject '' when nothing extractable
    (the caller asks for input in that case), level from explicit words
    else beginner.
    """
    raw = (text or "").strip()
    low = raw.lower()
    level = "beginner"
    for lv, words in (("advanced", r"\b(?:advanced|expert|master)\b"),
                      ("intermediate", r"\bintermediate\b")):
        if re.search(words, low):
            level = lv
            break
    m = re.search(
        r"\b(?:learn(?:ing)?|study(?:ing)?|revi[sz]e|master\s|understand)\s+",
        low)
    tail = low[m.end():] if m else low
    tail = re.sub(
        r"\b(?:from|for|in\s+(?:the\s+)?(?:morning|evening)|in\s+\w+)\s+.*$"
        r"|\bfor\s+|\bin\b|\b(?:asap|please)\b", " ", tail).strip()
    words = [w for w in re.findall(r"[a-z0-9+#.]+", tail) if w not in _STOP]
    if words and len(words) <= 8:
        subject = " ".join(words)
    else:
        subject = ""
    return {"subject": subject, "level": level,
            "hint": raw[:120]}


def mastery_score(results: list[dict], today: date | None = None) -> dict:
    """The mastery record for one goal from its graded attempts.

    Weighted success ratio (recent attempts weigh 2, older 1), plus the
    simple streak of consecutive-correct at the END of the history used by
    the ladder. Empty history → score None with an honest "no attempts".
    """
    rows = list(results or [])
    today = today or date.today()
    recent_cutoff = today - timedelta(days=14)
    score, tried, correct = 0.0, 0, 0
    for r in rows:
        got = bool(r.get("passed"))
        weight = 2.0 if _parse_day(r.get("at")) and \
            _parse_day(r.get("at")) >= recent_cutoff else 1.0
        tried += weight
        if got:
            correct += weight
    score = round(correct / tried, 4) if tried else None
    streak = 0
    for r in reversed(rows):
        if bool(r.get("passed")):
            streak += 1
        else:
            break
    counts = {"attempts": len(rows),
              "passed": sum(1 for r in rows if r.get("passed"))}
    return {"score": score, "streak_correct": streak,
            "attempts": counts["attempts"], "passed": counts["passed"]}


def next_level(level: str, mastery: dict) -> dict:
    """The adaptive-difficulty decision: promote / hold / demote / need-more.

    Deterministic and inspectable: {level, decision, reason}. A mastery
    score that does not exist yet (fewer than MIN_ATTEMPTS attempts) never
    moves the dial — the answer names what is missing instead.
    """
    cur = (level or "beginner").lower()
    if cur not in LEVELS:
        cur = "beginner"
    if mastery.get("attempts", 0) < MIN_ATTEMPTS_BEFORE_ADAPT:
        return {"level": cur, "decision": "need-more",
                "reason": f"only {mastery.get('attempts', 0)} graded "
                          f"attempt(s) — need {MIN_ATTEMPTS_BEFORE_ADAPT} "
                          "before adapting"}
    score = mastery.get("score") or 0.0
    if score >= ADAPT_PROMOTE:
        idx = min(LEVELS.index(cur) + 1, len(LEVELS) - 1)
        nxt = LEVELS[idx]
        return {"level": nxt, "decision": "promote" if nxt != cur else "hold",
                "reason": f"mastery {score:.2f} ≥ {ADAPT_PROMOTE}"}
    if score < ADAPT_DEMOTE:
        idx = max(LEVELS.index(cur) - 1, 0)
        nxtd = LEVELS[idx]
        return {"level": nxtd, "decision": "demote" if nxtd != cur else "hold",
                "reason": f"mastery {score:.2f} < {ADAPT_DEMOTE}"}
    return {"level": cur, "decision": "hold",
            "reason": f"mastery {score:.2f} inside the keep band"}


def plan_for_goal(subject: str, level: str, deadline: str,
                  today: date | None = None) -> dict:
    """A deterministic dated plan: one session per day — teach day, drill
    day, alternating — with the last three days reserved for revision and
    a final mock quiz (when the window allows). Days to deadline; an
    already-reached deadline (0/1 days) collapses to the single today
    session."""
    today = today or date.today()
    sub = (subject or "").strip() or "your topic"
    dl = _parse_day(deadline)
    days = max(0, (dl - today).days) if dl else 7
    if days <= 1:
        return {"subject": sub, "level": (level or "beginner").lower(),
                "deadline": _fmt(dl) if dl else "", "days": 1,
                "items": [{"id": "s1", "date": _fmt(today),
                           "kind": "teach", "topic": sub, "minutes": 45}]}
    days = min(days, 30)
    items: list[dict] = []
    day = today
    items.append({"id": "s1", "date": _fmt(day), "kind": "teach",
                  "topic": sub, "minutes": 45})
    for i in range(2, days - 2):
        kind = "drill" if (i % 2 == 0) else "recap"
        items.append({"id": f"s{i}", "date": _fmt(day + timedelta(days=i - 1)),
                      "kind": kind,
                      "topic": f"{sub} ({kind})" if kind == "recap" else sub,
                      "minutes": 30})
    for j in range(max(days - 3, 2), days):
        items.append({"id": f"s{j}", "date": _fmt(day + timedelta(days=j - 1)),
                      "kind": "revise", "topic": f"{sub} — revision",
                      "minutes": 30})
    items.append({"id": f"s{days}", "date": _fmt(day + timedelta(days=days - 1)),
                  "kind": "quiz", "topic": f"{sub} — final check",
                  "minutes": 30})
    return {"subject": sub, "level": (level or "beginner").lower(),
            "deadline": _fmt(dl) if dl else "", "days": days,
            "items": items}


def level_guidance(level: str) -> str:
    return {"beginner": "definitions first, small worked examples",
            "intermediate": "mix concepts, multi-step problems",
            "advanced": "edge cases, proofs, timed drills"}.get(
        (level or "beginner").lower(),
        "definitions first, small worked examples")


# ── helpers ───────────────────────────────────────────────────────────────

def _parse_day(value) -> date | None:
    try:
        if not value:
            return None
        s = str(value)[:10]
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _fmt(d: date | None) -> str:
    return d.strftime("%Y-%m-%d") if d else ""


def streak_days(results: list[dict], today: date | None = None) -> int:
    """Distinct-day streak of graded attempts ending today/yesterday."""
    today = today or date.today()
    days = sorted({r.get("at", "")[:10] for r in (results or []) if r.get("at")},
                  reverse=True)
    if not days:
        return 0
    try:
        last = datetime.strptime(days[0], "%Y-%m-%d").date()
    except ValueError:
        return 0
    if (today - last).days > 1:
        return 0
    count = 1
    for prev, cur in zip(days, days[1:]):
        try:
            gap = (datetime.strptime(prev, "%Y-%m-%d") -
                   datetime.strptime(cur, "%Y-%m-%d")).days
        except ValueError:
            break
        if gap != 1:
            break
        count += 1
    return count
