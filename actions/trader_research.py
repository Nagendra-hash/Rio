"""
trader_research.py — learn top traders' strategies from the live web.

One JARVIS tool that turns "study trader X" into a durable, citable asset:

  1. RESEARCH — web search (DuckDuckGo via actions.web_search's ddgs client)
     over trader-specific query templates: strategy rules, risk management,
     stock/option selection, famous trades, interviews/podcasts.
  2. WATCH — top trending YouTube videos for the trader are pulled through
     actions.youtube_transcribe (captions first, Gemini-watching fallback),
     so spoken lessons land in the dossier, not just articles.
  3. SYNTHESIZE — Gemini distills everything into a profile: arc, playbook,
     entries/exits, risk management, psychology, JARVIS-journal cross-check.
  4. INDEX — the dossier is chunked into PAGES and ingested into the RAG
     library (collection 'trading_books') so every claim is retrievable with
     a page number — same citation contract as the user's saved PDFs.
  5. VERSION — re-running research on the same trader creates the next
     version (older versions stay searchable unless pruned).

Research + education only. It never places trades and never guarantees
outcomes, and it marks third-party approximations as such.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path

from core.skill_registry import BASE_DIR  # repo root, same convention


# ── imports from sibling actions (guarded: the tool must degrade, not die) ──
def _web_results(query: str, n: int = 8) -> list[dict]:
    try:
        from actions.web_search import _ddg_search

        return _ddg_search(query, max_results=n) or []
    except Exception as e:
        print(f"[TraderResearch] web search failed: {e}")
        return []


def _yt_videos(trader: str, n: int = 4) -> list[dict]:
    """Trending YouTube videos for this trader via web search."""
    out = []
    for r in _web_results(f"{trader} trading strategy interview site:youtube.com", n):
        url = r.get("url", "")
        m = re.search(r"(?:watch\?v=|youtu\.be/)([\w-]{11})", url)
        if m:
            out.append(
                {"video_id": m.group(1), "title": r.get("title", ""), "url": url}
            )
    return out[:n]


def _transcripts(videos: list[dict], budget_s: int = 150) -> list[dict]:
    """Captions for each video (Gemini-watch fallback disabled in batch mode —
    it burns shared quota). Fails alone per video."""
    got = []
    if not videos:
        return got
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        api = YouTubeTranscriptApi()
    except Exception:
        return got
    t0 = time.monotonic()
    for v in videos:
        if time.monotonic() - t0 > budget_s:
            break
        try:
            tr = api.fetch(v["video_id"])
            text = " ".join(s.text for s in tr)
            if len(text) > 400:
                got.append({**v, "transcript": text[:40000]})
        except Exception:
            continue
    return got


def _sources_block(web: list[dict], vids: list[dict]) -> str:
    lines = []
    for r in web:
        if r.get("title") and r.get("url"):
            lines.append(f"- {r['title'][:90]} — {r['url']}")
    for v in vids:
        lines.append(
            f"- [video] {v.get('title', v.get('video_id','?'))} — "
            f"https://www.youtube.com/watch?v={v.get('video_id','')}"
        )
    return "\n".join(lines[:24])


def _synthesize(trader: str, web: list[dict], vids: list[dict]) -> str:
    """Gemini distills raw research into the dossier markdown."""
    from core import gemini

    web_text = "\n\n".join(
        f"[{r.get('title','')}] {r.get('snippet','')}" for r in web if r.get("snippet")
    )
    vid_text = "\n\n".join(
        f"[VIDEO: {v.get('title','')[:80]}]\n{v.get('transcript','')[:12000]}"
        for v in vids
    )
    prompt = f"""You are compiling a trading-strategy dossier on "{trader}" from web research material below.

Write a markdown dossier with EXACTLY these sections:
# Top Trader Dossier: {trader}
## Who (2-4 sentences: market, era, arc, credibility)
## Core Playbook (numbered rules with concrete parameters wherever the sources give them: setups, entry/exit conditions, timeframes, indicators, selection filters)
## Risk Management (position sizing %, stops, daily limits — as stated or implied)
## Psychology & Discipline (their stated principles)
## How To Apply In Indian Markets (adaptation notes; note what maps to NSE/BSE instruments)
## What Is Unverified (explicitly list claims that are rumor/approximation/marketing)
## Sources

Rules: only claims supported by the material (or clearly mark inference).
Concrete numbers beat vague advice. If material is thin, say so — never invent.

MATERIAL:
{web_text[:14000]}

{vid_text[:24000]}
"""
    out = gemini.text(prompt, tier=gemini.SMART, timeout_ms=180000)
    return out or ""


def _dossier_path(trader: str) -> Path:
    safe = re.sub(r"[^a-z0-9]+", "_", trader.lower()).strip("_") or "trader"
    return BASE_DIR / "knowledge" / "library" / f"trader_{safe}.md"


def _dossier_to_pdf(md_path: Path) -> Path:
    """Render a dossier markdown into a paged PDF: one PDF page per H1/H2
    section (page numbers in RAG citations then map to dossier sections)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas

    pdf_path = md_path.with_suffix(".pdf")
    src = md_path.read_text(encoding="utf-8")
    sections = re.split(r"\n(?=#{1,2} )", src)
    w, h = A4
    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    for part in sections:
        y = h - 50
        for raw in part.strip().splitlines():
            for ln in simpleSplit(raw, "Helvetica", 10, w - 100) or [""]:
                if y < 50:
                    c.showPage()
                    y = h - 50
                c.drawString(50, y, ln[:6000])
                y -= 13
        c.showPage()
    c.save()
    return pdf_path


def _ingest_dossier(path: Path, title: str) -> dict:
    from core import rag

    pdf = _dossier_to_pdf(path)
    return rag.ingest(
        str(pdf), collection="trading_books", title=title, source="trader_research"
    )


def trader_research(parameters: dict, player=None, **_) -> str:
    p = parameters or {}
    trader = str(p.get("trader", "")).strip()
    op = (p.get("action") or "research").strip().lower()

    if op == "list":
        files = sorted((BASE_DIR / "knowledge" / "library").glob("trader_*.md"))
        if not files:
            return "No trader dossiers yet — ask me to research a trader."
        reg = _reg()
        lines = []
        for f in files:
            name = f.stem.replace("trader_", "").replace("_", " ")
            e = reg.get(_doc_id_for(name, reg))
            lines.append(
                f"- {name} — {f.stat().st_size // 1024} KB"
                + (
                    f" (v{e.get('version')}, {e.get('chunk_count')} chunks)"
                    if e
                    else " (not indexed)"
                )
            )
        return "Trader dossiers in the library:\n" + "\n".join(lines)

    if not trader:
        return (
            "Name a trader to research, sir — for example "
            "'research Takashi Kotegawa' or 'study Gautam Jha'."
        )

    trader = trader.strip()

    if op == "research":
        # 1) gather
        web = _web_results(f"{trader} trading strategy rules risk management", 8)
        web += _web_results(f"{trader} trading style famous trades interview", 6)
        vids = _yt_videos(trader, 4)
        trans = _transcripts(vids)
        if not web and not trans:
            return f"Found nothing substantial on '{trader}' — try the full name or check the spelling."
        print(
            f"[TraderResearch] {trader}: {len(web)} web hits, "
            f"{len(trans)}/{len(vids)} transcripts"
        )

        # 2) synthesize (Gemini); degrade to a curated raw-material doc if the
        #    model tier is unavailable — an honest dossier beats none.
        dossier = _synthesize(trader, web, trans)
        if not dossier:
            raw = "\n\n".join(
                f"[{r.get('title','')}] {r.get('snippet','')}"
                for r in web
                if r.get("snippet")
            )
            dossier = (
                f"# Top Trader Dossier: {trader}\n\n"
                "(synthesis unavailable — raw research material below)\n\n"
                + raw
                + "\n\n"
                + _sources_block(web, vids)
            )
        if "## Sources" not in dossier:
            dossier += "\n\n## Sources\n" + _sources_block(web, vids)

        # 3) persist + index (page-cited like any book)
        path = _dossier_path(trader)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if "# Top Trader Dossier" in dossier:
            dossier = re.sub(r"\(researched [0-9-]+\)", "", dossier)
            dossier = dossier.replace(
                "# Top Trader Dossier:",
                f"# Top Trader Dossier: (researched {stamp})  ",
                1,
            )
        path.write_text(dossier, encoding="utf-8")
        title = f"Top Trader Playbook: {trader}"
        ing = _ingest_dossier(path, title)
        if ing.get("ok"):
            n_v = len(vids)
            return (
                f"Research complete: '{trader}' → {title}, indexed as "
                f"v{ing.get('version')} ({ing.get('pages')} pages / "
                f"{ing.get('chunks')} chunks, page-cited). "
                f"Sources: {len(web)} web + {n_v} videos "
                f"({len(trans)} transcribed). Ask me anything about "
                f"{trader}'s playbook — every answer now cites the dossier."
            )
        return (
            f"Dossier saved to {path.name} but indexing failed: " f"{ing.get('error')}"
        )

    if op == "forget":
        path = _dossier_path(trader)
        doc_id = _doc_id_for(f"Top Trader Playbook: {trader}")
        if doc_id:
            from core import rag

            rag.forget(doc_id)
        if path.exists():
            path.unlink()
        pdf = path.with_suffix(".pdf")
        if pdf.exists():
            pdf.unlink()
        return f"Removed the {trader} dossier from the library."

    return "Use action='research' (default), 'list', or 'forget'."


def _reg():
    try:
        from core import rag

        return rag._load_registry()
    except Exception:
        return {}


def _doc_id_for(title: str, reg: dict | None = None):
    try:
        from core import rag

        reg = reg or rag._load_registry()
        for doc_id, e in reg.items():
            if e.get("title") == title:
                return doc_id
    except Exception:
        pass
    return None


TOOL = {
    "name": "trader_research",
    "description": (
        "Research a top/legendary trader's strategy from the live web and "
        "YouTube (articles, interviews, trending videos), synthesize a "
        "playbook dossier, and index it into JARVIS' knowledge library with "
        "page-cited retrieval. Use when the user names a trader to learn or "
        "study (e.g. Takashi Kotegawa, Gautam Jha, any famous trader), or "
        "asks to update/refresh a trader's playbook. Also 'list' to show "
        "researched traders. Advice quality rule: afterwards, ground trade "
        "opinions in the dossier + the trading-experience journal evidence."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "trader": {
                "type": "STRING",
                "description": "Trader name, e.g. 'Takashi Kotegawa' or 'Gautam Jha'",
            },
            "action": {
                "type": "STRING",
                "description": "research (default) | list | forget",
            },
        },
        "required": [],
    },
    "handler": trader_research,
}
