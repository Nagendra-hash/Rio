---
name: agent-reach
description: Use when the user wants to research, search, or read anything on the internet — especially Twitter/X, Reddit, Bilibili, V2EX, XiaoHongShu, YouTube transcripts, GitHub code search, RSS, or any web page. Routes each platform to its best available backend.
---

# Agent Reach (adapted for JARVIS)

From `Panniantong/Agent-Reach` (MIT). The `agent-reach` CLI is installed in
the project venv. Run `.venv/bin/agent-reach doctor` first — it reports each
channel as usable, needs-config, or not installed.

**Standing rule: use the routed command for a platform; do not invent another
scraper.** On login-state platforms say which backend you use, and follow the
retry chains in `references/<category>.md` rather than guessing.

## Working now (verified on this machine)

```bash
curl -s "https://r.jina.ai/<URL>"          # any web page → markdown
curl -s "https://www.v2ex.com/api/topics/hot.json" \
     -H "User-Agent: agent-reach/1.0"      # V2EX hot
gh search repos "q" --sort stars --limit 10 # GitHub
gh search code "q" --limit 10
.venv/bin/agent-reach doctor               # RSS: follow per-channel hint

# YouTube transcripts (yt-dlp installed; doctor prints the config fix
# if the JS runtime is missing — node is present)
yt-dlp --write-sub --write-auto-sub --skip-download -o "/tmp/%(id)s" "<URL>"
```

## Needs setup (doctor says exactly what)

- **Exa semantic search** — `npm i -g mcporter` + Exa key; `search.md`.
- **Twitter / Reddit / 小红书 / Bilibili / Facebook / Instagram** — cookie or
  browser-session login; retry chains in `social.md`. **Never log in for the
  user; never read browser cookies unasked.**
- **LinkedIn / Boss直聘** — `career.md`; Boss needs a dedicated CDP Chrome the
  user confirms visually. 雪球 — `finance.md`.

Research wide, then synthesise with citations. Not for posting (read-only),
and platforms with a dedicated JARVIS action use that first.