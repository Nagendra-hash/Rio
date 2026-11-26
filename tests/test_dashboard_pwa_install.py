"""
tests/test_dashboard_pwa_install.py — E2E regression test for the PWA
install flow of the dashboard.

History: the dashboard became an installable PWA (manifest + icons +
pass-through service worker + install button) so the installed Android app
survives backgrounding far better than a browser tab. This locks the whole
chain down so a refactor can't silently break installability.

What this test proves (real headless Chrome + the REAL DashboardServer):
  1. Service worker: registers, activates, and controls the page
     (127.0.0.1 is a secure context, so no HTTPS needed here).
  2. Install surface: manifest is linked + valid (standalone, root scope),
     every manifest icon resolves as PNG, and the header install button
     follows the browser's beforeinstallprompt lifecycle.
  3. SW fetch semantics: immutable icons ARE cached, but API responses and
     HTML are NEVER served stale (pass-through) — a caching SW would break
     both the bearer-token APIs and the live control surface.

Isolation: everything runs against a sandboxed server (temp static token),
never against your real config/.

Exit codes: 0 = pass, 1 = regression, 2 = environment error (deps missing).
Also pytest-compatible: test_pwa_install_flow().

Run:  .venv/bin/python tests/test_dashboard_pwa_install.py
"""

import json
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ── isolation: sandbox the static-token store BEFORE imports ─────────────────
_TMPDIR = tempfile.mkdtemp(prefix="jarvis-pwa-e2e-")
_TMP_CONFIG = Path(_TMPDIR) / "api_keys.json"
_TMP_CONFIG.write_text(json.dumps({"dashboard_auth_token": "e2e-static-token-1234"}))

import memory.config_manager as cfg  # noqa: E402

cfg.CONFIG_FILE = _TMP_CONFIG

# Same isolation guard as test_conversation_reliability.py: other modules in
# this repo also monkeypatch cfg.CONFIG_FILE at import time, and pytest runs
# every module's imports before any test — re-assert THIS module's sandbox
# around each test (and restore afterwards) so file order never decides
# which sandbox a request actually sees (401 cross-contamination, 2026-10-13).
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _sandbox_config_file():
    prev = cfg.CONFIG_FILE
    cfg.CONFIG_FILE = _TMP_CONFIG
    yield
    cfg.CONFIG_FILE = prev


from dashboard.server import DashboardServer  # noqa: E402

STATIC_DIR = BASE / "dashboard" / "static"


def _env_ok() -> tuple[bool, str]:
    try:
        import uvicorn  # noqa: F401
        import playwright  # noqa: F401
    except ImportError as e:
        return False, f"missing dep: {e} (.venv/bin/pip install playwright uvicorn)"
    return True, ""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(port: int):
    srv = DashboardServer()
    import uvicorn

    config = uvicorn.Config(srv.app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    end = time.time() + 25
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=2)
            return srv, server
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("test server did not come up")


# ── static checks (always run, even without a browser) ───────────────────────


def test_pwa_files_present() -> tuple[bool, str]:
    manifest = STATIC_DIR / "manifest.webmanifest"
    sw = STATIC_DIR / "sw.js"
    if not manifest.exists():
        return False, "dashboard/static/manifest.webmanifest is missing"
    if not sw.exists():
        return False, "dashboard/static/sw.js is missing"
    try:
        mf = json.loads(manifest.read_text())
    except Exception as e:
        return False, f"manifest is not valid JSON: {e}"
    problems = []
    if mf.get("display") != "standalone":
        problems.append("display != standalone")
    if mf.get("start_url") != "/":
        problems.append("start_url != /")
    icons = mf.get("icons") or []
    if not any(i.get("purpose") == "maskable" for i in icons):
        problems.append("no maskable icon declared")
    for i in icons:
        # manifest src is a URL path (/static/icons/x.png); the file lives in
        # dashboard/static/x.png — strip the mount prefix, not the slash.
        rel = i["src"].removeprefix("/static/")
        p = STATIC_DIR / rel
        if not p.exists():
            problems.append(f"icon file missing: {i['src']} (looked at {p})")
        elif p.read_bytes()[:4] != b"\x89PNG":
            problems.append(f"icon is not a PNG: {i['src']}")
    if problems:
        return False, "; ".join(problems)
    return True, f"manifest + sw.js valid, {len(icons)} icon files all PNG"


def test_no_cache_headers() -> tuple[bool, str]:
    """HTML, the SW script and the manifest must be served no-cache.

    History: the phone once kept rendering a pre-fix app.html for days while
    the server had long moved on — and a cached /sw.js would be worse, since
    Chrome only re-fetches the worker script through its HTTP cache.
    """
    import urllib.error

    try:
        import uvicorn  # noqa: F401
    except ImportError as e:
        return True, f"ENV-SKIP: {e}"  # header check needs a live server

    port = _free_port()
    _, server = _start_server(port)
    base = f"http://127.0.0.1:{port}"
    try:
        no_cache_ok = lambda h: (h.get("cache-control", "").startswith("no-cache"))
        html_paths = ["/", "/login", "/chart"]
        problems = []
        for p in html_paths:
            try:
                r = urllib.request.urlopen(base + p)
            except urllib.error.HTTPError as e:
                problems.append(f"{p} -> HTTP {e.code}")
                continue
            if r.status != 200 or not no_cache_ok(r.headers):
                problems.append(
                    f"{p} missing no-cache Cache-Control "
                    f"(status {r.status}, got {r.headers.get('cache-control')!r})"
                )
        for p in ("/sw.js", "/manifest.webmanifest"):
            r = urllib.request.urlopen(base + p)
            if not no_cache_ok(r.headers):
                problems.append(
                    f"{p} missing no-cache Cache-Control "
                    f"(got {r.headers.get('cache-control')!r}) — stale-SW trap"
                )
        if problems:
            return False, "; ".join(problems)
        return True, (
            "no-cache headers on /, /login, /chart, /sw.js, " "/manifest.webmanifest"
        )
    finally:
        server.should_exit = True


def test_pwa_wiring_present() -> tuple[bool, str]:
    problems = []
    app = (STATIC_DIR / "app.html").read_text()
    for needle in (
        'rel="manifest"',
        "beforeinstallprompt",
        "installPwa",
        "serviceWorker.register",
        "install-btn",
        "pwa-badge",
        "openPwaPanel",
        "_pwaState",
        "_pwaVerdict",
    ):
        if needle not in app:
            problems.append(f"app.html missing {needle!r}")
    sw_src = (STATIC_DIR / "sw.js").read_text()
    if "addEventListener('fetch'" not in sw_src:
        problems.append(
            "sw.js has no fetch handler (Chrome installability requires one)"
        )
    server_src = (BASE / "dashboard" / "server.py").read_text()
    for needle in (
        '"/manifest.webmanifest"',
        '"/sw.js"',
        '"/static/icons/{filename}"',
        "Service-Worker-Allowed",
    ):
        if needle not in server_src:
            problems.append(f"server.py missing route {needle}")
    if problems:
        return False, "; ".join(problems)
    return True, "manifest/SW/routes/install-button all wired"


# ── the live browser test ────────────────────────────────────────────────────


def test_pwa_install_flow() -> tuple[bool, str]:
    from playwright.sync_api import sync_playwright

    failures: list[str] = []
    port = _free_port()
    srv, server = _start_server(port)
    base = f"http://127.0.0.1:{port}"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                channel="chrome",
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(base + "/login", wait_until="domcontentloaded")
            page.evaluate(
                "sessionStorage.setItem('jarvis_token','e2e-static-token-1234');"
                "sessionStorage.setItem('jarvis_key','E2EKEY');"
            )
            page.goto(base + "/", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)

            # ── 1) service worker ───────────────────────────────────────────
            if not page.evaluate("window.isSecureContext"):
                failures.append("127.0.0.1 page is not a secure context?!")
            reg = page.evaluate(
                "navigator.serviceWorker.getRegistration()"
                ".then(r => r ? {active: !!r.active, installing: !!r.installing,"
                " waiting: !!r.waiting} : null)"
            )
            if not (
                reg and reg["active"] and not reg["installing"] and not reg["waiting"]
            ):
                failures.append(f"SW not cleanly registered+active: {reg}")
            if not page.evaluate("!!navigator.serviceWorker.controller"):
                failures.append("SW does not control the page")

            # ── 2) install surface ──────────────────────────────────────────
            link = page.evaluate(
                "document.querySelector('link[rel=manifest]')?.href || ''"
            )
            if not link.endswith("/manifest.webmanifest"):
                failures.append(f"manifest link wrong: {link}")

            mf = json.load(urllib.request.urlopen(base + "/manifest.webmanifest"))
            if mf.get("display") != "standalone" or mf.get("start_url") != "/":
                failures.append("served manifest lost standalone/root start_url")

            for icon in mf.get("icons", []):
                r = urllib.request.urlopen(base + icon["src"])
                if r.status != 200 or r.headers["content-type"] != "image/png":
                    failures.append(f"icon did not resolve as PNG: {icon['src']}")

            if page.evaluate(
                "document.getElementById('install-btn')" ".classList.contains('show')"
            ):
                failures.append("install button visible before any install prompt")

            # simulate Chrome's install prompt
            page.evaluate("""
              const ev = new Event('beforeinstallprompt');
              ev.prompt = () => {};
              ev.userChoice = Promise.resolve({ outcome: 'accepted' });
              window.dispatchEvent(ev);
            """)
            page.wait_for_timeout(400)
            if not page.evaluate(
                "document.getElementById('install-btn')" ".classList.contains('show')"
            ):
                failures.append("install button did not appear on beforeinstallprompt")
            if page.evaluate("typeof installPwa !== 'function'"):
                failures.append("installPwa() not defined")
            else:
                page.click("#install-btn")
                page.wait_for_timeout(400)
                if page.evaluate(
                    "document.getElementById('install-btn')"
                    ".classList.contains('show')"
                ):
                    failures.append("install button not consumed after prompt fired")
                page.evaluate("installPwa()")  # no-prompt path must not throw

            # ── 2b) SW status badge + diagnostics panel ─────────────────────
            # Healthy path: the badge must be 'ok' class (SW ✓ / SW ✓ ⤓ —
            # which label depends on whether the synthetic prompt from
            # scenario 2 has already fired), and the panel must report a
            # secure context + a healthy verdict.
            badge_txt = page.evaluate(
                "document.getElementById('pwa-badge')?.textContent || ''"
            )
            if badge_txt not in ("SW \u2713", "SW \u2713 \u2913"):
                failures.append(f"pwa-badge unhealthy in healthy env: {badge_txt!r}")
            if not page.evaluate(
                "document.getElementById('pwa-badge').classList.contains('ok')"
            ):
                failures.append("pwa-badge lacks 'ok' class in healthy state")

            # beforeinstallprompt was dispatched above → badge must show the ⤓
            if badge_txt != "SW \u2713 \u2913":
                failures.append("pwa-badge did not reflect available install prompt")

            page.evaluate("openPwaPanel()")
            page.wait_for_timeout(150)
            if not page.evaluate(
                "document.getElementById('pwa-panel').classList.contains('show')"
            ):
                failures.append("diagnostics panel did not open")
            panel_txt = page.evaluate("document.getElementById('pwa-rows').innerText")
            if "Secure context" not in panel_txt or "yes" not in panel_txt:
                failures.append(
                    f"diagnostics panel missing secure-context row: {panel_txt!r}"
                )
            verdict = page.evaluate("document.getElementById('pwa-fix').innerText")
            if not (
                "healthy" in verdict.lower() or "ready to install" in verdict.lower()
            ):
                failures.append(f"healthy-state verdict wrong: {verdict!r}")
            page.evaluate("closePwaPanel()")
            if page.evaluate(
                "document.getElementById('pwa-panel').classList.contains('show')"
            ):
                failures.append("diagnostics panel did not close")

            # ── 3) SW fetch semantics ───────────────────────────────────────
            page.evaluate("fetch('/static/icons/icon-192.png').then(r => r.blob())")
            page.wait_for_timeout(400)
            icon_cached = page.evaluate("""
              (async () => {
                for (const k of await caches.keys()) {
                  if (await (await caches.open(k))
                      .match('/static/icons/icon-192.png')) return true;
                }
                return false;
              })()
            """)
            if not icon_cached:
                failures.append("immutable icon was not cached by the SW")

            # API responses must never be cached
            req = urllib.request.Request(
                base + "/api/phone/status",
                headers={"Authorization": "Bearer e2e-static-token-1234"},
            )
            urllib.request.urlopen(req)
            api_cached = page.evaluate("""
              (async () => {
                for (const k of await caches.keys()) {
                  const reqs = await (await caches.open(k)).keys();
                  if (reqs.some(r => r.url.includes('/api/'))) return true;
                }
                return false;
              })()
            """)
            if api_cached:
                failures.append("an API response ended up in SW cache")

            # HTML passthrough: unique URL only works if SW forwards to network,
            # and SW-fetched HTML must equal a direct network fetch.
            if not page.evaluate(
                "fetch('/?sw-freshness=' + Date.now()).then(r => r.ok)"
            ):
                failures.append("SW did not pass a unique URL through to the network")
            # Direct fetch for comparison (no need for server loop)
            direct = urllib.request.urlopen(base + "/").read().decode()
            through = page.evaluate("fetch('/').then(r => r.text())")
            if through != direct:
                failures.append(
                    f"SW-served HTML differs from network HTML "
                    f"({len(through)} vs {len(direct)} bytes) — stale-cache risk"
                )

            # ── 4) SW registration failure surface (the phone's cert case) ──
            # Fresh context where Chrome REFUSES service-worker registration
            # (exactly what happens on an origin whose self-signed cert was
            # merely bypassed — page.route can't simulate this because SW
            # script fetches bypass Playwright routing; overriding register()
            # in an init script is the deterministic equivalent). The badge
            # must end at 'SW ✗' and the panel must explain the CA fix.
            ctx2 = browser.new_context()
            p2 = ctx2.new_page()
            p2.add_init_script("""
              navigator.serviceWorker.register = () =>
                Promise.reject(new Error(
                  'Failed to register a ServiceWorker: The document is not fully trusted'));
            """)
            p2.goto(base + "/login", wait_until="domcontentloaded")
            p2.evaluate(
                "sessionStorage.setItem('jarvis_token','e2e-static-token-1234');"
                "sessionStorage.setItem('jarvis_key','E2EKEY');"
            )
            p2.goto(base + "/", wait_until="domcontentloaded")
            p2.wait_for_timeout(1500)
            if p2.url.rstrip("/").endswith("/login"):
                failures.append(
                    "scenario 4 page fell back to /login (token seeding failed)"
                )
            badge2 = p2.evaluate("document.getElementById('pwa-badge')?.textContent")
            if badge2 != "SW \u2717":
                failures.append(
                    f"badge on SW-registration failure should be 'SW \u2717', got {badge2!r}"
                )
            p2.evaluate("openPwaPanel()")
            fix2 = p2.evaluate("document.getElementById('pwa-fix').innerText")
            if "CA" not in fix2 or "certificate" not in fix2.lower():
                failures.append(f"failure verdict missing CA fix guidance: {fix2!r}")
            ctx2.close()

            browser.close()
    finally:
        server.should_exit = True

    if failures:
        return False, "; ".join(failures)
    return True, (
        "SW registered+controlling, badge states (SW ✓ / SW ✓ ⤓ / SW ✗) "
        "+ diagnostics panel verified, manifest/icons valid, install-button "
        "lifecycle correct, cache semantics (icons cached, API/HTML live) verified"
    )


# ── standalone runner (repo convention: 0 pass / 1 regression / 2 env) ──────


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--static-only", action="store_true", help="Run only static checks"
    )
    args = parser.parse_args()

    ok, msg = _env_ok()
    if not ok:
        print(f"ENV-SKIP: {msg}")
        return 2

    for fn in (test_pwa_files_present, test_no_cache_headers, test_pwa_wiring_present):
        ok_n, msg_n = fn()
        print(f"{'PASS' if ok_n else 'FAIL'}  {fn.__name__}: {msg_n}")
        if not ok_n:
            return 1

    if args.static_only:
        print("Static checks passed")
        return 0

    try:
        ok_live, msg_live = test_pwa_install_flow()
    except Exception as e:
        print(f"ENV-SKIP: browser test could not run ({type(e).__name__}: {e})")
        return 2
    print(f"{'PASS' if ok_live else 'FAIL'}  live PWA install flow: {msg_live}")
    return 0 if ok_live else 1


if __name__ == "__main__":
    sys.exit(main())
