#!/usr/bin/env python3
"""Regression tests for pendrive mode (portable.bat / portable.sh / build_portable.py).

Locks down the class of bugs found when the portable setup was audited
(2026-10-02), none of which could be caught by running the app on the dev
machine — they only bite on a target PC:

1. The Windows setup flow bootstrapped pip via `python.exe -m venv` from the
   EMBEDDABLE distribution. The embeddable package ships no pip and no
   ensurepip machinery (python.org docs, "The embeddable package": "Using pip
   to manage dependencies as for a regular Python installation is not
   supported"), so first-run setup could never finish on any machine. The flow
   must bootstrap via get-pip.py and install into jarvis-portable/site-packages
   (--target), never create a venv from the embedded runtime.

2. The ._pth file that re-enables imports in the embedded distribution carried
   `..\\..\\..\\site-packages` — three levels up from jarvis-portable\\python\\,
   which resolves to the DRIVE ROOT (E:\\site-packages), so installed
   dependencies were invisible. It must be exactly `..\\site-packages`, and
   launcher + build script must write the SAME body.

3. `build_portable.py --linux` shipped a copied venv. A venv hardcodes the
   build host's interpreter path in bin/python (symlink) and pyvenv.cfg, so the
   runtime was dead on any other machine. The default must be the relocatable
   python-build-standalone distribution; the venv path must be an explicitly
   non-relocatable fallback (--linux-venv).

4. portable.sh used `readlink -f` and `sort -V` — GNU coreutils absent on the
   stock macOS bash 3.2 the script is supposed to support.

5. Interrupted dependency installs (user pulls the stick mid-install) must be
   detected on next launch via the jarvis-portable/.deps-ok marker, not launch
   into a half-installed runtime.

Plus one live check of what the launchers exist to guarantee: with JARVIS_HOME
set, every writable-path owner (config_manager, memory_manager, the
conversation journal) resolves under that folder.

Usage:
    .venv/bin/python -m pytest tests/test_portable_mode.py -q
    .venv/bin/python tests/test_portable_mode.py
Exit codes (standalone): 0 = pass, 1 = regression, 2 = environment error.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

SH = REPO_ROOT / "portable.sh"
BAT = REPO_ROOT / "portable.bat"
BUILD = REPO_ROOT / "scripts" / "build_portable.py"

try:
    SH_TEXT = SH.read_text(encoding="utf-8")
    BAT_TEXT = BAT.read_text(encoding="utf-8", errors="replace")
    BUILD_TEXT = BUILD.read_text(encoding="utf-8")
except Exception as e:  # repo layout broken
    print(f"❌ ENVIRONMENT ERROR — cannot read launcher sources: {e}")
    sys.exit(2)


def _code_lines(text: str) -> list[str]:
    """Lines with comments stripped (a '#' mid-line or an 'REM' line), so
    documentation can mention a forbidden token without tripping the test —
    the same guard style as tests/test_flight_url.py's source scan."""
    out = []
    for ln in text.splitlines():
        stripped = ln.lstrip()
        if stripped.startswith("#") or stripped.startswith("REM "):
            continue
        out.append(ln.split("#", 1)[0])
    return out


SH_CODE = "\n".join(_code_lines(SH_TEXT))
BAT_CODE = "\n".join(_code_lines(BAT_TEXT))
BUILD_CODE = "\n".join(_code_lines(BUILD_TEXT))


# ── 1. Windows setup: no venv from the embedded runtime; get-pip instead ────


def test_windows_setup_never_creates_a_venv():
    assert "-m venv" not in BAT_CODE, (
        "portable.bat creates a venv from the embedded distribution — that "
        "runtime ships no pip/ensurepip (python.org docs §The embeddable "
        "package), so `python.exe -m venv` cannot bootstrap a usable venv and "
        "first-run setup dies partway. Bootstrap pip with get-pip.py and "
        "install --target jarvis-portable\\site-packages instead."
    )


def test_windows_setup_bootstraps_pip_and_targets_site_packages():
    assert "get-pip.py" in BAT_CODE and "%GETPIP%" in BAT_CODE, (
        "portable.bat must fetch/execute get-pip.py — the embedded "
        "distribution ships no pip of its own."
    )
    assert '--target "%SITE_PKG%"' in BAT_CODE, (
        "dependencies must install --target into jarvis-portable\\site-packages"
        " (the ._pth points there), not into the runtime directory."
    )


# ── 2. The ._pth layout: exactly one .. — and identical in both writers ─────


def _pth_body_from_build() -> str:
    for var in ("PTH_BODY",):
        for ln in BUILD_TEXT.splitlines():
            if ln.startswith(f"{var} = "):
                return (
                    ln.split("= ", 1)[1]
                    .strip()
                    .strip('"')
                    .encode()
                    .decode("unicode_escape")
                )
    raise AssertionError("build_portable.py no longer defines PTH_BODY")


def test_pth_points_one_level_up_not_to_drive_root():
    body = _pth_body_from_build()
    assert (
        "..\\site-packages" in body
    ), f"PTH_BODY lost the site-packages line: {body!r}"
    assert "..\\..\\.." not in body, (
        "The regression is back: ..\\..\\..\\site-packages resolves to the "
        "DRIVE ROOT (E:\\site-packages), not jarvis-portable\\site-packages — "
        "the ._pth file sits in jarvis-portable\\python\\, so ONE '..' is the "
        "dependency folder."
    )
    assert "import site" in body, (
        "._pth must end with 'import site' or the embedded distribution "
        "keeps imports disabled entirely."
    )


def test_launcher_and_build_write_the_same_pth_body():
    # The launcher regenerates the ._pth on setup; the build script writes its
    # own. If these drift, a build-prepared drive and a self-setup drive end up
    # with different import paths.
    build_body = _pth_body_from_build()
    bat_block = BAT_CODE.split('> "%RUNTIME%\\python312._pth"')[1]
    bat_body = []
    for raw in bat_block.splitlines():
        ln = raw.strip()
        if ln in ("(", ""):
            continue  # the block opener sits on the marker line
        if ln == ")":
            break  # end of the redirect block
        if ln.startswith("echo "):
            bat_body.append(ln[5:])
        else:
            break
    assert bat_body, "portable.bat no longer writes the ._pth file itself"
    for line in [l for l in build_body.splitlines() if l]:
        assert line in bat_body, (
            f"._pth line {line!r} is in build_portable.py but missing from "
            f"portable.bat's generated ._pth ({bat_body}) — the two writers "
            "have drifted."
        )


# ── 3. Linux runtime must be relocatable (no shipped dead venv) ──────────────


def test_linux_build_defaults_to_relocatable_runtime():
    assert "python-build-standalone" in BUILD_CODE, (
        "build_portable.py --linux must download the relocatable "
        "python-build-standalone distribution. A copied venv hardcodes the "
        "build host's interpreter path and is dead on any other machine."
    )
    assert "--linux-venv" in BUILD_CODE, (
        "the venv fallback must be a separate, explicitly non-relocatable "
        "option (--linux-venv), not the default behavior."
    )


# ── 4. portable.sh stays macOS bash 3.2 compatible ───────────────────────────


def test_portable_sh_avoids_gnu_only_tools():
    for tool in ("readlink -f", "sort -V"):
        assert tool not in SH_CODE, (
            f"portable.sh uses '{tool}', which stock macOS (bash 3.2, no GNU "
            "coreutils) does not have — the launcher must work there too."
        )


def test_portable_sh_is_valid_bash():
    bash = shutil.which("bash")
    if not bash:
        print("⚠️  SKIP  test_portable_sh_is_valid_bash (no bash on PATH)")
        return
    r = subprocess.run([bash, "-n", str(SH)], capture_output=True, text=True)
    assert r.returncode == 0, f"portable.sh has a syntax error: {r.stderr}"


# ── 5. Interrupted installs are detected via the .deps-ok marker ─────────────


def test_deps_marker_contract():
    assert ".deps-ok" in SH_CODE and ".deps-ok" in BAT_CODE, (
        "both launchers must gate launch on jarvis-portable/.deps-ok so a "
        "stick pulled mid-install is repaired on next run instead of starting "
        "a half-installed runtime."
    )
    assert ".deps-ok" in BUILD_CODE and 'DEPS_MARKER = ".deps-ok"' in BUILD_TEXT, (
        "build_portable.py must write .deps-ok when its install completes — "
        "the launchers treat its absence as an interrupted install."
    )
    # The launcher's repair path reinstalls when the marker is missing.
    assert (
        "Dependencies incomplete" in SH_CODE and "Dependencies incomplete" in BAT_CODE
    )


# ── 6. JARVIS_HOME: the reason pendrive mode exists at all ───────────────────


def test_launchers_set_jarvis_home_to_the_drive():
    assert "export JARVIS_HOME=" in SH_CODE
    assert 'set "JARVIS_HOME=' in BAT_CODE
    # Both must point INSIDE the drive (jarvis-home/), and portable.sh must
    # export it so child processes (the app, dashboard server) inherit it.
    assert "jarvis-home" in SH_CODE and "jarvis-home" in BAT_CODE


def test_writable_paths_follow_jarvis_home(tmp_path=None):
    """The real contract: with JARVIS_HOME set, every writable-path owner
    resolves under that folder — this is what makes memories/keys/journal ride
    the stick instead of the host."""
    home = Path(tmp_path or tempfile.mkdtemp(prefix="jarvis-home-test-"))
    env = dict(os.environ, JARVIS_HOME=str(home))
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "import memory.config_manager as cm\n"
        "import memory.memory_manager as mm\n"
        "import memory.conversation_journal as cj\n"
        "assert Path(cm.CONFIG_FILE).is_relative_to(Path(%r)), cm.CONFIG_FILE\n"
        "assert Path(mm.MEMORY_PATH).is_relative_to(Path(%r)), mm.MEMORY_PATH\n"
        "assert cj.JOURNAL_DIR.is_relative_to(Path(%r)), cj.JOURNAL_DIR\n"
        "print('ok')\n"
    ) % (str(REPO_ROOT), str(home), str(home), str(home))
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=tempfile.gettempdir(),
        timeout=120,
    )
    assert r.returncode == 0, (
        "a writable-path owner does not honour JARVIS_HOME:\n" f"{r.stdout}{r.stderr}"
    )


# ── Standalone runner (repo convention; exit 0/1/2) ──────────────────────────

TESTS = [
    test_windows_setup_never_creates_a_venv,
    test_windows_setup_bootstraps_pip_and_targets_site_packages,
    test_pth_points_one_level_up_not_to_drive_root,
    test_launcher_and_build_write_the_same_pth_body,
    test_linux_build_defaults_to_relocatable_runtime,
    test_portable_sh_avoids_gnu_only_tools,
    test_portable_sh_is_valid_bash,
    test_deps_marker_contract,
    test_launchers_set_jarvis_home_to_the_drive,
    test_writable_paths_follow_jarvis_home,
]


def main() -> int:
    failures = 0
    for t in TESTS:
        try:
            t()
            print(f"✅ PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"❌ FAIL  {t.__name__}\n        {str(e)[:400]}")
        except Exception as e:
            failures += 1
            print(f"❌ ERROR {t.__name__}: {type(e).__name__}: {str(e)[:200]}")
    print("─" * 60)
    print(
        "portable mode:", "CLEAN ✅" if failures == 0 else f"{failures} FAILURE(S) ❌"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
