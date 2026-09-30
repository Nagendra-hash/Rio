# JARVIS Remote — Android APK

A tiny WebView shell (zero external dependencies) that wraps the JARVIS
dashboard PWA (`dashboard/server.py`) as a native Android app.

What it adds over the browser/PWA:

- **Always-on background link** — the **Link** button (top-left) starts a
  foreground service that holds a WebSocket to `/ws` (role `phone`),
  auto-reconnects with backoff, pings for liveness, and shows a persistent
  "JARVIS link active" notification with **Ping JARVIS / Stop** quick actions.
  It restarts after reboot (RECEIVE_BOOT_COMPLETED) until stopped. The
  service authenticates via `/api/device-login` (device token → session),
  matching the web app's silent-repair flow, so it reconnects cleanly
  after every JARVIS restart.
- **Minimize-safe** — on resume the app re-syncs the page's socket
  (immediate reconnect when dead, 3s pong probe when alive) and requests
  battery-optimization exemption once, so Doze can't strangle the link.
- **Mic pass-through** — `RECORD_AUDIO` is granted to the WebView, so
  `/ws/phone-audio` voice works without per-site browser toggles.
- **File upload** — `onShowFileChooser` bridges the Android picker to the
  dashboard's upload endpoint.
- **Local CA trust** — the dashboard's `config/certs/ca.crt` (from
  `make_certs.sh`) is bundled at build time and trusted via
  `network_security_config`. An unknown signer raises an explicit TOFU
  prompt instead of a silent failure.
- **Bundled dashboard URL** — the PC's LAN IP is injected at build time
  (`resValue dashboard_url`); the floating **URL** button (top-right)
  retargets it if the IP changes.

First run: open the app → log in (QR/auto-login) once — the app captures the
paired device token from localStorage automatically — then tap **Link**.

## Build

```
scripts/build_apk.sh
```

One-time bootstrap downloads cmdline-tools + platform 34 + build-tools into
`~/Android/Sdk` and Gradle 8.7 into `~/gradle-8.7` (~1.5GB total, user-level,
no sudo). Output: `dist/JARVIS-Remote-1.3.apk` (versionCode 4).

Rebuilds are fast and fully offline.

## Install

1. Copy `dist/JARVIS-Remote-1.0.apk` to the phone.
2. Open it → allow "install from this source" when prompted.
3. Ensure the phone is on the same LAN as the PC and the dashboard is running.

## Files

| Path | Purpose |
| --- | --- |
| `app/src/main/java/com/jarvis/remote/MainActivity.java` | The app UI: WebView + pinning client + mic/upload + Link toggle | 
| `app/src/main/java/com/jarvis/remote/BackgroundLinkService.java` | Foreground service: WebSocket link, reconnect/backoff, notification quick actions |
| `app/src/main/res/xml/network_security_config.xml` | Trusts system + bundled JARVIS CA |
| `app/src/main/res/raw/jarvis_ca.pem` | Copied from `config/certs/ca.crt` at build time |
| `keystore.properties` / `jarvis-release.keystore` | Signing identity (generated once, **gitignored — back it up**) |

## Notes

- Keep the keystore stable: a new keystore means Android treats it as a
  different app and refuses an in-place upgrade.
- If `make_certs.sh` regenerates the CA, rebuild the APK (the CA is bundled at
  build time) or accept the one-time TOFU prompt in the app.
- HTTPS only: cleartext HTTP is disabled in both the manifest and WebView
  settings; the dashboard must be running with `config/certs/` present.
