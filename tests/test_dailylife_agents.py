"""
tests/test_dailylife_agents.py — offline acceptance tests for the Phase 13
daily-life specialists (Briefing / Meal / Travel).

NO network, NO LLM, NO real Google tokens, NO live prices: JARVIS_HOME
points at a fresh temp dir, the toolbox gets injectable fake backends, and
the Google/market services are fakes. Token material is injected into
payloads to prove the redaction boundary holds.

What this locks in:
  1. TOOLBOX    — permitted web search/fetch/KB wiring, calendar/email
                  reads via the shared google_tools, provenance-labeled
                  market quotes, and HONEST ok=False on every failure.
  2. BRIEFING   — assembly order, honesty labels, audio-ready render,
                  top-email urgency ranking, tasks in/out.
  3. MEAL       — diet/allergy filtering, deterministic rotation, use-up
                  flags, grocery = menu − pantry, budget from the user's
                  OWN price hints (never live), expiry report.
  4. TRAVEL     — deep links, ranking over SUPPLIED rows only (missing
                  fields are never guessed), itinerary, watch deltas need
                  two live observations, checks say so without a source.
  5. STORE      — 0600 files in a 0700 dir, redaction on write,
                  actions-only audit, caps, wipe.
  6. CHECKPOINT — snapshots, prune, perms, redaction, never raises.
  7. AGENTS     — full flows: briefing with fake sources, prefs, tasks,
                  menu/grocery/pantry/expiry, travel search/route/
                  itinerary/watches; never raise on garbage input.
  8. SECURITY   — tokens never appear in any reply or on disk.
  9. WIRING     — classifier lanes + explicit routes; registry stubs off
                  by default, real agents behind JARVIS_*_AGENT=on.

Run:  .venv/bin/python -m pytest tests/test_dailylife_agents.py -q
"""
import itertools
import json
import os
import stat
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ── isolation: this module gets a fresh JARVIS_HOME ──────────────────────────
_TMP = tempfile.mkdtemp(prefix="jarvis-dailylife-")
os.environ["JARVIS_HOME"] = _TMP

from orchestrator.agents.dailylife import briefing_logic, meal_logic  # noqa: E402
from orchestrator.agents.dailylife import travel_logic                 # noqa: E402
from orchestrator.agents.dailylife.briefing_agent import BriefingAgent # noqa: E402
from orchestrator.agents.dailylife.checkpoint import (                 # noqa: E402
    DailyLifeCheckpoint)
from orchestrator.agents.dailylife.meal_agent import MealAgent         # noqa: E402
from orchestrator.agents.dailylife.store import DailyLifeStore         # noqa: E402
from orchestrator.agents.dailylife.toolbox import DailyLifeToolbox     # noqa: E402
from orchestrator.agents.dailylife.travel_agent import TravelAgent     # noqa: E402
from orchestrator.agents.productivity import security                  # noqa: E402
from orchestrator.agents import build_default_registry                 # noqa: E402
from orchestrator.classifier import IntentClassifier                   # noqa: E402
from orchestrator.contracts import AgentRequest, TaskStatus, TaskType  # noqa: E402

_SEQ = itertools.count(1)


def _req(text: str = "", ttype: TaskType = TaskType.BRIEFING,
         **meta) -> AgentRequest:
    return AgentRequest(task_id=f"dailylife-test-{next(_SEQ)}",
                        task_type=ttype, text=text, metadata=meta)


@pytest.fixture()
def tmp():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture()
def store(tmp):
    return DailyLifeStore(tmp / "dl")


@pytest.fixture()
def ckpt(tmp):
    return DailyLifeCheckpoint(tmp / "ckpt")


def ok_search(query, max_results):
    """Fake permitted search: always 'succeeds' with one canned hit."""
    return [{"title": f"About {query}", "snippet": "sunny, 30C, light winds",
             "url": "https://example.com/hit"}]


def dead_search(query, max_results):
    raise RuntimeError("backend down")


# ══════════════════════════════════════════════════════ 1. toolbox ══════════

class FakeCalendar:
    def events(self):
        class _E:
            def list(self, **kw):
                return _Req({"items": [{
                    "id": "ev1", "summary": "Standup",
                    "start": {"dateTime": "2026-10-08T10:00:00+05:30"},
                    "end": {"dateTime": "2026-10-08T10:30:00+05:30"}}]})
        return _E()


class FakeGmail:
    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kw):
        return _Req({"messages": [{"id": "m1"}]})

    def get(self, **kw):
        return _Req({"id": "m1", "threadId": "m1", "snippet": "hi",
                     "internalDate": "0",
                     "payload": {"headers": [
                         {"name": "From", "value": "a@b.com"},
                         {"name": "Subject", "value": "Test"}]}})


class _Req:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return self._data


class FakeMarket:
    def quote(self, symbol):
        from orchestrator.agents.trading.data import DataResult, Provenance
        return DataResult(ok=True, provenance=Provenance.DELAYED,
                          source="fake", data={"symbol": symbol, "last": 101.5,
                                               "previous_close": 100.0})


class TestToolbox:
    def test_web_search_ok_and_labeled(self):
        tb = DailyLifeToolbox(search_fn=ok_search)
        res = tb.web_search("weather in Pune today")
        assert res["ok"] is True and res["queried_at"]
        assert res["results"][0]["url"] == "https://example.com/hit"

    def test_web_search_failure_is_honest(self):
        tb = DailyLifeToolbox(search_fn=dead_search)
        res = tb.web_search("anything")
        assert res["ok"] is False and res["results"] == []
        assert "unavailable" in res["error"]

    def test_web_search_never_leaks_private_urls(self, monkeypatch):
        # DNS-independent: fake resolution so the loopback check still fires.
        import socket as _socket
        from orchestrator.agents import research_tools as rt

        def fake_getaddrinfo(host, *a, **kw):
            if host == "127.0.0.1":
                return [(0, 0, 0, "", ("127.0.0.1", 0))]
            return [(0, 0, 0, "", ("93.184.216.34", 0))]
        monkeypatch.setattr(_socket, "getaddrinfo", fake_getaddrinfo)
        tb = DailyLifeToolbox(search_fn=lambda q, k: [
            {"title": "x", "snippet": "y", "url": "http://127.0.0.1:9/x"},
            {"title": "z", "snippet": "w", "url": "https://ok.example.com"}])
        res = tb.web_search("q")
        urls = [r["url"] for r in res["results"]]
        assert urls == ["https://ok.example.com"]

    def test_calendar_reads_through_shared_google_tools(self):
        tb = DailyLifeToolbox(calendar_factory=FakeCalendar)
        res = tb.calendar_events(datetime(2026, 10, 8, 0, 0), days=1)
        assert res["ok"] is True
        assert res["events"][0]["summary"] == "Standup"

    def test_calendar_without_source_is_honest(self):
        res = DailyLifeToolbox().calendar_events(datetime(2026, 10, 8))
        assert res["ok"] is False
        assert "no calendar source" in res["error"]

    def test_email_reads_through_shared_google_tools(self):
        tb = DailyLifeToolbox(gmail_factory=FakeGmail)
        res = tb.unread_email()
        assert res["ok"] is True and res["messages"][0]["id"] == "m1"

    def test_market_quotes_carry_provenance(self):
        tb = DailyLifeToolbox(market=FakeMarket())
        res = tb.market_snapshot(["NIFTY"])
        q = res["quotes"][0]
        assert res["ok"] is True
        assert q["provenance"] == "delayed" and q["source"] == "fake"
        assert q["data"]["last"] == 101.5

    def test_market_without_source_never_invents(self):
        res = DailyLifeToolbox().market_snapshot(["NIFTY"])
        assert res["ok"] is False and res["quotes"] == []
        assert "no market source" in res["error"]

    def test_market_failure_per_symbol_is_labeled_unavailable(self):
        class Boom:
            def quote(self, symbol):
                raise RuntimeError("nope")
        res = DailyLifeToolbox(market=Boom()).market_snapshot(["X"])
        q = res["quotes"][0]
        assert q["ok"] is False and q["provenance"] == "unavailable"


# ═════════════════════════════════════════ 2. briefing logic ════════════════

class TestBriefingLogic:
    def _assemble(self, tb, store, **kw):
        return briefing_logic.assemble(
            tb, store, city="Pune", topics=["AI"],
            symbols=["NIFTY"], **kw)

    def test_sections_honest_when_no_sources(self, store):
        data = self._assemble(DailyLifeToolbox(search_fn=dead_search), store,
                              market_enabled=False)
        assert data["calendar"]["ok"] is False
        assert data["email"]["ok"] is False
        assert data["weather"]["ok"] is False
        assert data["news"][0]["ok"] is False
        assert data["market"]["ok"] is None          # disabled, not failed
        text = briefing_logic.render_briefing(data)
        assert "unavailable" in text
        assert "backend down" in text          # the honest reason travels
        assert "Unverified sections say so explicitly" in text

    def test_live_labels_only_when_search_ran(self, store):
        data = self._assemble(DailyLifeToolbox(search_fn=ok_search), store,
                              market_enabled=False)
        text = briefing_logic.render_briefing(data)
        assert "live web search" in text
        assert "sunny" in text

    def test_audio_variant_is_flat_and_speakable(self, store):
        data = self._assemble(DailyLifeToolbox(search_fn=ok_search), store,
                              market_enabled=False)
        audio = briefing_logic.render_briefing(data, audio=True)
        assert "•" not in audio and "─" not in audio
        assert audio.lower().startswith("good day")

    def test_email_urgency_ranking(self, store):
        tb = DailyLifeToolbox(search_fn=dead_search,
                              gmail_factory=_FakeGmailUrgent)
        data = self._assemble(tb, store, market_enabled=False)
        msgs = data["email"]["messages"]
        assert msgs[0]["urgency"] >= msgs[-1]["urgency"]
        assert any(m["urgency"] == 3 for m in msgs)

    def test_market_disabled_vs_unavailable(self, store):
        disabled = self._assemble(DailyLifeToolbox(search_fn=dead_search),
                                  store, market_enabled=False)
        assert disabled["market"]["ok"] is None
        failed = self._assemble(DailyLifeToolbox(search_fn=dead_search),
                                store, market_enabled=True)
        assert failed["market"]["ok"] is False


class _FakeGmailUrgent:
    """Two unread mails: one urgent, one routine (order shuffled)."""

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kw):
        return _Req({"messages": [{"id": "m2"}, {"id": "m1"}]})

    def get(self, **kw):
        msgs = {
            "m1": ("Digest <news@bulk.example.com>", "Weekly digest #9",
                   "View in browser. Unsubscribe any time."),
            "m2": ("Boss <boss@corp.example.com>", "Outage — urgent",
                   "Please fix immediately."),
        }
        frm, subject, body = msgs[kw["id"]]
        return _Req({"id": kw["id"], "threadId": kw["id"],
                     "snippet": body[:100], "internalDate": "0",
                     "payload": {"headers": [
                         {"name": "From", "value": frm},
                         {"name": "Subject", "value": subject},
                         {"name": "List-Unsubscribe",
                          "value": "<https://x/unsub>" if "m1" == kw["id"]
                          else ""}]}})


# ═════════════════════════════════════════════ 3. meal logic ════════════════

PANTRY = [
    {"id": "p1", "name": "rice", "qty": 2.0, "unit": "kg",
     "expires": (date.today() + timedelta(days=1)).isoformat()},
    {"id": "p2", "name": "poha", "qty": 500.0, "unit": "g", "expires": ""},
]


class TestMealLogic:
    def test_veg_menu_never_serves_meat(self):
        menu = meal_logic.build_menu({"diet": "veg"}, PANTRY)
        names = {m["name"] for d in menu["days"]
                 for m in d["meals"].values()}
        assert "Chicken curry + rice" not in names
        assert "Egg fried rice" not in names

    def test_vegan_menu_stricter_than_veg(self):
        vegan = {m["name"] for d in
                 meal_logic.build_menu({"diet": "vegan"}, [])
                 ["days"] for m in d["meals"].values()}
        assert "Oats bowl" not in vegan            # milk is dairy
        assert "Poha" in vegan

    def test_dietary_gap_is_honest(self):
        menu = meal_logic.build_menu({"diet": "vegan"}, [])
        names = {m["name"] for d in menu["days"]
                 for m in d["meals"].values()}
        assert any("no vegan" in n for n in names)   # no invented dish

    def test_allergies_filter(self):
        menu = meal_logic.build_menu({"diet": "veg",
                                      "allergies": ["dairy"]}, [])
        names = {m["name"] for d in menu["days"]
                 for m in d["meals"].values()}
        assert "Oats bowl" not in names
        assert "Paneer bhurji + roti" not in names

    def test_deterministic_rotation(self):
        a = meal_logic.build_menu({"diet": "veg"}, [],
                                  start=date(2026, 10, 1),
                                  today=date(2026, 10, 1))
        b = meal_logic.build_menu({"diet": "veg"}, [],
                                  start=date(2026, 10, 1),
                                  today=date(2026, 10, 1))
        assert a == b

    def test_overrides_win(self):
        menu = meal_logic.build_menu(
            {"diet": "veg"}, [], start=date(2026, 10, 1),
            overrides={"0": {"dinner": "Pasta in tomato sauce"}})
        assert menu["days"][0]["meals"]["dinner"]["name"] \
            == "Pasta in tomato sauce"

    def test_use_up_flags_for_expiring_pantry(self):
        menu = meal_logic.build_menu({"diet": "veg"}, PANTRY,
                                     today=date.today())
        ups = {i for d in menu["days"] for m in d["meals"].values()
               for i in m["use_up"]}
        assert "rice" in ups

    def test_grocery_excludes_pantry_items(self):
        menu = meal_logic.build_menu({"diet": "veg"}, PANTRY,
                                     today=date.today())
        g = meal_logic.grocery_list(menu, PANTRY)
        buy_names = {i["name"] for i in g["buy"]}
        pantry_names = {i["name"] for i in g["items"] if i["in_pantry"]}
        assert "poha" in pantry_names and "poha" not in buy_names

    def test_budget_from_own_price_hints_only(self):
        menu = meal_logic.build_menu({"diet": "veg"}, [],
                                     today=date.today())
        g = meal_logic.grocery_list(menu, [], prices={"poha": 40.0,
                                                      "oats": 90.0},
                                    budget=100.0)
        text = meal_logic.render_grocery(g)
        assert "your saved price hints" in text
        assert "no live prices queried" in text
        assert g["priced_items"] <= len(g["buy"])

    def test_expiry_report_buckets(self):
        rep = meal_logic.expiry_report(PANTRY, today=date.today())
        assert any(r["name"] == "rice" for r in rep["soon"])
        assert any(r["name"] == "poha" for r in rep["no_date"])

    def test_grocery_qty_parsing_and_merging(self):
        menu = {"start": "2026-10-01", "days": [
            {"date": "2026-10-01", "meals": {
                "breakfast": {"name": "Poha", "minutes": 20,
                              "ingredients": {"poha": "200 g",
                                              "onion": "1"},
                              "pantry_hits": [], "use_up": []},
                "lunch": {"name": "Poha again", "minutes": 20,
                          "ingredients": {"poha": "100 g"},
                          "pantry_hits": [], "use_up": []},
                "dinner": {"name": "X", "minutes": 20,
                           "ingredients": {}, "pantry_hits": [],
                           "use_up": []}}}]}
        g = meal_logic.grocery_list(menu, [])
        row = next(i for i in g["items"] if i["name"] == "poha")
        assert row["qty"] == "300 g"


# ═════════════════════════════════════════ 4. travel logic ══════════════════

class TestTravelLogic:
    def test_deep_links_cover_all_modes(self):
        links = travel_logic.deep_links("Delhi", "Goa", "2026-10-20", "",
                                        2, "business")
        assert set(links) == {"flights", "trains", "buses", "road",
                              "hotels", "tourism"}
        assert "adults=2" in links["flights"]
        assert "cabin=3" in links["flights"]          # business
        assert "origin=Delhi" in links["road"]

    def test_links_quote_special_characters(self):
        links = travel_logic.deep_links("New York", "Kota Kinabalu",
                                        "2026-10-20")
        assert "New+York" in links["flights"]
        assert "Kota+Kinabalu" in links["hotels"]

    def test_ranking_uses_only_supplied_fields(self):
        rank = travel_logic.rank_options([
            {"mode": "flight", "minutes": 95, "cost": 4200},
            {"mode": "train", "minutes": 420},
            {"mode": "bus"},
        ])
        assert rank["fastest"]["mode"] == "flight"
        assert rank["cheapest"]["mode"] == "flight"
        assert [r["mode"] for r in rank["unranked_time"]] == ["bus"]
        assert [r["mode"] for r in rank["unranked_cost"]] == ["train", "bus"]

    def test_ranking_never_invents(self):
        rank = travel_logic.rank_options([{"mode": "bus"}])
        assert rank["fastest"] is None and rank["cheapest"] is None
        assert "no live source" in rank["source_note"]

    def test_itinerary_days_and_ordering(self):
        plan = travel_logic.build_itinerary("2026-10-20", 2, {
            "1": [{"time": "18:00", "activity": "Sunset"}],
            "0": [{"time": "09:00", "activity": "Beach"},
                  {"time": "14:00", "activity": "Museum"}],
        })
        assert len(plan["days"]) == 2
        day0 = [i["activity"] for i in plan["days"][0]["items"]]
        assert day0 == ["Beach", "Museum"]
        assert plan["days"][1]["items"][0]["activity"] == "Sunset"

    def test_itinerary_caps_days(self):
        plan = travel_logic.build_itinerary("2026-10-20", 99, {})
        assert len(plan["days"]) == 14

    def test_watch_delta_needs_two_live_observations(self):
        first = travel_logic.watch_delta("price", None, 4200)
        assert first["ok"] is False and "two live" in first["reason"]
        second = travel_logic.watch_delta("price", 4200, 3900)
        assert second["ok"] is True and second["direction"] == "down"
        assert second["pct"] == -7.14

    def test_render_search_never_claims_live_without_source(self):
        links = travel_logic.deep_links("A", "B", "2026-10-20")
        text = travel_logic.render_search("A", "B", "2026-10-20", "",
                                          links, {"ok": False,
                                                  "results": [],
                                                  "error": "down"})
        assert "I have NOT queried" in text
        assert "No live search ran" in text
        assert "flights" in text and "hotels" in text

    def test_render_search_labels_live_results(self):
        links = travel_logic.deep_links("A", "B", "2026-10-20")
        text = travel_logic.render_search(
            "A", "B", "2026-10-20", "", links,
            {"ok": True, "results": [{"title": "Cheap fares",
                                       "snippet": "4,200",
                                       "url": "https://s.example"}],
             "queried_at": "2026-10-07T08:00:00+05:30", "error": ""})
        assert "Live web search at 08:00" in text
        assert "Cheap fares" in text

    def test_render_route_reports_source(self):
        rank = travel_logic.rank_options(
            [{"mode": "flight", "minutes": 90, "cost": 4000}])
        text = travel_logic.render_route(rank)
        assert "supplied rows only" in text


# ═════════════════════════════════════════════════ 5. store ═════════════════

class TestStore:
    def test_permissions_owner_only(self, tmp):
        s = DailyLifeStore(tmp / "dl")
        s.add_task("x")
        s.pantry_add("rice")
        assert stat.S_IMODE(s.root.stat().st_mode) == 0o700
        for f in s.root.iterdir():
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, f.name

    def test_prefs_roundtrip(self, tmp):
        s = DailyLifeStore(tmp / "dl")
        s.prefs_set("briefing", {"city": "Pune"})
        s.prefs_set("briefing", {"topics": ["AI"]})
        assert s.prefs_get("briefing") == {"city": "Pune", "topics": ["AI"]}
        assert s.prefs_get("meal", default={"diet": "any"}) == {"diet": "any"}

    def test_task_lifecycle(self, tmp):
        s = DailyLifeStore(tmp / "dl")
        row = s.add_task("pay bill", due="2026-10-09")
        hit = s.complete_task(row["id"])
        assert hit["done"] is True
        assert s.complete_task(row["id"]) is None      # already done
        assert s.complete_task("no such task") is None
        assert s.clear_done_tasks() == 1

    def test_pantry_merge_and_take(self, tmp):
        s = DailyLifeStore(tmp / "dl")
        s.pantry_add("Rice", qty=1.0, unit="kg", expires="2026-10-20")
        row = s.pantry_add("rice", qty=0.5)
        assert row["qty"] == 1.5 and row["unit"] == "kg"
        hit = s.pantry_take("RICE", qty=0.25)
        assert hit["qty"] == 1.25
        assert s.pantry_take("rice") is not None        # whole item
        assert s.pantry_take("rice") is None            # now unknown

    def test_watch_roundtrip(self, tmp):
        s = DailyLifeStore(tmp / "dl")
        w = s.add_watch({"kind": "price", "origin": "A",
                         "destination": "B", "date": "2026-10-20"})
        s.update_watch(w["id"], {"baseline": 4200})
        assert s.watches()[0]["baseline"] == 4200
        assert s.remove_watch(w["id"]) is True
        assert s.remove_watch(w["id"]) is False

    def test_menu_and_grocery_artifacts(self, tmp):
        s = DailyLifeStore(tmp / "dl")
        assert s.last_menu() is None
        s.save_menu({"days": [1, 2]})
        s.save_grocery({"items": [1]})
        assert s.last_menu()["days"] == [1, 2]
        assert s.last_grocery()["items"] == [1]

    def test_audit_records_actions_only(self, tmp):
        s = DailyLifeStore(tmp / "dl")
        marker = "top-secret-pantry-note-xyzzy"
        s.add_task(marker)
        audit = (tmp / "dl" / "audit.jsonl").read_text()
        assert "task_added" in audit
        assert marker not in audit

    def test_redaction_on_write(self, tmp):
        s = DailyLifeStore(tmp / "dl")
        s.prefs_set("travel", {"note": "token ya29.SHOULD_NEVER_APPEAR"})
        raw = (tmp / "dl" / "prefs.json").read_text()
        assert "ya29." not in raw and "[REDACTED]" in raw

    def test_drafts_and_wipe(self, tmp):
        s = DailyLifeStore(tmp / "dl")
        s.save_draft("itinerary", {"start": "2026-10-20"})
        assert s.latest_draft("itinerary")["payload"]["start"] \
            == "2026-10-20"
        assert s.wipe()
        assert s.summary()["tasks"] == 0
        assert s.latest_draft("itinerary") is None


# ══════════════════════════════════════════════ 6. checkpoint ═══════════════

class TestCheckpoint:
    def test_save_load_prune_and_perms(self, tmp):
        cp = DailyLifeCheckpoint(tmp / "ck")
        for i in range(25):
            assert cp.save({"i": i})
        assert len(cp.list_ids()) == 20
        assert cp.load_latest()["i"] == 24
        assert stat.S_IMODE(
            (tmp / "ck" / "latest.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp / "ck").stat().st_mode) == 0o700

    def test_redacts_token_payloads(self, tmp):
        cp = DailyLifeCheckpoint(tmp / "ck")
        cp.save({"payload": {"access_token": "ya29.secrettoken123456"}})
        assert "ya29." not in (tmp / "ck" / "latest.json").read_text()

    def test_never_raises_on_broken_dir(self, tmp):
        cp = DailyLifeCheckpoint(tmp / "ck")
        blocked = tmp / "blocked"
        blocked.write_text("not a dir")
        cp.dir = blocked
        assert cp.save({"x": 1}) is None
        assert cp.load_latest() is None
        assert cp.list_ids() == []


# ═════════════════════════════════════════════ 7. security ══════════════════

class TestSecurityBoundary:
    def test_no_token_material_in_any_reply(self, tmp):
        tb = DailyLifeToolbox(search_fn=lambda q, k: [
            {"title": "token ya29.SHOULD_NEVER_APPEAR leaked",
             "snippet": "bearer abc123def456 inside",
             "url": "https://example.com/t"}])
        ba = BriefingAgent(store=DailyLifeStore(tmp / "b"),
                           checkpoint=DailyLifeCheckpoint(tmp / "c"),
                           toolbox=tb)
        for req in (_req("briefing", city="Pune", topics=["AI"]),
                    _req("add task hold ya29.SHOULD_NEVER_APPEAR safe"),
                    _req("tasks")):
            r = ba.handle(req)
            assert "ya29." not in r.text
            assert "SHOULD_NEVER_APPEAR" not in r.text
            assert "abc123def456" not in r.text


# ════════════════════════════════════ 8. agent flows ════════════════════════

@pytest.fixture()
def briefing_agent(tmp, ckpt):
    return BriefingAgent(store=DailyLifeStore(tmp / "b"), checkpoint=ckpt,
                         toolbox=DailyLifeToolbox(search_fn=ok_search))


@pytest.fixture()
def meal_agent(tmp, ckpt):
    return MealAgent(store=DailyLifeStore(tmp / "m"), checkpoint=ckpt)


@pytest.fixture()
def travel_agent(tmp, ckpt):
    return TravelAgent(store=DailyLifeStore(tmp / "t"), checkpoint=ckpt,
                       toolbox=DailyLifeToolbox(search_fn=ok_search))


class TestBriefingAgent:
    def test_full_briefing_with_fake_sources(self, briefing_agent):
        r = briefing_agent.handle(_req("morning briefing", city="Pune",
                                       topics=["AI"], market_enabled=False))
        assert r.status is TaskStatus.COMPLETED
        assert r.text.startswith("Daily briefing")
        assert "live web search" in r.text
        assert r.data["audio"].lower().startswith("good day")
        assert r.data["briefing"]["weather_ok"] is True

    def test_prefs_fallback_when_no_metadata(self, briefing_agent, tmp):
        s = DailyLifeStore(tmp / "b")
        s.prefs_set("briefing", {"city": "Nashik", "topics": ["space"],
                                 "market_enabled": False})
        r = briefing_agent.handle(_req("briefing"))
        assert "Nashik" in r.text and "space" in r.text

    def test_market_only_when_enabled(self, briefing_agent):
        r = briefing_agent.handle(_req("briefing", city="Pune",
                                       topics=[], market_enabled=False))
        assert "disabled" in r.text

    def test_task_add_done_list_flow(self, briefing_agent):
        r1 = briefing_agent.handle(_req("add task pay the electricity bill"))
        assert "Task added" in r1.text
        r2 = briefing_agent.handle(_req("done pay the electricity bill"))
        assert "Done: pay the electricity bill" in r2.text
        r3 = briefing_agent.handle(_req("tasks"))
        assert "0 open" in r3.text
        assert "[x]" in r3.text          # completed shows as done

    def test_task_done_by_id(self, briefing_agent):
        r1 = briefing_agent.handle(_req("x", tasks_add="file taxes"))
        tid = r1.data["task_id"]
        r2 = briefing_agent.handle(_req("x", task_done=tid))
        assert "Done: file taxes" in r2.text

    def test_prefs_set_and_show(self, briefing_agent):
        r = briefing_agent.handle(_req("set my briefing city",
                                       city="Nashik", topics=["cricket"],
                                       briefing_action="prefs"))
        assert "Nashik" in r.text and "cricket" in r.text
        r2 = briefing_agent.handle(_req("briefing"))     # picks up prefs
        assert "Nashik" in r2.text

    def test_menu_on_garbage_never_raises(self, briefing_agent):
        r = briefing_agent.handle(_req("tell me a joke"))
        assert r.status is TaskStatus.COMPLETED          # a briefing anyway
        for text in ("", "   ", "!!!", "x" * 5000):
            assert briefing_agent.handle(_req(text)) is not None


class TestMealAgent:
    def test_menu_build_persists_and_reuses(self, meal_agent, tmp):
        r1 = meal_agent.handle(_req("plan my weekly menu", diet="veg",
                                    ttype=TaskType.MEAL))
        assert "Weekly menu" in r1.text
        assert DailyLifeStore(tmp / "m").last_menu() is not None
        r2 = meal_agent.handle(_req("show the menu",
                                    ttype=TaskType.MEAL))
        assert r2.text == r1.text

    def test_menu_metadata_overrides_prefs(self, meal_agent, tmp):
        DailyLifeStore(tmp / "m").prefs_set("meal", {"diet": "veg"})
        r = meal_agent.handle(_req("rebuild the menu", diet="any",
                                   ttype=TaskType.MEAL))
        assert "diet: any" in r.text

    def test_pantry_flow(self, meal_agent):
        r1 = meal_agent.handle(_req("", pantry_item="rice", qty=2,
                                    unit="kg", expires="2026-10-20",
                                    meal_action="pantry_add",
                                    ttype=TaskType.MEAL))
        assert "rice" in r1.text
        r2 = meal_agent.handle(_req("used 0.5 kg rice",
                                    ttype=TaskType.MEAL))
        assert "1.5 kg left" in r2.text
        r3 = meal_agent.handle(_req("what's in the pantry",
                                    ttype=TaskType.MEAL))
        assert "Pantry (1 item(s))" in r3.text

    def test_pantry_add_needs_name(self, meal_agent):
        r = meal_agent.handle(_req("add to pantry", meal_action="pantry_add",
                                   ttype=TaskType.MEAL))
        assert r.status is TaskStatus.NEEDS_INPUT

    def test_grocery_and_budget(self, meal_agent):
        meal_agent.handle(_req("", pantry_item="rice", qty=2, unit="kg",
                               meal_action="pantry_add",
                               ttype=TaskType.MEAL))
        meal_agent.handle(_req("", diet="veg",
                               price_hints={"poha": 40, "oats": 90},
                               budget_week=500, meal_action="prefs",
                               ttype=TaskType.MEAL))
        r = meal_agent.handle(_req("make a grocery list",
                                   ttype=TaskType.MEAL))
        assert "Grocery list" in r.text
        assert "no live prices queried" in r.text
        assert "within budget" in r.text or "OVER budget" in r.text

    def test_expiry_report(self, meal_agent):
        meal_agent.handle(_req("", pantry_item="milk", qty=1, unit="l",
                               expires=(date.today()
                                        + timedelta(days=1)).isoformat(),
                               meal_action="pantry_add",
                               ttype=TaskType.MEAL))
        r = meal_agent.handle(_req("what's expiring",
                                   ttype=TaskType.MEAL))
        assert "Expiring within" in r.text and "milk" in r.text

    def test_never_raises_on_garbage(self, meal_agent):
        for text in ("", "   ", "!!!", "x" * 5000, "pantry",
                     "add task", "groceries now"):
            assert meal_agent.handle(_req(text,
                                          ttype=TaskType.MEAL)) is not None


class TestTravelAgent:
    def test_search_needs_origin_destination(self, travel_agent):
        r = travel_agent.handle(_req("find me flights",
                                     ttype=TaskType.TRAVEL))
        assert r.status is TaskStatus.NEEDS_INPUT

    def test_search_needs_date(self, travel_agent):
        r = travel_agent.handle(_req("flights from Delhi to Goa",
                                     ttype=TaskType.TRAVEL))
        assert r.status is TaskStatus.NEEDS_INPUT

    def test_search_from_text_with_live_label(self, travel_agent):
        r = travel_agent.handle(_req("flights from Delhi to Goa on "
                                     "2026-10-20", ttype=TaskType.TRAVEL))
        assert r.status is TaskStatus.COMPLETED
        assert "Live web search" in r.text
        assert r.data["live"] is True
        assert "I have NOT queried" in r.text
        assert "flights" in r.data["links"]

    def test_search_without_source_says_so(self, tmp, ckpt):
        agent = TravelAgent(store=DailyLifeStore(tmp / "t2"),
                            checkpoint=ckpt,
                            toolbox=DailyLifeToolbox(
                                search_fn=dead_search))
        r = agent.handle(_req("trains from Delhi to Jaipur on 2026-10-20",
                              ttype=TaskType.TRAVEL))
        assert r.data["live"] is False
        assert "No live search ran" in r.text
        assert "trains" in r.text and "buses" in r.text
        assert "₹" not in r.text and "$" not in r.text   # no fake prices

    def test_route_comparison_over_supplied_rows(self, travel_agent):
        r = travel_agent.handle(_req("compare routes", options=[
            {"mode": "flight", "minutes": 95, "cost": 4200},
            {"mode": "train", "minutes": 420, "cost": 900}],
            ttype=TaskType.TRAVEL))
        assert "Fastest: flight" in r.text and "Cheapest: train" in r.text
        assert r.data["fastest"] == "flight"

    def test_route_needs_rows(self, travel_agent):
        r = travel_agent.handle(_req("compare", ttype=TaskType.TRAVEL))
        assert "Give me the options" in r.text

    def test_itinerary_saved_as_draft(self, travel_agent, tmp):
        r = travel_agent.handle(_req("plan my trip", start="2026-10-20",
                                     days=2, ttype=TaskType.TRAVEL))
        assert "Itinerary from 2026-10-20" in r.text
        assert "(free day" in r.text
        assert DailyLifeStore(tmp / "t").latest_draft("itinerary")

    def test_watch_add_check_and_baseline(self, travel_agent, tmp):
        r1 = travel_agent.handle(_req("watch flights from Delhi to Goa on "
                                      "2026-10-20", ttype=TaskType.TRAVEL))
        assert r1.data["watch_id"]
        wid = r1.data["watch_id"]
        r2 = travel_agent.handle(_req("check watches", watch_id=wid,
                                      value=4200, ttype=TaskType.TRAVEL))
        assert "baseline set to 4200" in r2.text
        r3 = travel_agent.handle(_req("check watches", watch_id=wid,
                                      value=3900, ttype=TaskType.TRAVEL))
        assert "delta vs baseline: -300" in r3.text
        r4 = travel_agent.handle(_req("list my watches",
                                      watch_action="list",
                                      ttype=TaskType.TRAVEL))
        assert "baseline: 4200" in r4.text

    def test_watch_check_without_source_is_honest(self, tmp, ckpt):
        agent = TravelAgent(store=DailyLifeStore(tmp / "t3"),
                            checkpoint=ckpt,
                            toolbox=DailyLifeToolbox(
                                search_fn=dead_search))
        r = agent.handle(_req("watch flights from A to B on 2026-10-20",
                              ttype=TaskType.TRAVEL))
        r2 = agent.handle(_req("check my watch", watch_id=r.data["watch_id"],
                               ttype=TaskType.TRAVEL))
        assert "no live source" in r2.text

    def test_watch_remove(self, travel_agent):
        r = travel_agent.handle(_req("watch flights from A to B on "
                                     "2026-10-20", ttype=TaskType.TRAVEL))
        wid = r.data["watch_id"]
        r2 = travel_agent.handle(_req("remove the watch", watch_id=wid,
                                      watch_action="remove",
                                      ttype=TaskType.TRAVEL))
        assert "removed" in r2.text

    def test_never_raises_on_garbage(self, travel_agent):
        for text in ("", "   ", "!!!", "x" * 5000, "watch", "compare",
                     "flights from", "plan my trip"):
            assert travel_agent.handle(
                _req(text, ttype=TaskType.TRAVEL)) is not None


# ═════════════════════════════════════════════════ 9. wiring ════════════════

class TestWiring:
    def test_classifier_briefing_lane(self):
        c = IntentClassifier()
        for text in ("give me my morning briefing",
                     "daily brief please", "brief me on the day"):
            got = c.classify(text)
            assert got.task_type is TaskType.BRIEFING, (text, got.reason)

    def test_classifier_meal_lane(self):
        c = IntentClassifier()
        for text in ("plan my weekly menu", "what's in my pantry",
                     "make a grocery list", "what's expiring in the fridge"):
            got = c.classify(text)
            assert got.task_type is TaskType.MEAL, (text, got.reason)

    def test_classifier_travel_lane(self):
        c = IntentClassifier()
        for text in ("find flights from Delhi to Goa",
                     "cheap hotels in Jaipur",
                     "plan an itinerary for my trip",
                     "compare train tickets Delhi to Jaipur",
                     "things to do in Istanbul"):
            got = c.classify(text)
            assert got.task_type is TaskType.TRAVEL, (text, got.reason)

    def test_explicit_routes(self):
        c = IntentClassifier()
        assert c.classify("route this to briefing") \
            .task_type is TaskType.BRIEFING
        assert c.classify("[meal] what's for dinner") \
            .task_type is TaskType.MEAL
        assert c.classify("travel agent find me a hotel") \
            .task_type is TaskType.TRAVEL

    def test_other_lanes_unchanged(self):
        c = IntentClassifier()
        assert c.classify("fix the bug in my python function") \
            .task_type is TaskType.CODING
        assert c.classify("analyze RELIANCE chart setup") \
            .task_type is TaskType.TRADING
        assert c.classify("what's the latest news about EVs") \
            .task_type is TaskType.RESEARCH
        assert c.classify("I have fever and headache") \
            .task_type is TaskType.HEALTH
        assert c.classify("triage my inbox") is not None
        assert c.classify("hi").task_type is TaskType._FALLBACK

    def test_registry_off_by_default(self):
        for gate in ("JARVIS_BRIEFING_AGENT", "JARVIS_MEAL_AGENT",
                     "JARVIS_TRAVEL_AGENT"):
            os.environ.pop(gate, None)
        reg = build_default_registry()
        for name in ("briefing_agent", "meal_agent", "travel_agent"):
            assert type(reg.get(name)).__name__ == "StubAgent"
        r = reg.get("briefing_agent").handle(_req("briefing"))
        assert r.status is TaskStatus.NEEDS_INPUT

    def test_registry_on(self):
        os.environ["JARVIS_BRIEFING_AGENT"] = "on"
        os.environ["JARVIS_MEAL_AGENT"] = "on"
        os.environ["JARVIS_TRAVEL_AGENT"] = "on"
        try:
            reg = build_default_registry()
            assert type(reg.get("briefing_agent")).__name__ == "BriefingAgent"
            assert type(reg.get("meal_agent")).__name__ == "MealAgent"
            assert type(reg.get("travel_agent")).__name__ == "TravelAgent"
        finally:
            for gate in ("JARVIS_BRIEFING_AGENT", "JARVIS_MEAL_AGENT",
                         "JARVIS_TRAVEL_AGENT"):
                os.environ.pop(gate, None)



