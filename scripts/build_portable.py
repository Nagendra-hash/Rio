#!/usr/bin/env python3
"""build_portable.py — assemble a ready-to-run JARVIS bundle for a pendrive.

Prepares everything on a machine that HAS Python and (optionally) internet,
so the drive itself never needs to compile or download anything:

    python3 scripts/build_portable.py --linux     # relocatable runtime for Linux/macOS
    python3 scripts/build_portable.py --windows   # embedded Python 3.12 for Windows
    python3 scripts/build_portable.py --windows --offline-wheels
                                                  # also downloads .whl files so
                                                  # the drive installs with NO internet

What lands next to this repo (default ./jarvis-portable-build/, ~2-4 GB):
    jarvis-portable-build/
    ├── python/          the runtime (relocatable CPython or embedded distro)
    ├── site-packages/   ALL dependencies (the drive's only copy of them)
    ├── wheels/          (with --offline-wheels) pip cache for reinstall
    ├── get-pip.py       (Windows) offline bootstrap for a damaged runtime
    └── .deps-ok         marker that tells portable.bat/sh "deps are complete"

Copy the WHOLE repo folder + jarvis-portable-build/ contents onto the drive so
they merge into E:\\JARVIS\\jarvis-portable\\, run portable.bat / portable.sh,
done. The drive's config and memory live in jarvis-home/ on the drive, created
on first run.

Why the Linux runtime is NOT a copied venv: a venv hardcodes the build host's
interpreter path in bin/python (a symlink) and pyvenv.cfg, so the moment the
stick moves to another machine the runtime is dead. The python-build-standalone
distribution is built to be relocatable — bin/python3 resolves its own prefix
at run time, so the same folder runs on any machine of the same architecture.
"""

import argparse
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUILD_DIR = REPO / "jarvis-portable-build"
EMBED_URL = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip"
EMBED_NAME = "python-embed.zip"
GETPIP_URL = "https://bootstrap.pypa.io/get-pip.py"
DEPS_MARKER = ".deps-ok"

# Relocatable CPython for Linux/macOS (the same distribution uv ships). Pinned
# for reproducibility; override either value below if a newer one is wanted.
PBS_RELEASE = "20241016"
PBS_VERSION = "3.12.7"
PBS_TMPL = (
    "https://github.com/astral-sh/python-build-standalone/releases/"
    f"download/{PBS_RELEASE}/"
    f"cpython-{PBS_VERSION}%2B{PBS_RELEASE}-{{triple}}-install_only.tar.gz"
)

# The embeddable distribution ships with imports restricted by its ._pth file.
# This body replaces the stock one verbatim:
#   ..\site-packages      — the drive's dependency folder (sibling of python/)
#   Lib\site-packages     — where get-pip.py installs pip itself
# Paths inside ._pth resolve relative to the ._pth file's own directory.
PTH_BODY = "python312.zip\n.\n..\\site-packages\nLib\\site-packages\nimport site\n"


def run(cmd, **kw):
    print(f"[build] $ {' '.join(str(c) for c in cmd)}")
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def download(url: str, dest: Path) -> None:
    print(f"[build] downloading {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dest)


def write_marker() -> None:
    (BUILD_DIR / DEPS_MARKER).write_text("ok\n", encoding="utf-8")


def pbs_triple() -> str:
    system = platform.system().lower()
    machine = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
    }.get(platform.machine().lower(), "")
    if system == "linux" and machine:
        return f"{machine}-unknown-linux-gnu"
    if system == "darwin" and machine:
        return f"{machine}-apple-darwin"
    sys.exit(
        f"[build] --linux: no prebuilt runtime for "
        f"{platform.system()}/{platform.machine()}; use --linux-venv "
        f"on a Python 3.11+ machine instead."
    )


def build_linux(pbs_url: str | None) -> None:
    """Relocatable runtime: works on target machines with NO Python at all."""
    rt = BUILD_DIR / "python"
    sp = BUILD_DIR / "site-packages"
    if rt.exists():
        print(f"[build] removing old {rt}")
        shutil.rmtree(rt)

    url = pbs_url or PBS_TMPL.format(triple=pbs_triple())
    tarball = BUILD_DIR / "python-runtime.tar.gz"
    download(url, tarball)
    print("[build] unpacking relocatable CPython")
    with tarfile.open(tarball) as t:  # archive contains python/…
        t.extractall(BUILD_DIR, filter="tar")  # trusted upstream; keep exec bits
    tarball.unlink()
    py = rt / "bin" / "python3"
    if not py.exists():
        sys.exit(f"[build] runtime unpack did not produce {py} — layout changed?")

    run([py, "-c", "import sys; print('[build] runtime:', sys.version)"])
    print("[build] installing dependencies into site-packages/")
    run([py, "-m", "pip", "install", "-r", REPO / "requirements.txt", "--target", sp])
    write_marker()
    print(f"[build] ✓ runtime ready: {rt} (relocatable — runs anywhere)")
    print("[build] copy the repo folder + python/ + site-packages/ + .deps-ok")
    print("[build] into jarvis-portable/ on the drive; portable.sh finds them.")


def build_linux_venv() -> None:
    """Fallback for odd platforms: venv from the host Python.

    The result only runs on machines that have a Python of the same minor
    version at the same path — say so loudly instead of shipping a dead venv.
    """
    py = sys.executable
    if sys.version_info < (3, 11):
        sys.exit("[build] Python 3.11+ required to build the bundle.")
    venv_dir = BUILD_DIR / "python-venv"
    if venv_dir.exists():
        print(f"[build] removing old {venv_dir}")
        shutil.rmtree(venv_dir)
    print("[build] creating venv (NOTE: tied to this machine's Python)")
    run([py, "-m", "venv", venv_dir])
    vpy = venv_dir / "bin" / "python"
    run([vpy, "-m", "pip", "install", "-r", REPO / "requirements.txt"])
    print(f"[build] ✓ venv ready: {venv_dir}")
    print("[build] this venv is NOT relocatable — it only works on machines")
    print("[build] with the same Python at /usr/bin (see module docstring).")


def build_windows(offline_wheels: bool) -> None:
    rt = BUILD_DIR / "python"
    sp = BUILD_DIR / "site-packages"
    zip_path = BUILD_DIR / EMBED_NAME
    rt.mkdir(parents=True, exist_ok=True)

    if not (rt / "python.exe").exists():
        if not zip_path.exists():
            download(EMBED_URL, zip_path)
        print("[build] unpacking embedded Python")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(rt)
        # Enable imports in the embeddable distribution: point it at the
        # drive's dependency folder and at its own Lib\\site-packages (where
        # get-pip.py puts pip). See PTH_BODY for why these exact lines.
        pth = next(rt.glob("python*._pth"))
        pth.write_text(PTH_BODY, encoding="utf-8")
        # venv creation from an embedded distribution is unsupported (it ships
        # no venv/ensurepip machinery — python.org docs §4.2), so dependencies
        # install straight into the sibling site-packages folder the ._pth
        # already points at, via a pip bootstrapped with get-pip.py.
        print("[build] installing dependencies into site-packages/")
        host_py = sys.executable
        if platform.system() != "Windows" or sys.version_info[:2] != (3, 12):
            sys.exit(
                "[build] --windows needs to run under Windows Python 3.12 "
                "so pip builds/binary wheels match the embedded runtime."
            )
        run(
            [
                host_py,
                "-m",
                "pip",
                "install",
                "-r",
                REPO / "requirements.txt",
                "--target",
                sp,
            ]
        )
    else:
        print("[build] runtime already present, skipping install")

    write_marker()
    # Insurance for target machines with no internet: get-pip.py bundles the
    # pip wheel itself, so a damaged runtime can be repaired fully offline
    # together with the wheels/ folder.
    try:
        download(GETPIP_URL, BUILD_DIR / "get-pip.py")
    except Exception as e:
        print(f"[build] could not fetch get-pip.py (offline?): {e}")

    if offline_wheels:
        wh = BUILD_DIR / "wheels"
        wh.mkdir(exist_ok=True)
        print("[build] downloading wheels for offline reinstall …")
        run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "-r",
                REPO / "requirements.txt",
                "-d",
                wh,
            ]
        )

    print(f"[build] ✓ runtime ready: {rt}")
    print("[build] copy the repo folder + python/ + site-packages/ + .deps-ok")
    print("[build] (+ get-pip.py, wheels/ if built) into jarvis-portable/ on")
    print("[build] the drive; portable.bat picks them up automatically.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--linux",
        action="store_true",
        help="build a relocatable Linux/macOS runtime "
        "(python-build-standalone — runs on machines with no "
        "Python installed)",
    )
    g.add_argument(
        "--linux-venv",
        action="store_true",
        help="fallback: venv from this host's Python (NOT "
        "relocatable — same-machine use only)",
    )
    g.add_argument(
        "--windows",
        action="store_true",
        help="build the Windows embedded-Python runtime",
    )
    ap.add_argument(
        "--pbs-url",
        default=None,
        help="override the python-build-standalone tarball URL",
    )
    ap.add_argument(
        "--offline-wheels",
        action="store_true",
        help="also download wheels so the drive can reinstall "
        "with no internet at all",
    )
    args = ap.parse_args()

    if args.windows:
        build_windows(args.offline_wheels)
    elif args.linux_venv:
        build_linux_venv()
    else:
        build_linux(args.pbs_url)


if __name__ == "__main__":
    main()
