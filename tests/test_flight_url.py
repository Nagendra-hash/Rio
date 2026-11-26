#!/usr/bin/env python3
"""Regression test for the flight_finder Google Flights URL (actions/flight_finder.py).

For as long as the tool existed, every search silently loaded the WRONG trip:
the URL carried a hardcoded `tfs=` blob — Google's base64 structured-search
state — encoding Istanbul→London on 2025-03-15 (the author's manual test
search). Google Flights honors `tfs` over the free-text `q=`, so the requested
route never loaded; worse, the blob's date was in the past, so the results
list never rendered and the page text stayed at the ~500-char app shell. Every
lookup ended in "I couldn't find any flights" while still burning an LLM
extraction call. Fixed 2026-09-21 by dropping the `tfs` parameter entirely.

Three guards, so this class of bug can never silently return:

1. Builder behavior (offline, always run): `_build_google_flights_url()` must
   never emit `tfs=`, must carry the REQUESTED origin/destination/date (catches
   any hardcoded route replacing the parameters), and must pass cabin/passengers
   through.

2. Source scan (offline, always run): no `tfs=` anywhere in flight_finder.py —
   catches a blob reintroduced outside the URL builder (e.g. hand-built in
   `_search_flights_browser`).

3. Live sanity (network, skipped when offline): the built URL's page actually
   titles itself with the requested route — the definitive check that Google
   isn't being handed a stale structured search. Only runs with --live.

Exit codes: 0 = pass, 1 = regression, 2 = environment error (repo layout
broken / module unimportable) so automation can distinguish "test failed"
from "can't check right now".

Usage:
    python3 tests/test_flight_url.py
    python3 tests/test_flight_url.py --live     # + network tier (opens a browser)

Also pytest-compatible (test functions below; the live tier is a separate
main() branch so pytest never opens a browser).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from actions.flight_finder import _build_google_flights_url, _CABIN_CODE
except Exception as e:  # repo layout broken
    print(f"❌ ENVIRONMENT ERROR — cannot import flight_finder: {e}")
    sys.exit(2)

MODULE_PATH = REPO_ROOT / "actions" / "flight_finder.py"

# Distinctive tokens that cannot accidentally appear in a stale hardcoded
# route — if these come back in the URL, the parameters made it through.
CASES = [
    # (origin, destination, date, return_date, passengers, cabin)
    ("Austin", "Osaka", "2027-04-10", None, 1, "economy"),
    ("Kochi", "Toronto", "2026-12-01", "2026-12-20", 2, "business"),
    (" Vienna", "Nagoya", "2027-01-15", None, 3, "premium"),
]

# The incident's residue — none of these may ever appear in a built URL.
_POISON_TOKENS = ("Istanbul", "London", "IST", "LHR", "2025-03-15")


def test_no_tfs_param_in_builder():
    for origin, dest, date, ret, pax, cabin in CASES:
        url = _build_google_flights_url(origin, dest, date, ret, pax, cabin)
        assert "tfs=" not in url, (
            f"tfs= blob is back in the URL for {origin}→{dest}: {url}\n"
            "Google honors tfs (structured search state) over q= — a stale "
            "blob silently overrides the requested trip. See module docstring."
        )


def test_requested_route_and_date_present():
    for origin, dest, date, ret, pax, cabin in CASES:
        url = _build_google_flights_url(origin, dest, date, ret, pax, cabin)
        for token in (origin.strip(), dest.strip(), date):
            assert token in url, (
                f"requested '{token}' missing from URL for {origin}→{dest}: {url}\n"
                "A hardcoded route/date may be overriding the parameters."
            )


def test_cabin_and_passengers_passed_through():
    url = _build_google_flights_url(
        "Austin", "Osaka", "2027-04-10", None, 2, "business"
    )
    assert "adults=2" in url, f"passengers lost: {url}"
    assert f"cabin={_CABIN_CODE['business']}" in url, f"cabin lost: {url}"


def test_no_incident_artifacts_in_urls():
    for origin, dest, date, ret, pax, cabin in CASES:
        url = _build_google_flights_url(origin, dest, date, ret, pax, cabin)
        for poison in _POISON_TOKENS:
            assert poison not in url, (
                f"incident artifact {poison!r} found in URL for "
                f"{origin}→{dest}: {url}"
            )


def test_no_tfs_anywhere_in_module_source():
    src = MODULE_PATH.read_text(encoding="utf-8")
    hits = []
    for ln in src.splitlines():
        if "tfs=" not in ln:
            continue
        # Only a comment mentioning tfs= is allowed: check the CODE portion
        # (text before '#'); with no '#' the code portion is the whole line.
        code = ln.split("#", 1)[0] if "#" in ln else ln
        if "tfs=" in code:
            hits.append(ln)
    assert not hits, (
        "tfs= found outside comments in actions/flight_finder.py:\n  "
        + "\n  ".join(ln.strip() for ln in hits)
        + "\nA structured-search blob anywhere in this module silently "
        "overrides the user's requested trip. See module docstring."
    )


# ── Live tier (--live only): Google must title the page with the route ──────


def _live_check(origin: str, dest: str, date: str) -> None:
    """Opens the built URL in the automation browser and asserts the page
    title names the requested route (the definitive anti-stale-blob check).
    Requires a display + Playwright chromium; skipped unless --live."""
    import asyncio

    import actions.browser_control as bc

    url = _build_google_flights_url(origin, dest, date, None, 1, "economy")

    async def flow(sess):
        page = await sess._get_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        title = ""
        for _ in range(8):  # JS app: shell paints first
            await asyncio.sleep(3)
            title = await page.title()
            if len(await page.inner_text("body")) > 1500:
                break
        return title

    sess = bc._registry.get("chrome")
    title = sess.run(flow(sess), timeout=90)
    assert origin.split()[-1].lower() in title.lower(), (
        f"page title {title!r} does not name requested origin {origin!r} — "
        "Google served a stale/overridden search (tfs-class bug is back?)"
    )
    assert (
        dest.split()[-1].lower() in title.lower()
    ), f"page title {title!r} does not name requested destination {dest!r}"


def main(argv: list[str]) -> int:
    tests = [
        test_no_tfs_param_in_builder,
        test_requested_route_and_date_present,
        test_cabin_and_passengers_passed_through,
        test_no_incident_artifacts_in_urls,
        test_no_tfs_anywhere_in_module_source,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"✅ PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"❌ FAIL  {t.__name__}\n        {str(e)[:300]}")

    if "--live" in argv:
        try:
            _live_check("Lucknow", "Delhi", "2026-12-01")
            print("✅ PASS  live_title_matches_route")
        except AssertionError as e:
            failures += 1
            print(f"❌ FAIL  live_title_matches_route\n        {str(e)[:300]}")
        except Exception as e:
            print(f"⚠️  SKIP  live_title_matches_route (environment: {str(e)[:120]})")

    print("─" * 60)
    print(
        "flight URL regression:",
        "CLEAN ✅"
        if failures == 0
        else f"{failures} FAILURE(S) ❌ — the tfs/hardcoded-route bug is back",
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
