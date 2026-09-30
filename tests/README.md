# Test suite

Unit tests plus real-browser end-to-end tests for the phone dashboard. The
E2E tests run the **actual** `DashboardServer` (uvicorn, in-process, sandboxed
temp config) against real headless Chrome via
[Playwright](https://playwright.dev/python/) — not mocks.

## Running

One command for everything (pytest):

```bash
.venv/bin/python -m pytest tests/ -q              # all tests, ~35 s
.venv/bin/python -m pytest tests/ -q -m "not browser_e2e"   # fast unit-only pass, ~3 s
.venv/bin/python -m pytest tests/ -q -m "browser_e2e"       # browser E2E only
```

Markers are registered in `pytest.ini` (`--strict-markers` on):
- `browser_e2e` — launches real headless Chrome against the live server;
  auto-SKIPped (with the exact fix in the reason) when Playwright or system
  Chrome is missing.
- `e2e` — any end-to-end test against a live server (browser or not).

Standalone runners still work exactly as before (PASS/FAIL/ENV-SKIP output,
exit codes 0 = pass / 1 = regression / 2 = env error):

```bash
.venv/bin/python tests/test_dashboard_offline.py        # offline/reconnect behavior
.venv/bin/python tests/test_dashboard_pwa_install.py    # PWA install flow
.venv/bin/python tests/test_voice_keepalive.py          # phone-mic keep-alive
```

Missing deps for the browser suite:
`.venv/bin/pip install playwright pytest && .venv/bin/playwright install chromium`
(system Chrome also required — the tests use `channel="chrome"`).

### Note on `(ok, msg)`-style tests

The standalone runner functions return `(ok: bool, message: str)` per this
repo's convention. Plain pytest ignores return values, so `tests/conftest.py`
wraps them: a `(False, msg)` becomes a real assertion failure, `(True, msg)`
passes cleanly. Assert-style tests that return `None` are untouched.

## What each test locks down

### `test_dashboard_offline.py` — offline screen + reconnect machinery
1. **Static check** (always runs): the reconnect/offline/4001 pieces exist in
   `app.html`.
2. **Baseline**: WS connects against the real server; broadcasts render in the
   feed; the client's `_lastSeq` advances.
3. **Phone offline** (CDP `Network.emulateNetworkConditions`): the offline
   screen shows "Phone is offline"; session and device tokens survive; the
   page reconnects by itself when the network returns and the screen hides.
4. **PC unreachable** (the real thing): the server object is **stopped** —
   then **restarted as a fresh instance** (in-memory sessions gone, exactly
   like a reboot). The page must show "Can't reach JARVIS" during the outage,
   reconnect on its own afterwards **via the close-code-4001 → silent device
   re-pair path** (asserted by checking the session token actually changed),
   and use the `boot`-id handshake to reset its `since=` baseline so nothing
   is replayed or lost.
5. **Login page**: a failed device-login (route aborted) keeps the
   `localStorage` device token instead of forcing a QR re-scan.

### `test_dashboard_pwa_install.py` — PWA install flow
1. **Static checks** (always run): manifest/SW/icons exist and are valid;
   server routes and the install button are wired.
2. **Live**: the service worker registers, activates and **controls** the
   page; the install button follows the browser's `beforeinstallprompt`
   lifecycle (hidden → shown → consumed); immutable icons are cached by the
   SW while **API responses and HTML are never served stale** (pass-through
   semantics — a caching SW would break the bearer-token APIs).

### `test_voice_keepalive.py` — phone-mic keep-alive
Pre-existing test: mic tap opens the audio socket; killing the socket
mid-session auto-reconnects without user interaction; a second tap stops
cleanly with no zombie reconnects.

## Isolation guarantees

- Browser contexts are created fresh per run (`storage_state=None`), and the
  offline test blocks service workers so the two suites don't interact.
- The conftest probes for Playwright in a **subprocess** (an in-process
  import check would be poisoned by `sys.modules` caching).

- The static dashboard token and the device-token store are **temp copies**
  (`tempfile.mkdtemp`) — `config_manager.CONFIG_FILE` and
  `srv._devices_file` are repointed before anything starts. Your real
  `config/` is never read or written.
- Servers bind `127.0.0.1` on a free port; nothing listens on the network.
- Browser contexts are created fresh per run (`storage_state=None`), and the
  offline test blocks service workers so the two suites don't interact.

## Notes & gotchas (learned the hard way)

- **`page.request` bypasses service workers.** To assert SW fetch semantics,
  fetch *from inside the page* (`page.evaluate("fetch(...)")`).
- **A close before WS-accept surfaces as code 1006**, not 4001 — that's why
  the server now accepts first and *then* closes with 4001 (see
  `dashboard/server.py`), and why the client also has a mid-loop silent
  re-pair fallback for opaque failures.
- **`since=`/`seq` is per-server-lifetime.** After a restart the seq counter
  restarts at 1, so clients send a `boot` id and reset their baseline when it
  changes — otherwise resumes silently drop new messages.
- The server records `_loop` **before** `websocket.accept()`; tests that
  broadcast must wait for the *new* server to actually register the client
  (`len(srv._clients) > 0`), not just for the page socket to look open.
