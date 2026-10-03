---
name: claude-mem
source: https://github.com/thedotmack/claude-mem
ref: main
capability: knowledge
skill_md: skills/claude-mem/SKILL.md
enabled: true
description: Memory compression system for Claude Code - persist context across sessions (claude-mem plugin)
hash: 348f365f126c429bcee5e5edb2d154f4d9a6b74648807a90ea0c2a6868471622
---

# claude-mem skills (Claude Code plugin: persistent memory)

`thedotmack/claude-mem` is a Claude Code plugin (v13.29.0) that compresses and
persists tool-use history across sessions. This install ships the plugin's
skills that map cleanly onto JARVIS' knowledge-skill convention:

- `mem-search` — search persistent cross-session memory DB (search → filter →
  fetch → disclose). Use when the user asks "did we already solve this?" or
  "how did we do X last time?".
- `do` — phased implementation plan execution via subagents (verify → verify →
  commit).
- `pathfinder` — (skill body as found in the plugin; install it as a skill
  package).

Install method: shallow clone `github.com/thedotmack/claude-mem`, copied
`plugin/skills/{mem-search,do,pathfinder}/SKILL.md` into `skills/claude-mem/`.
The plugin's own `.claude-plugin/plugin.json` (name `claude-mem`, version
13.29.0) is not part of this install — it targets the Claude Code plugin
directory, not this repo.

House rules: stdlib only, never executed at install time, recorded in
`skills-lock.json` with the commit hash of the upstream `main` branch.
