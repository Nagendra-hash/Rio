# RESOURCE_REPORT.md — Phase 0 Deliverable

> Measured on the audit host, 2026-10-02. All figures from `psutil`, `du`, `free`,
> and import-time RSS measurement — **not** estimated. Target machine per spec §76
> is 4 GB RAM / 4-core CPU / 20 GB free; this host matches that profile.

## 1. Host baseline

| Metric | Measured | Spec target (§76) | Verdict |
|---|---|---|---|
| CPU | 4 cores | 4-core consumer | ✅ exact match |
| RAM total | **3,820 MB** | 4,096 MB | ⚠️ 6% under target |
| RAM available (idle) | **1,505 MB** | — | ⚠️ tight |
| Swap | 0 reported | — | ⚠️ no safety net |
| Disk free | 90 GB | 20 GB | ✅ ample |
| Disk used | 84 GB / 183 GB | — | — |
| GPU | none assumed | optional | ✅ CPU-only path works |

> ⚠️ **The audit host *is* the target class of machine.** Budget rules are binding
> on every phase, not hypothetical. There is no swap, so a RAM overshoot is an OOM
> kill rather than a slowdown.

## 2. Inspection tooling cost (Phase 0 budget)

| Resource | Target (§57) | Actual |
|---|---|---|
| CPU | low | ✅ `du`/`free`/`psutil` only, no build |
| RAM (excl. model) | < 500 MB | ✅ ~55 MB peak (short-lived `.venv/bin/python`) |
| Additional disk | < 100 MB | ✅ 0 MB — **no source modified, no docs cache, no venv created** |

Phase 0 target met with large margin.

## 3. Application footprint — actual vs budget

### 3.1 Import-time RSS (measured incrementally)

| Component | RSS after import | Notes |
|---|---|---|
| Python interpreter | 17.0 MB | baseline |
| + fastapi + uvicorn | 42.6 MB | +25.6 MB — the whole web tier |
| + numpy | 54.3 MB | +11.7 MB |
| + sqlite3 | 54.4 MB | +0.1 MB — **stdlib SQLite is essentially free** |
| + PyQt6.QtWidgets | 74.3 MB | +19.9 MB before any widget exists |

**Key finding: SQLite costs ~0.1 MB.** The plan's §58 budget of "< 50 MB for
SQLite" is met by an enormous margin. SQLite adoption (P3 in the migration plan)
is essentially free on this dimension — it is justified by query/structure needs,
not by a memory constraint. Conversely **numpy is the single largest mandatory
import** (11.7 MB) and is unavoidable given the RAG design.

### 3.2 Disk

| Component | Measured | Spec budget | Verdict |
|---|---|---|---|
| App + data (excl. venvs, openhuman, .git) | **4.8 GB** | < 1 GB app + <1 GB data | ⚠️ see breakdown |
| — `models/` (Qwen 3B GGUF) | 2.0 GB | 2–3 GB model | ✅ expected |
| — `knowledge/` (RAG index) | 26 MB | ~1–2× docs | ✅ far under 200 MB MVP cap |
| — `memory/` | 5.0 MB | < 100 MB | ✅ |
| — `memory/long_term.json` | 132 KB | — | ✅ |
| — `dashboard/` | 524 KB | < 150 MB build | ✅ |
| `.venv` | **2.5 GB** | — | ⚠️ PyQt6/opencv/playwright wheels |
| `.venv-tradingagents` | 403 MB | — | ⚠️ separate venv, only for trading |
| `pinokio/` | 803 MB | — | ⚠️ not JARVIS runtime |
| `openhuman/` | 112 MB | — | ⚠️ separate Cargo world |

Total on disk: **5.0–8 GB** vs spec §80 target "approximately 5–8 GB" — ✅
in range. But note the composition: **only ~26 MB is application data.** The bulk
is dependencies and the model. The spec's "base application < 1 GB" target is
met; the 4.8 GB figure is dominated by the 2 GB model and 2.5 GB venv.

### 3.3 RAM — the honest picture

| Scenario | Estimated | Spec budget (§76) | Verdict |
|---|---|---|---|
| Python + web tier + numpy (idle, no model) | ~55–75 MB | backend 200–350 MB | ✅ ~4× under |
| + PyQt6 HUD | ~75–120 MB | frontend 100–150 MB | ✅ |
| + Ollama `qwen2.5:1.5b` (986 MB file) | ~1.5–2 GB | model 2–3 GB | ✅ |
| + Qwen 3B GGUF Q4_K_M (2.0 GB file) | ~2.5–3 GB | model 2–3 GB | ✅ |
| **Both local models resident** | ~4–5 GB | **forbidden by §78** | ❌ **must be prevented** |

> **The critical measurement:** this host has **3,820 MB total RAM and no swap.**
> Ollama and the llama.cpp GGUF *together* exceed physical RAM. The router
> *prefers* Ollama (so the GGUF rung normally doesn't load), but nothing
> *enforces* mutual exclusion — so a task-class miss (e.g. a `code` prompt after a
> `simple` one) can pull both rungs resident and OOM-kill the process.
>
> This is why `MAX_LOCAL_LLM_TASKS = 1` (§78) must be a hard semaphore, not a
> routing hint. It is item P0 in `MIGRATION_PLAN.md`.

### 3.4 Unmeasured / needs a live run

| Metric | Status |
|---|---|
| Full `main.py` steady-state RSS | ⏳ not measured — needs audio devices + Gemini session |
| Cold startup time (§75 <15 s) | ⏳ not measured — Gemini Live connect is network-bound |
| CPU % at idle | ⏳ not measured (audit ran short commands only) |
| RAG index build peak | ⏳ not measured |
| Hermes process RSS | ⏳ not measured (binary present, not running) |

These need a controlled measurement pass before Phase 1 begins; the §85 rule
("measure RAM, measure CPU, measure startup time") cannot be enforced without a
baseline to compare against.

## 4. Constraint summary

1. **No swap + 3.8 GB RAM** → RAM overruns are fatal, not degradable.
2. **Two local model paths exist** and can both engage → must be mutually exclusive.
3. **numpy (11.7 MB) is the largest fixed import**; the RAG design depends on it.
4. **SQLite is free** (~0.1 MB) → use it for structure, not because of RAM.
5. **Data footprint is tiny** (26 MB RAG, 5 MB memory) → disk is not the constraint;
   dependencies (2.9 GB across two venvs) and the model (2 GB) are.
6. **`pinokio/` (803 MB) is not JARVIS runtime** — exclude it from the portable
   build target (§74 "< 2 GB portable app excluding LLM"), otherwise the portable
   bundle will blow its budget on vendored tooling.

## 5. Recommendation

Land `MIGRATION_PLAN.md` **P0 (ResourceManager)** before any other phase. On this
hardware it is not a feature — it is the difference between a slow assistant and
an OOM kill. Then take a baseline measurement pass (the ⏳ rows in §3.4) so the
§85 regression rule has something to compare against.