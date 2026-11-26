# RIO as the Default Desktop UI — Change Report

**Date:** 2026-10-10 · **Branch:** `upgrade-20260930` · **Rollback dir:** `backup/rio_default_ui_20261010/`

## What the old start-up chain was (verified before any change)

```
~/.config/systemd/user/jarvis-gui.service (enabled, WantedBy=graphical-session.target)
  → ExecStart=/home/nagendra/Mark-III/jarvis_gui_wrapper.sh   (single-instance guard: pgrep + flock)
    → ./run.sh   (venv bootstrap, pid file /tmp/jarvis.pid)
      → exec .venv/bin/python main.py
        → def main():  ui = JarvisUI("face.png")   ← old PyQt6 HUD window (ui.py MainWindow)
        → JarvisLive(ui).run()  → DashboardServer().serve()   ← :8000/:8001
```

The old Jarvis UI = `JarvisUI("face.png")` constructed at the top of `main()`
in `main.py` (real window class: `MainWindow` in `ui.py`). The RIO web UI was
already served by the same dashboard at the twelve routes
`/home /chat /agents /brain /memory /files /hermes /trading /missions /activity /system /settings`
(`dashboard/rio_api.py`), but nothing ever opened it on the desktop.

## What changed (3 files modified, 1 added)

| File | Change |
|---|---|
| `rio_window.py` (**new**) | Opens RIO as a Chrome **app-mode** window (`--app=`, frameless, no tabs) on the running dashboard. Waits for the dashboard to actually answer (HTTPS probe — the cert pair makes :8000 TLS-only), mints one of the server's own short-lived pairing keys through `DashboardServer.live` (class registry set in `serve()`), and opens `/auto-login?key=…&to=/home` — the exact QR-pairing flow a phone walks. Session token lives only in the window's `sessionStorage`. Throwaway profile `--user-data-dir=/tmp/jarvis_rio_window_profile` (never the user's real Chrome profile). Single-window guard with a dying-window re-check (survives service restarts). Skipped entirely when `JARVIS_RIO_WINDOW=0`. |
| `main.py` | `main()` now honours `JARVIS_RIO_WINDOW` (default **1**): a `JarvisUI`-compatible shim `_RioWindowUI` replaces the old HUD construction, so **no PyQt window is ever shown** and the voice loop/dashboard/Hermes/agents run exactly as before. A daemon opener thread opens the RIO window once the dashboard is live. Set `JARVIS_RIO_WINDOW=0` → code path is byte-for-byte the old one (`JarvisUI("face.png")`). |
| `run.sh` | One notice line only (launch actually happens inside `main.py`'s opener so it can pass the pairing key). Same exec line, same ports, same service. |
| `dashboard/server.py` | `/auto-login` gets an optional `to=` param (allow-list of the 12 RIO routes; anything else → `/`, so no open-redirect / template-injection) + the `DashboardServer.live` registry (2-line `serve()` change). Verified: valid `to=/home` → lands there; `to=/evil'` → `/`; bad key → "Link Expired"; minted token authenticates `/api/rio/health`. |

**Net effect:** clicking your JARVIS desktop icon / autostart brings up
**RIO's /home as the main window** — the old HUD never appears (not even
briefly: its construction is gated out before `show()` is ever reached).

## What was NOT touched

Hermes gateway, Telegram/WhatsApp bridge, ngrok tunnel, agents, RAG, memory,
uploads, trading, config files, ports (still 8000/8001), the `jarvis-gui`
service unit itself, the old `ui.py` HUD (intact as fallback), and every
route/asset the old `app.html` PWA uses.

## Backups (byte-identical, sha256-verified)

- `backup/rio_default_ui_20261010/main.py`
- `backup/rio_default_ui_20261010/run.sh`
- `backup/rio_default_ui_20261010/dashboard/server.py`

## How to launch RIO

```bash
systemctl --user start jarvis-gui.service        # as before — nothing new
# or by hand: ~/Mark-III/run.sh
```
RIO window opens on `/home` automatically; backend voice session, mic,
dashboard APIs keep running as before.

## Rollback — restore the old Jarvis HUD

```bash
cd ~/Mark-III
cp backup/rio_default_ui_20261010/main.py main.py
cp backup/rio_default_ui_20261010/run.sh run.sh
cp backup/rio_default_ui_20261010/dashboard/server.py dashboard/server.py
rm -f rio_window.py
systemctl --user restart jarvis-gui.service
```
(Or without restoring anything: `JARVIS_RIO_WINDOW=0` in the service's env,
or temporarily `Environment=JARVIS_RIO_WINDOW=0` in the unit's env.conf.)

## Test results

| Check | Result |
|---|---|
| `py_compile` main.py / rio_window.py / dashboard/server.py; `bash -n run.sh` | **PASS** |
| Standalone live server: `/auto-login?key=…&to=/home` → 200 → redirect `/home` | **PASS** |
| `to=` injection (`/evil'`) → falls back to `/` | **PASS** |
| Bad/expired key → "Link Expired" page | **PASS** |
| Minted session token hits `/api/rio/health` → 200 | **PASS** |
| tests: `test_coding_agent.py` 71/71, `test_llm_router_plan.py` 16/16, dashboard/rio-related 21/23 | **PASS** (2 failures verified **pre-existing** — reproduce with the task's changes stashed; `_load_devices` missing and a growth-agent text assertion, both unrelated) |
| Real launch: `systemctl --user start jarvis-gui` → RIO window appears at `/home`, authed via real pairing key | **PASS** (chrome `--app` proc seen with `/auto-login?key=…&to=/home`) |
| Old PyQt HUD: **not** constructed/shown in window mode | **PASS** (no `JarvisUI` instantiation; only `python main.py` + one chrome window group) |
| Service **restart** → window re-opens (race with dying window fixed) | **PASS** (×2 consecutive restarts) |
| Single intended window (no stacking/duplicates) | **PASS** (one `--app` group per check) |
| Hermes gateway / WhatsApp bridge / ngrok untouched+active | **PASS** (separate units, not restarted) |
| RIO pages + assets over HTTPS: `/home`, `/chat`, `/rio/assets/app.js`, `/rio/assets/styles.css` → 200 | **PASS** |
| **All twelve RIO pages** (2026-10-10 full sweep): 12/12 routes → 200; 11/11 assets (styles.css, app.js + agents/brain/chat/files/hermes/memory/missions/system/trading.js) → 200; 20/20 authed GET endpoints the pages call (health, home, agents, brain, memory, files, hermes+stats, missions, rag, system metrics/services, trading overview/positions/watchlist/signals/risk/connection/autotrade, autonomy tasks) → 200 | **PASS** |
| **Real-browser render of all 12 pages** (Playwright Chromium, same flags/profile semantics as the desktop window, authed session seeded via the same token path): each page titles correctly (`Home · RIO` … `Settings · RIO`), all 12 sidebar nav items render, live data painted (no "pair required"/"backend unreachable" markers), **zero console/page errors** — script verdict `ALL 12 PAGES PASS` | **PASS** |
| HUD fallback `JARVIS_RIO_WINDOW=0` (temp systemd drop-in): no RIO window; old PyQt HUD takes over (libQt6Widgets + Qt Wayland xdg-shell loaded, 2 live wayland-shm frame buffers, mic/voice/dashboard all start); drop-in removed → RIO window returned | **PASS** (both directions) |
| Screenshot of desktop window | **NOT TESTED** (GNOME blocks screenshots for this session; window existence/steady-state verified via process + journal) |

## Remaining issues / notes

- CDP could not attach to the *already-open* desktop window (Chrome binds the
  debug port only on first launch), so the browser-real render check ran an
  identical Chromium engine with the same profile/flags instead. The desktop
  window itself was re-opened through the standard pairing flow afterwards;
  the system is left in its normal verified state.
- The default window size comes from Chrome's `--app` default (fills screen at
  last-used size); set e.g. `--window-size=1280,860` inside `rio_window.py` if
  you want a fixed size.
- `/tmp/jarvis_rio_window_profile` is cleared by OS reboots — the window
  re-pairs silently each boot through the same one-time key flow (no user
  action; nothing to renew).
- Keyboard shortcuts from the old HUD (studio/panel drawers) still live in the
  old HUD only; in RIO mode they're available through RIO's own web UI pages.
