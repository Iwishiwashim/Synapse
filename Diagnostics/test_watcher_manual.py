"""
Manual watcher smoke test — no API key needed.
Tests: start, file detection, debounce, status, stop.
Run: python test_watcher_manual.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from server.config import SynapseConfig
from server.watcher import start_watcher, stop_watcher, watcher_status

ROOT = Path(__file__).parent
cfg = SynapseConfig(root_path=ROOT, vault_path=ROOT / "vault")


def section(title: str) -> None:
    print(f"\n{'='*50}")
    print(f"  {title}")
    print("=" * 50)


with tempfile.TemporaryDirectory() as tmpdir:
    watch_dir = Path(tmpdir) / "testproject"
    watch_dir.mkdir()
    (watch_dir / "main.py").write_text("def hello(): pass\n", encoding="utf-8")

    # --- 1. Start ---
    section("1. Start watcher")
    result = start_watcher(cfg, str(watch_dir))
    print(result)
    assert result["status"] == "started", f"Expected started, got: {result}"

    # --- 2. Already running ---
    section("2. Start again (should report already_running)")
    result2 = start_watcher(cfg, str(watch_dir))
    print(result2)
    assert result2["status"] == "already_running"

    # --- 3. Status while idle ---
    section("3. Status (idle)")
    status = watcher_status()
    print(status)
    assert status["status"] == "running"
    assert status["queued_files"] == []

    # --- 4. Create a new file — should appear in queue ---
    section("4. Write new file -> check queue")
    (watch_dir / "utils.py").write_text("def helper(): return 42\n", encoding="utf-8")
    time.sleep(3)  # let observer poll (2s interval)
    status = watcher_status()
    print(status)
    queued = status["queued_files"]
    assert "utils.py" in queued, f"utils.py not in queue: {queued}"
    print("  utils.py detected in queue - OK")

    # --- 5. Modify existing file ---
    section("5. Modify existing file -> check queue")
    (watch_dir / "main.py").write_text("def hello(): return 'hi'\n", encoding="utf-8")
    time.sleep(3)
    status = watcher_status()
    print(status)
    assert "main.py" in status["queued_files"], f"main.py not queued: {status['queued_files']}"
    print("  main.py detected in queue - OK")

    # --- 6. Non-watched extension ignored ---
    section("6. Non-watched extension (.txt) should not appear")
    (watch_dir / "notes.txt").write_text("just a note\n", encoding="utf-8")
    time.sleep(3)
    status = watcher_status()
    print(status)
    assert "notes.txt" not in status["queued_files"], "notes.txt should be ignored"
    print("  notes.txt correctly ignored - OK")

    # --- 7. Stop ---
    section("7. Stop watcher")
    result = stop_watcher()
    print(result)
    assert result["status"] == "stopped"

    # --- 8. Status after stop ---
    section("8. Status after stop")
    status = watcher_status()
    print(status)
    assert status["status"] == "not_running"

    # --- 9. Stop when not running ---
    section("9. Stop again (not running)")
    result = stop_watcher()
    print(result)
    assert result["status"] == "not_running"

print("\n\nAll watcher tests passed.")
