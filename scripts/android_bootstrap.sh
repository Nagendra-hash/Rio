#!/usr/bin/env bash
# android_bootstrap.sh — one-time install of the Android APK build toolchain.
#
# Installs (user-local, no sudo):
#   ~/.gradle-dist/gradle-8.7      (build tool, ~120 MB)
#   ~/.android-sdk/                (cmdline-tools + platforms;android-34 +
#                                   build-tools;34.0.0, ~400 MB)
#
# JDK 17 must already be present (java -version) — AGP 8.4 requires it.
# After this, dev_agent / the coding agent can build real APKs:
#   gradle assembleDebug  →  app/build/outputs/apk/debug/app-debug.apk
set -euo pipefail

SDK="${HOME}/.android-sdk"
GRADLE_HOME="${HOME}/.gradle-dist"
GRADLE_VER="8.7"
CMDTOOLS_ZIP="commandlinetools-linux-11076708_latest.zip"

if ! command -v java >/dev/null 2>&1; then
    echo "✗ JDK not found — install first: sudo apt install openjdk-17-jdk"
    exit 1
fi
echo "✓ JDK: $(java -version 2>&1 | head -1)"

# ── gradle ────────────────────────────────────────────────────────────────
if [ -x "${GRADLE_HOME}/gradle-${GRADLE_VER}/bin/gradle" ]; then
    echo "✓ gradle ${GRADLE_VER} already installed"
else
    echo "→ downloading gradle ${GRADLE_VER}..."
    mkdir -p "${GRADLE_HOME}"
    TMP="$(mktemp -d)"
    curl -fL --retry 3 -o "${TMP}/gradle.zip" \
        "https://services.gradle.org/distributions/gradle-${GRADLE_VER}-bin.zip"
    unzip -q "${TMP}/gradle.zip" -d "${GRADLE_HOME}"
    rm -rf "${TMP}"
    echo "✓ gradle installed: ${GRADLE_HOME}/gradle-${GRADLE_VER}"
fi

# ── Android SDK cmdline-tools ─────────────────────────────────────────────
if [ ! -x "${SDK}/cmdline-tools/latest/bin/sdkmanager" ]; then
    echo "→ downloading Android cmdline-tools..."
    TMP="$(mktemp -d)"
    curl -fL --retry 3 -o "${TMP}/${CMDTOOLS_ZIP}" \
        "https://dl.google.com/android/repository/${CMDTOOLS_ZIP}"
    mkdir -p "${SDK}/cmdline-tools"
    unzip -q "${TMP}/${CMDTOOLS_ZIP}" -d "${TMP}"
    mv "${TMP}/cmdline-tools" "${SDK}/cmdline-tools/latest"
    rm -rf "${TMP}"
    echo "✓ cmdline-tools installed"
else
    echo "✓ cmdline-tools already installed"
fi

export ANDROID_HOME="${SDK}"

# ── SDK packages ──────────────────────────────────────────────────────────
SDKM="${SDK}/cmdline-tools/latest/bin/sdkmanager"
echo "→ accepting licenses..."
yes | "${SDKM}" --licenses > /dev/null 2>&1 || true
echo "→ installing platform-tools, platforms;android-34, build-tools;34.0.0..."
yes | "${SDKM}" --sdk_root="${SDK}" \
    "platform-tools" "platforms;android-34" "build-tools;34.0.0" \
    | tail -2

# symlink gradle onto PATH (~/.local/bin is the house convention)
mkdir -p "${HOME}/.local/bin"
ln -sf "${GRADLE_HOME}/gradle-${GRADLE_VER}/bin/gradle" \
       "${HOME}/.local/bin/gradle"

echo
echo "══ DONE — toolchain ready ══"
echo "  gradle:  $(command -v gradle || echo "${GRADLE_HOME}/gradle-${GRADLE_VER}/bin/gradle")"
echo "  SDK:     ${SDK}"
echo "Now ask JARVIS to build the APK again (or: gradle assembleDebug in the project)."
