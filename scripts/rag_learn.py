#!/usr/bin/env python3
"""
scripts/rag_learn.py — operate the self-directed RAG knowledge learner.

The learner runs by itself once a day inside JARVIS (main.py background task),
researching a rotating set of topics from the web and merging the findings into
cited dossiers in the RAG collection "web_knowledge". This CLI is for running
it on demand, inspecting what it knows, and steering its curiosity.

    python scripts/rag_learn.py status
    python scripts/rag_learn.py run                 # one cycle now
    python scripts/rag_learn.py run "quantum computing, rust async"
    python scripts/rag_learn.py topics
    python scripts/rag_learn.py add "macro economics"
    python scripts/rag_learn.py remove "macro economics"
    python scripts/rag_learn.py gaps
    python scripts/rag_learn.py config enabled=false
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core import rag_learner  # noqa: E402


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _parse_topics(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [t.strip() for t in raw.split(",") if t.strip()] or None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RAG web knowledge learner")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="learner state and counters")
    p_run = sub.add_parser("run", help="run one learning cycle now")
    p_run.add_argument("topics", nargs="?", help="comma-separated topics")
    sub.add_parser("topics", help="topics, weights and dossier ids")
    p_add = sub.add_parser("add", help="add/boost a topic")
    p_add.add_argument("topic")
    p_rm = sub.add_parser("remove", help="remove a topic (keeps its dossier)")
    p_rm.add_argument("topic")
    sub.add_parser("gaps", help="questions the library could not answer")
    p_cfg = sub.add_parser("config", help="merge keys into the config, e.g. enabled=false")
    p_cfg.add_argument("assignments", nargs="+", help="key=value ...")

    args = ap.parse_args(argv)

    if args.cmd == "status":
        _print(rag_learner.status())
    elif args.cmd == "run":
        _print(rag_learner.run_cycle(force=True,
                                     topics=_parse_topics(args.topics)))
    elif args.cmd == "topics":
        _print(rag_learner.list_topics(limit=200))
    elif args.cmd == "add":
        _print(rag_learner.add_topic(args.topic))
    elif args.cmd == "remove":
        _print(rag_learner.remove_topic(args.topic))
    elif args.cmd == "gaps":
        path = rag_learner._gaps_file()
        rows = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines()[-50:]:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        except FileNotFoundError:
            pass
        _print(rows)
    elif args.cmd == "config":
        patch = {}
        for item in args.assignments:
            if "=" not in item:
                continue
            k, v = item.split("=", 1)
            low = v.strip().lower()
            if low in ("true", "false"):
                patch[k.strip()] = low == "true"
            else:
                try:
                    patch[k.strip()] = int(v)
                except ValueError:
                    try:
                        patch[k.strip()] = float(v)
                    except ValueError:
                        patch[k.strip()] = v
        _print(rag_learner.save_config(patch))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
