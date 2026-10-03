---
name: paperclip
description: Searches and reads biomedical papers, FDA/PMDA/EMA documents, clinical trials, and protein records with the GXL Paperclip CLI and Python SDK. Supports source-scoped search, full-text grep, metadata SQL, map/reduce extraction, figure analysis, optional repositories and claim verification, and line-pinned citations. Use when a task names GXL paperclip, asks to install or authenticate it, or requests literature retrieval and evidence extraction through Paperclip.
allowed-tools: Bash Read Write
license: MIT
compatibility: Requires network access and the GXL paperclip CLI. The macOS/Linux installer requires Python 3.8+ plus curl or wget; it installs a Python launcher and private library, not an interpreter. Use PAPERCLIP_API_KEY or existing browser-login credentials. Hosted MCP is available without local CLI installation. Reviewed against CLI/SDK 0.7.92.
metadata: 
  version: 1.5
  skill-author: K-Dense Inc.
  last-reviewed: 2026-09-30
  upstream-version: 0.7.92
  openclaw: 
    primaryEnv: PAPERCLIP_API_KEY
    envVars: 
      - name: PAPERCLIP_API_KEY
        required: false
        description: Paperclip API key from https://paperclip.gxl.ai/keys. Existing OAuth credentials also work; installation and help do not require a key.
---

# Paperclip CLI

Paperclip by GXL exposes scientific documents through a virtual filesystem and server-side search
and readers. This is **GXL Paperclip** at `paperclip.gxl.ai`, not the unrelated Paperclip agent-company
application. Use the source requested by the user; other providers have their own skills.

This revision checks official docs, the public API schema, and installed CLI/SDK **0.7.92**.
Local help, SDK request construction, and public metadata were checked. Authenticated retrieval,
LLM readers, uploads, and repository mutations were not run during this review; their examples
are illustrative. Server behavior can change independently of the CLI version.

## Preflight and authentication

```bash
command -v paperclip
paperclip --version
paperclip --help
```

If installed, use the existing account or a key configured privately at
`https://paperclip.gxl.ai/keys`. Do not ask for a key in chat or print credential files.
Paperclip does not automatically load a project `.env`. If the user has supplied a **trusted,
shell-compatible** `.env`, export it in the same shell invocation as the command:

```bash
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
paperclip config 2>&1 | grep -E 'Auth:|Health:'
```

Sourcing `.env` executes shell code: use only a trusted file, and keep it out of Git. Repeat the
export when a tool starts a fresh shell; an already-exported environment needs no prefix. A bearer
token can take precedence over an API key, and absent environment credentials may fall back to a
stored OAuth identity. Check the intended account rather than assuming any OAuth login is wrong.
`Auth` reports credential presence; `Health` probes public reachability. Neither proves the
credential is valid. Check authentication on the next task-authorized request.

Use browser `paperclip login` when the user is available to complete sign-in. Installation and
`paperclip install` also contain prompts; see [installation.md](references/installation.md).
Do not run update, uninstall, account changes, or uploads merely to test documentation.

## Choose the retrieval operation

| Goal | Command | Interpretation |
|---|---|---|
| Papers about a topic | `search -s pmc "..." -n 5` | Ranked discovery; summaries are triage |
| Exact terms in full text | `grep "TP53" /papers/` | May be time- or match-limited |
| Known DOI/PMID | `lookup doi 10.1073/pnas.2307796121` | Resolve identity before reading |
| Counts and metadata | `sql "SELECT ..."` | Metadata aggregation, not body-text search |
| Methodological analogues | `search -s arxiv --ranking analogical "..."` | Describe the method or problem in full sentences |
| The same fields across papers | `map --from s_ID "..."` | LLM extraction; verify material evidence |

Always pass a source or virtual directory to `search`. Comma-separated sources work, but separate
targeted queries are easier to interpret when mixing papers, trials, and regulatory evidence.
Use `paperclip skill proteins` before protein queries and `paperclip skill patents` before patents.

## Find, read, and cite

```bash
paperclip search -s pmc "CRISPR base editing delivery" -n 5
paperclip cat /papers/PMC10945750/meta.json
paperclip head -40 /papers/PMC10945750/content.lines
paperclip ls /papers/PMC10945750/sections/
paperclip grep -n "lipid nanoparticle" /papers/PMC10945750/content.lines
paperclip scan /papers/PMC10945750/content.lines "IC50" "off-target" "efficiency"
```

Capture the returned result ID; do not reuse the illustrative IDs in this skill. `s_` IDs identify
saved search/grep/filter cohorts, `m_` map runs, and `r_` reduce artifacts. Recover recent IDs with
`paperclip results --list`. Terminal output can be a truncated preview, so use saved results or
SDK `result.papers` for structured hits, not a regex over rendered paper titles. See
[search-and-retrieval.md](references/search-and-retrieval.md) and
[python-sdk.md](references/python-sdk.md).

Use absolute virtual paths. Prefer `head`, sections, `grep`, and `scan` to dumping a whole paper.
A failed read or empty, bounded grep result does not establish scientific absence.

### Multi-paper extraction

```bash
paperclip search -s pmc "lipid nanoparticle mRNA delivery" -n 5
# Substitute the actual search ID below.
paperclip filter --from s_ID "in vivo delivery with quantified efficiency"
paperclip map --from s_ID "Report delivery vector, target cell type, efficiency, and supporting lines. State 'not reported' when absent."
paperclip results m_ID --save map.txt
```

`filter` modifies its saved cohort in place. Save the original search separately if it must remain
reproducible. Start with 3–10 papers; readers incur service work and quota. Use Draft 2020-12 JSON
Schema through `--output-schema` when exact fields matter. For a persistent Paperclip extraction
dataset, inspect `paperclip routines show paperclip-data-extraction` and the current extraction
workflow before creating it. A returned routine does not authorize extra persistence or sharing.

`reduce --from m_ID --strategy table "Compare the results"` requests a table, but validate the actual
output and construct 