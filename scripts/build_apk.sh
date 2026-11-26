#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# build_apk.sh — build the JARVIS Remote Android app (WebView shell APK)
#
# Produces: Mark-LIII/dist/JARVIS-Remote-<version>.apk
#
# One-time bootstrap (user-level, no sudo):
#   - Android cmdline-tools + platform 34 + build-tools → ~/Android/Sdk
#   - Gradle 8.7 → ~/gradle-8.7
#   - signing keystore android/jarvis-release.keystore (+ keystore.properties)
#   - dashboard CA → android/app/src/main/res/raw/jarvis_ca.pem
#
# Rebuilds after the first run are fast and fully offline.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

SDK="${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}"
GRADLE_VER=8.7
GRADLE_BIN="$HOME/gradle-$GRADLE_VER/bin/gradle"
CMDLINE_TOOLS_VER=11076708
DIST=dist
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

log() { echo "[apk] $*"; }

# ── 0. sanity ────────────────────────────────────────────────────────────────
command -v java >/dev/null || { echo "✗ java not found (need JDK 17)"; exit 1; }
[ -n "$LAN_IP" ] || { echo "✗ could not detect LAN IP"; exit 1; }
log "LAN IP: $LAN_IP"

# ── 1. Android SDK cmdline-tools ─────────────────────────────────────────────
if [ ! -x "$SDK/cmdline-tools/latest/bin/sdkmanager" ]; then
    log "downloading Android cmdline-tools (one-time, ~130MB)…"
    mkdir -p "$SDK/cmdline-tools"
    TMP="$(mktemp -d)"
    curl -fsSL --retry 3 -o "$TMP/tools.zip" \
        "https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_TOOLS_VER}_latest.zip"
    unzip -q "$TMP/tools.zip" -d "$TMP"
    rm -rf "$SDK/cmdline-tools/latest"
    mv "$TMP/cmdline-tools" "$SDK/cmdline-tools/latest"
    rm -rf "$TMP"
fi
export ANDROID_SDK_ROOT="$SDK"
SDKMANAGER="$SDK/cmdline-tools/latest/bin/sdkmanager"

# ── 2. SDK packages + licenses ───────────────────────────────────────────────
if [ ! -d "$SDK/platforms/android-34" ] || [ ! -d "$SDK/build-tools/34.0.0" ]; then
    log "accepting licenses + installing platform/build-tools (one-time)…"
    # NB: yes(1) dies of SIGPIPE when sdkmanager exits; with pipefail that must
    # never abort the script — hence the subshell + || true, followed by an
    # explicit existence check below.
    ( yes | "$SDKMANAGER" --licenses ) >/dev/null 2>&1 || true
    ( yes | "$SDKMANAGER" "platform-tools" "platforms;android-34" \
        "build-tools;34.0.0" ) >/dev/null 2>&1 || true
    [ -d "$SDK/platforms/android-34" ] || { echo "✗ SDK platform install failed"; exit 1; }
    [ -d "$SDK/build-tools/34.0.0" ] || { echo "✗ SDK build-tools install failed"; exit 1; }
fi

# ── 3. Gradle ────────────────────────────────────────────────────────────────
if [ ! -x "$GRADLE_BIN" ]; then
    log "downloading Gradle $GRADLE_VER (one-time, ~120MB)…"
    TMP="$(mktemp -d)"
    curl -fsSL --retry 3 -o "$TMP/g.zip" \
        "https://services.gradle.org/distributions/gradle-${GRADLE_VER}-bin.zip"
    unzip -q "$TMP/g.zip" -d "$HOME"
    rm -rf "$TMP"
fi

# ── 4. local.properties ──────────────────────────────────────────────────────
grep -q "^sdk.dir=" android/local.properties 2>/dev/null \
    || echo "sdk.dir=$SDK" > android/local.properties

# ── 5. signing keystore (stable across rebuilds — keep it!) ──────────────────
if [ ! -f android/keystore.properties ]; then
    STORE_PASS="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 20)"
    log "generating release keystore android/jarvis-release.keystore…"
    keytool -genkeypair -keystore android/jarvis-release.keystore \
        -alias jarvis -keyalg RSA -keysize 2048 -validity 3650 \
        -storepass "$STORE_PASS" -keypass "$STORE_PASS" \
        -dname "CN=JARVIS Remote, O=JARVIS, C=IN" >/dev/null 2>&1
    {
        echo "storeFile=jarvis-release.keystore"
        echo "storePassword=$STORE_PASS"
        echo "keyAlias=jarvis"
        echo "keyPassword=$STORE_PASS"
    } > android/keystore.properties
    chmod 600 android/keystore.properties android/jarvis-release.keystore
    log "keystore created (gitignored). Back it up or reinstalls will need a new app entry."
fi

# ── 6. bundle the dashboard CA so the app trusts it ──────────────────────────
if [ ! -f config/certs/ca.crt ]; then
    log "no CA found — running make_certs.sh first…"
    ./make_certs.sh >/dev/null
fi
cp config/certs/ca.crt android/app/src/main/res/raw/jarvis_ca.pem

# ── 7. build ─────────────────────────────────────────────────────────────────
log "building release APK (first run downloads AGP deps, be patient)…"
"$GRADLE_BIN" -p android --no-daemon \
    -PdashboardUrl="https://${LAN_IP}:8000/" \
    assembleRelease -q

mkdir -p "$DIST"
APK="$DIST/JARVIS-Remote-1.4.apk"
cp android/app/build/outputs/apk/release/app-release.apk "$APK"
log "✅ $APK ($(du -h "$APK" | cut -f1))  → sideload onto the phone"
log "   LAN IP baked in as default: https://${LAN_IP}:8000/ (URL button overrides)"
