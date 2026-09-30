"""Weekly top-trader playbook refresh — runs re-research on known traders.

Keeps the dossiers current with new videos/articles. Quota-lean: per trader
it costs ~2 web searches + 1 Gemini synthesis (+ transcripts when captions
exist). Failures are per-trader and logged; the timer never alerts the user.
"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TRADERS = ["Takashi Kotegawa", "Gautam Jha"]

def main():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tr", Path(__file__).resolve().parent.parent / "actions" / "trader_research.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    for trader in TRADERS:
        try:
            out = m.trader_research({"trader": trader})
            print(f"[trader-refresh] {trader}: {out[:160]}", flush=True)
        except Exception as e:
            print(f"[trader-refresh] {trader} failed: {e}", flush=True)
        time.sleep(5)

if __name__ == "__main__":
    main()
