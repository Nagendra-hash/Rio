# 🔑 JARVIS on a Pendrive

Run JARVIS from a USB stick: plug it into any PC, launch, unplug. Your config,
memories and conversation journal ride **on the drive** — nothing is written to
the host machine, and nothing is left behind when you pull it out.

---

## How it works

JARVIS decides where to keep its writable files by checking one environment
variable, `JARVIS_HOME`. When it is set, **everything** — `config/api_keys.json`
(your Gemini key), `memory/long_term.json`, the conversation journal, the
knowledge base, downloaded skills and the local GGUF model — resolves under
that folder instead of the repo. The launcher scripts set it to a
`jarvis-home/` folder on the drive, so the whole assistant becomes a folder you
can carry.

Every path in the app (main.py, ui.py, config_manager, memory_manager, the
journal, dashboard server, skill registry, local LLM, all actions) honours the
same variable — there is no second setting to forget.

> Without `JARVIS_HOME` the app behaves exactly as before. The variable only
> exists so a launcher can relocate it.

---

## Quick start (the easy way)

**You need:** a USB 3 stick, 16 GB+ recommended (the Python dependencies alone
are 2–4 GB), and a PC with Python 3.11+ and internet — for the first setup
only.

### Windows drive

1. Copy this whole repo folder onto the stick (e.g. `E:\JARVIS\`).
2. On the PC: `cd E:\JARVIS` then double-click **`portable.bat`**.
   - First run downloads the embedded Python (~11 MB) plus `get-pip.py`, then
     installs the dependencies — **one time, needs internet**, takes a while.
   - Every run after that: double-click, JARVIS starts, fully offline.
3. On first launch enter your Gemini API key as usual — it is saved to
   `jarvis-home\config\api_keys.json` **on the stick**, not on the PC.

### Linux / macOS drive

1. Copy the repo folder onto the stick.
2. On the PC: `cd /media/you/Stick/JARVIS && ./portable.sh`
   - First run builds a venv on the stick using the host's Python 3.11+
     (internet, one time). Works on macOS too (bash 3.2 — no GNU tools needed).
   - After that: `./portable.sh`, fully offline. Host machine has no Python?
     Skip setup entirely with the prebuilt bundle below — it needs none.
3. Same as above — the key lands in `jarvis-home/config/` on the stick.

---

## Prebuilt runtime (no internet needed on any target PC)

If the target machines may never have internet, build the entire runtime once
on your own PC and copy it over:

```bash
# Linux/macOS bundle (run on any Linux or Mac PC — targets need NO Python):
python3 scripts/build_portable.py --linux

# Windows bundle (run on a Windows PC with Python 3.12):
python scripts/build_portable.py --windows

# Windows, fully offline forever — also fetch every .whl so the drive can
# reinstall its dependencies without a network:
python scripts/build_portable.py --windows --offline-wheels
```

Then merge the build output onto the stick — everything inside
`jarvis-portable-build/` goes into the repo copy's `jarvis-portable/` folder:

| What | Where on the stick |
|---|---|
| the whole repo folder | `E:\JARVIS\` |
| `jarvis-portable-build/python/` | `E:\JARVIS\jarvis-portable\python\` |
| `jarvis-portable-build/site-packages/` | `E:\JARVIS\jarvis-portable\site-packages\` |
| `jarvis-portable-build/.deps-ok` | `E:\JARVIS\jarvis-portable\.deps-ok` |
| `get-pip.py`, `wheels/` (Windows builds) | same names under `jarvis-portable\` |

After that, `portable.bat` / `portable.sh` find the runtime and start
immediately — no download, no install, no admin rights, nothing written to the
host outside `/tmp`.

> The Linux/macOS runtime is **relocatable** (python-build-standalone): the
> same folder runs on any machine of the same architecture, Python installed
> or not. (`--linux-venv` builds a plain venv instead — that one is pinned to
> the build machine's Python and only works there.)

---

## What travels on the stick

```
E:\JARVIS\
├── main.py, ui.py, core/, actions/, memory/, …   ← the app (read-only)
├── portable.bat / portable.sh                    ← the launcher
├── jarvis-portable\
│   ├── python\            ← the runtime (embedded Windows / relocatable Linux)
│   ├── site-packages\     ← dependencies (both platforms)
│   ├── get-pip.py         ← offline pip bootstrap (Windows)
│   └── .deps-ok           ← marker: the dependency install completed
└── jarvis-home\           ← EVERYTHING WRITABLE lives here
    ├── config\api_keys.json     your Gemini key — treat the stick like a password
    ├── memory\long_term.json    what JARVIS remembers about you
    ├── memory\conversations\    the full conversation journal
    ├── memory\llm_usage.jsonl   the LLM router's usage ledger
    ├── hf-cache\                Whisper/Kokoro/wake-word models (HuggingFace cache)
    ├── knowledge\               the document library
    └── skills\                  skills installed from GitHub
```

Pull the stick out and the host PC has no trace — no config, no memories, no
key. (The OS itself may cache file reads; "safe eject" first.)

---

## Notes & limits

- **Speed:** USB sticks are slow. First launch after plugging in takes longer
  than an SSD install; a USB 3 stick on a USB 3 port cuts it a lot.
- **Filesystem:** format the stick **exFAT or NTFS**. The old FAT32 has a 4 GB
  per-file limit, and some wheels/models come close. exFAT is the safe default
  (readable on Windows, macOS and modern Linux).
- **Antivirus:** some Windows machines scan every `.py`/`.exe` on first mount,
  which can delay the first launch by a minute or two. Subsequent launches are
  fast.
- **Audio devices** are chosen per host machine (the picker stores devices by
  name and falls back to the system default when a machine lacks the saved
  one), so moving between PCs re-picks mic/speakers — as it should.
- **The API key is plaintext on the stick.** Anyone holding the stick holds
  the key (same trade-off as `config/api_keys.json` on a PC — see the main
  README's "Your Data"). If the stick is lost, revoke the key at
  aistudio.google.com. Several keys can be listed under `gemini_api_keys`;
  JARVIS pins one per session and hops to the next only on a 429 or a
  rejected key, so one exhausted project no longer takes the voice chat down.
- **Wake word / GGUF / Whisper / Kokoro models** download into `jarvis-home\`
  too (`hf-cache\` and `models\`), so they also travel — at several hundred MB
  apiece, budget stick space accordingly. Set `HF_HOME` yourself to override
  the `jarvis-home\hf-cache` default.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `portable.bat` flashes and closes | Run it from a `cmd` window in the stick's folder to see the error; `set JARVIS_KEEP_WINDOW=1` keeps it open. |
| First-run download fails | Get `python-3.12.8-embed-amd64.zip` from python.org manually, drop it in `jarvis-portable\` as `python-embed.zip`, re-run. |
| Linux: `no python3 on this machine` | The host has no Python — the relocatable runtime needs none: build the bundle on any Linux/Mac PC (`scripts/build_portable.py --linux`) and copy `jarvis-portable/` onto the stick. |
| Linux: host Python is 3.10 | venv setup mode needs 3.11+. Build the relocatable bundle on a newer machine instead — target machines need no Python. |
| Setup interrupted (stick pulled mid-install) | Next launch sees the missing `jarvis-portable\.deps-ok` marker and finishes the install automatically (internet once more). |
| JARVIS starts but has "forgotten" everything | You launched `main.py` directly instead of the launcher — `JARVIS_HOME` wasn't set, so it used the host's repo folder. Always start via `portable.bat` / `portable.sh`. |
