#!/usr/bin/env python3
"""Wait for Denver scrape job b4ab388e, download CSV, enrich socials, save final leads file."""
import csv, io, json, sys, time, urllib.request

sys.path.insert(0, "/home/nagendra/google-maps-scraper-kit/scripts")
from scrape import enrich_socials, LEAD

JOB = "b4ab388e-1f94-43b2-bb2b-7e2f774c7486"
BASE = "http://localhost:8080/api/v1/jobs"
OUT = "/home/nagendra/leads-denver-home-services.csv"


def get(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# 1. Poll until the job flips to ok/failed (hard cap 75 min)
for i in range(225):
    try:
        status = json.loads(get(f"{BASE}/{JOB}", timeout=10)).get("Status")
    except Exception as e:
        print("poll error:", e, flush=True)
        status = None
    print(f"status: {status} (attempt {i + 1})", flush=True)
    if status in ("ok", "failed"):
        break
    time.sleep(20)

if status != "ok":
    sys.exit(f"job ended with status={status!r}; not saving results")

# 2. Download raw CSV
raw = get(f"{BASE}/{JOB}/download")
rows = list(csv.DictReader(io.StringIO(raw)))
print(f"downloaded {len(rows)} rows", flush=True)

# 3. Keep lead fields, enrich with socials (0 LLM tokens, pure HTTP+regex)
results = [{k: r.get(k, "") for k in LEAD} for r in rows]
enrich_socials(results)
fields = LEAD + ["instagram", "facebook", "linkedin"]

# 4. Save final CSV
with open(OUT, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    w.writerows(results)

found = sum(1 for r in results if r.get("instagram") or r.get("facebook") or r.get("linkedin"))
print(f"saved -> {OUT} ({len(results)} leads; socials for {found})", flush=True)
