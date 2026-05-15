"""
Live watcher test — requires a real Gemini API key in .env
Tests the full loop: file change -> Gemma extraction -> vault patch applied
Run: python test_watcher_live.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from server.config import load_config
from server.watcher import start_watcher, stop_watcher, watcher_status

cfg = load_config()

WATCH_DIR = Path(__file__).parent / "vault" / "_watcher_test_project"
WATCH_DIR.mkdir(parents=True, exist_ok=True)

TEST_FILE = WATCH_DIR / "main.py"
TEST_FILE.write_text(
    'def add(a, b):\n    """Add two numbers."""\n    return a + b\n\ndef greet(name):\n    return f"Hello {name}"\n',
    encoding="utf-8",
)

print("Watcher live test")
print("-" * 40)
print(f"Watch dir : {WATCH_DIR}")
print(f"Vault     : {cfg.vault_path}")
print(f"API key   : {cfg.gemini_api_key[:8]}...{cfg.gemini_api_key[-4:]}")
print()

# Start
result = start_watcher(cfg, str(WATCH_DIR))
print(f"[start] {result}")
assert result["status"] == "started"

# Modify the file to trigger the watcher (it takes initial snapshot, so we need a change)
time.sleep(3)
TEST_FILE.write_text(
    'def add(a, b):\n    """Add two numbers and return result."""\n    return a + b\n\ndef greet(name):\n    return f"Hello, {name}!"\n',
    encoding="utf-8",
)
print(f"[write] modified {TEST_FILE.name}")

# Wait for debounce (4s) + processing time
print("[wait] waiting up to 90s for Gemma to process...")
deadline = time.time() + 90
last_processed = 0
while time.time() < deadline:
    time.sleep(5)
    s = watcher_status()
    processed = s.get("processed", 0)
    errors = s.get("errors", 0)
    queued = s.get("queued_files", [])
    last_file = s.get("last_file", "")
    last_error = s.get("last_error", "")
    print(f"  processed={processed} errors={errors} queued={queued} last_file={last_file}")
    if last_error:
        print(f"  last_error={last_error}")
    if processed > last_processed:
        print(f"[done] file processed by Gemma!")
        break
else:
    print("[timeout] Gemma did not process within 90s")

# Stop
result = stop_watcher()
print(f"\n[stop] {result}")

# Check vault for the node
project_dir = cfg.vault_path / "projects" / "watcher-test-project"
print(f"\n[vault] checking {project_dir}")
if project_dir.exists():
    for f in sorted(project_dir.rglob("*.md")):
        rel = f.relative_to(cfg.vault_path)
        print(f"  {rel}")
    print("Vault nodes created - OK")
else:
    print("  No vault nodes found (check last_error above)")

# Cleanup test files
import shutil

shutil.rmtree(WATCH_DIR, ignore_errors=True)
if project_dir.exists():
    shutil.rmtree(project_dir, ignore_errors=True)
print("\n[cleanup] test files removed")
