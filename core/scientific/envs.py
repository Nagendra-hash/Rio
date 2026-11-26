"""
core/scientific/envs.py — interpreter resolution and dependency compatibility.

The machine already carries a large RIO venv (numpy, pandas, scipy,
scikit-learn, torch) and must not have it reshuffled. So there are two
interpreter profiles:

    core / viz   RIO's own .venv (matplotlib is added there — it pulls only
                 numpy/pillow/contourpy and cannot upgrade the scientific stack)
    stats / chem a separate ~/.jarvis-science-venv, created on demand, for
                 pingouin/statsmodels/seaborn and biopython/rdkit, whose
                 resolves would otherwise touch pandas/numpy

Nothing is installed automatically. `check_skill()` answers "can this skill run
here, and if not, exactly what is missing"; provisioning is an explicit,
approved action. That distinction is the whole point: a silent `pip install` in
a shared environment is how a research assistant breaks the trading desk.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

BASE_DIR = Path(__file__).resolve().parents[2]
RIO_VENV = BASE_DIR / ".venv"
SCIENCE_VENV = Path.home() / ".jarvis-science-venv"
REQUIREMENTS_SCIENCE = BASE_DIR / "requirements-science.txt"

PROFILE_INTERPRETER: dict[str, Path] = {
    "core": RIO_VENV / "bin" / "python",
    "viz": RIO_VENV / "bin" / "python",
    "stats": SCIENCE_VENV / "bin" / "python",
    "chem": SCIENCE_VENV / "bin" / "python",
}

# What each profile is expected to provide. Used for the human-readable status
# and for the provisioning command.
PROFILE_PACKAGES: dict[str, tuple[str, ...]] = {
    "core": ("numpy", "pandas", "scipy", "scikit-learn"),
    "viz": ("numpy", "pandas", "matplotlib"),
    "stats": ("numpy", "pandas", "scipy", "statsmodels", "pingouin", "matplotlib", "seaborn"),
    "chem": ("numpy", "pandas", "scipy", "biopython", "rdkit"),
}

_VERSION_CACHE: dict[tuple[str, tuple[str, ...]], dict[str, str | None]] = {}


def interpreter_for(profile: str) -> Path | None:
    """The interpreter that serves a profile, or None when it is not installed."""
    candidate = PROFILE_INTERPRETER.get(profile) or PROFILE_INTERPRETER["core"]
    if candidate.exists():
        return candidate
    # A missing science venv is not a hard failure: fall back to RIO's venv so
    # a skill can still report precisely WHICH packages are missing.
    fallback = RIO_VENV / "bin" / "python"
    return fallback if fallback.exists() else Path(sys.executable)


def science_venv_ready() -> bool:
    return (SCIENCE_VENV / "bin" / "python").exists()


def package_versions(python: Path, packages: Iterable[str]) -> dict[str, str | None]:
    """Installed versions for `packages` in `python`, via one child process."""
    names = tuple(sorted({p.strip() for p in packages if p and p.strip()}))
    key = (str(python), names)
    if key in _VERSION_CACHE:
        return _VERSION_CACHE[key]
    code = (
        "import importlib.metadata as m, json, sys\n"
        "out = {}\n"
        "for name in %r:\n"
        "    try:\n"
        "        out[name] = m.version(name)\n"
        "    except Exception:\n"
        "        out[name] = None\n"
        "print(json.dumps(out))\n" % (list(names),)
    )
    try:
        proc = subprocess.run(
            [str(python), "-c", code],
            capture_output=True, text=True, timeout=60,
            env={k: os.environ[k] for k in ("HOME", "PATH", "LANG") if k in os.environ},
        )
        data = json.loads(proc.stdout.strip() or "{}")
        result = {name: data.get(name) for name in names}
    except Exception:
        result = {name: None for name in names}
    _VERSION_CACHE[key] = result
    return result


def split_requirement(requirement: str) -> tuple[str, str]:
    """'pingouin==0.7.0' -> ('pingouin', '0.7.0'); bare names get ''."""
    raw = str(requirement or "").strip()
    for separator in ("==", ">=", "<=", "~="):
        if separator in raw:
            name, _, version = raw.partition(separator)
            return name.strip(), version.strip()
    return raw, ""


@dataclass(frozen=True)
class EnvCheck:
    profile: str
    interpreter: str
    status: str          # ready | missing | conflict | unsupported_python
    missing: tuple[str, ...] = ()
    mismatch: tuple[str, ...] = ()
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    def public(self) -> dict:
        return {
            "profile": self.profile,
            "interpreter": self.interpreter,
            "status": self.status,
            "missing": list(self.missing),
            "mismatch": list(self.mismatch),
            "detail": self.detail,
        }


def check_skill(skill) -> EnvCheck:
    """Is this skill's declared environment satisfied by its profile?"""
    profile = getattr(skill, "env_profile", "core") or "core"
    python = interpreter_for(profile)
    if python is None:
        return EnvCheck(profile, "", "missing", detail="no interpreter available")

    requirements = list(getattr(skill, "requires", ()) or ())
    names = [split_requirement(req)[0] for req in requirements]
    installed = package_versions(python, names)

    missing: list[str] = []
    mismatch: list[str] = []
    for requirement in requirements:
        name, wanted = split_requirement(requirement)
        have = installed.get(name)
        if have is None:
            missing.append(name)
        elif wanted and have != wanted:
            mismatch.append(f"{name}: need {wanted}, have {have}")

    if missing:
        return EnvCheck(
            profile, str(python), "missing",
            missing=tuple(sorted(set(missing))), mismatch=tuple(mismatch),
            detail=_install_hint(profile, missing),
        )
    if mismatch:
        return EnvCheck(
            profile, str(python), "conflict",
            mismatch=tuple(mismatch),
            detail=(
                "version mismatch — not auto-upgraded in a shared environment; "
                f"provision the {profile} profile or accept the installed version"
            ),
        )
    return EnvCheck(profile, str(python), "ready", detail="ready")


def _install_hint(profile: str, missing: list[str]) -> str:
    unique = sorted(set(missing))
    if profile in ("stats", "chem"):
        return (
            f"missing {', '.join(unique)} — create the science venv: "
            f"uv venv --python 3.12 {SCIENCE_VENV} && "
            f"uv pip install --python {SCIENCE_VENV}/bin/python "
            f"-r {REQUIREMENTS_SCIENCE.name}"
        )
    return (
        f"missing {', '.join(unique)} — install with: "
        f"{RIO_VENV}/bin/python -m pip install {' '.join(unique)}"
    )


def profile_report() -> dict:
    """Status of every profile, for `scientific_agent(action='skills')`."""
    out: dict[str, dict] = {}
    for profile, packages in PROFILE_PACKAGES.items():
        python = interpreter_for(profile)
        versions = package_versions(python, packages) if python else {}
        out[profile] = {
            "interpreter": str(python or ""),
            "exists": bool(python and Path(python).exists()),
            "science_venv": science_venv_ready() if profile in ("stats", "chem") else None,
            "packages": {name: versions.get(name) for name in packages},
        }
    return out


def clear_cache() -> None:
    _VERSION_CACHE.clear()
