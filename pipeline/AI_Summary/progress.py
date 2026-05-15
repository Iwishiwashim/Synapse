import time
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DIR = Path("synapse_ai_summaries")
ERRORS = DIR / "errors.jsonl"
TOTAL = 2007
REFRESH = 5

start = None
last_count = 0

while True:
    files = list(DIR.glob("*.json"))
    count = len(files)
    errors = sum(1 for _ in ERRORS.open(encoding="utf-8")) if ERRORS.exists() else 0

    if start is None and count > 0:
        oldest = min(files, key=lambda f: f.stat().st_mtime)
        start = oldest.stat().st_mtime

    pct = count / TOTAL
    filled = int(pct * 40)
    bar = "#" * filled + "-" * (40 - filled)

    if start:
        elapsed = time.time() - start
        rate = count / (elapsed / 60)
        remaining = (TOTAL - count) / rate / 60 if rate > 0 else 0
        eta = f"{remaining:.1f}h"
        rate_str = f"{rate:.1f}/min"
    else:
        eta = "?"
        rate_str = "?"

    print(
        f"\r[{bar}] {count}/{TOTAL} ({pct*100:.1f}%) | {rate_str} | ETA: {eta} | Errors: {errors}   ",
        end="",
        flush=True,
    )

    if count >= TOTAL:
        print("\nDone!")
        break

    time.sleep(REFRESH)
