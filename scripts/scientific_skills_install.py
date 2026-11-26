#!/usr/bin/env python3
"""
scientific_skills_install.py — provenance lock for the vendored skill library.

Writes and verifies `SCIENTIFIC_SKILLS_LOCK.json`: the upstream tag and commit,
and per-skill sha256 + size + declared version. The vendored tree is never
modified, so this file is how "what exactly is installed, and did it change?"
is answered without trusting a directory listing.

Usage:
    python scripts/scientific_skills_install.py --write-lock
    python scripts/scientific_skills_install.py --verify
    python scripts/scientific_skills_install.py --summary
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.scientific.config import (  # noqa: E402
    CORE_SKILLS,
    GUARDED_SKILLS,
    LOCK_PATH,
    SKILLS_ROOT,
    UPSTREAM_COMMIT,
    UPSTREAM_REPO,
    UPSTREAM_TAG,
)
from core.scientific.skill_index import build_index, sha256_tree  # noqa: E402

LOCK_VERSION = 1


def write_lock() -> dict:
    index = build_index()
    skills = {}
    for name, skill in sorted(index.skills.items()):
        skills[name] = {
            "version": skill.version,
            "sha256": skill.sha256,
            "size_bytes": skill.size_bytes,
            "env_profile": skill.env_profile,
            "core": skill.core,
            "guarded": bool(skill.guarded_reason),
            "requires": list(skill.requires),
            "required_env": list(skill.required_env),
            "path": str(Path(skill.path).relative_to(BASE)),
        }
    payload = {
        "version": LOCK_VERSION,
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": {
            "repo": UPSTREAM_REPO,
            "tag": UPSTREAM_TAG,
            "commit": UPSTREAM_COMMIT,
            "license": "MIT",
            "vendored_files": "skills/ only (repo tests/docs/.github not vendored)",
        },
        "counts": {
            "skills": len(skills),
            "core": len([s for s in skills.values() if s["core"]]),
            "guarded": len([s for s in skills.values() if s["guarded"]]),
            "bytes": sum(s["size_bytes"] for s in skills.values()),
        },
        "skills": skills,
    }
    LOCK_PATH.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    return payload


def verify_lock() -> int:
    if not LOCK_PATH.exists():
        print(f"lock missing: {LOCK_PATH}. Run --write-lock first.", file=sys.stderr)
        return 2
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    recorded = lock.get("skills") or {}
    on_disk = {}
    for directory in sorted(p for p in SKILLS_ROOT.iterdir() if p.is_dir()):
        if not (directory / "SKILL.md").exists():
            continue
        on_disk[directory.name] = sha256_tree(directory)

    missing = sorted(set(recorded) - set(on_disk))
    extra = sorted(set(on_disk) - set(recorded))
    changed = sorted(
        name
        for name in set(recorded) & set(on_disk)
        if recorded[name].get("sha256") != on_disk[name]
    )
    problems = bool(missing or extra or changed)
    print(f"lock: {lock.get('source', {}).get('tag')} @ "
          f"{lock.get('source', {}).get('commit', '')[:12]}")
    print(f"recorded={len(recorded)} on_disk={len(on_disk)} "
          f"missing={len(missing)} extra={len(extra)} changed={len(changed)}")
    for label, names in (("missing", missing), ("extra", extra), ("changed", changed)):
        for name in names[:20]:
            print(f"  {label}: {name}")
    return 1 if problems else 0


def summary() -> int:
    index = build_index()
    stats = index.summary()
    total_bytes = sum(s.size_bytes for s in index.skills.values())
    print(json.dumps({**stats, "vendored_bytes": total_bytes,
                      "vendored_mb": round(total_bytes / 1024**2, 1)}, indent=1))
    print("\nenabled by default:")
    for name in CORE_SKILLS:
        skill = index.get(name)
        if skill is None:
            print(f"  MISSING  {name}")
            continue
        flags = []
        if skill.guarded_reason:
            flags.append("guarded")
        if skill.required_env:
            flags.append("needs " + ",".join(skill.required_env))
        print(f"  {name:32} v{skill.version:6} {skill.env_profile:6} "
              f"{('; '.join(flags))}")
    print("\nguarded (never enabled by default):")
    for name, reason in GUARDED_SKILLS.items():
        print(f"  {name:32} {reason}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Scientific skill provenance lock")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--write-lock", action="store_true")
    group.add_argument("--verify", action="store_true")
    group.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    if args.write_lock:
        payload = write_lock()
        print(f"wrote {LOCK_PATH} ({payload['counts']['skills']} skills, "
              f"{payload['counts']['bytes']/1024**2:.1f} MB)")
        return 0
    if args.verify:
        return verify_lock()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
