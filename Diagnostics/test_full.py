"""
Full end-to-end Synapse test — requires GEMINI_API_KEY in .env
Covers every MCP tool. Run: python test_full.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from server.config import load_config
from server.functions import (
    memory_apply_update,
    memory_conflicts,
    memory_diff,
    memory_get,
    memory_organize_vault,
    memory_propose_update,
    memory_relink_all,
    memory_scan_project,
    memory_search_tool,
    memory_start_watcher,
    memory_stop_watcher,
    memory_tree,
    memory_watcher_status,
    rebuild_index,
)
from server.weekly_report import generate_weekly_report

PASS = "[PASS]"
FAIL = "[FAIL]"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    tag = PASS if ok else FAIL
    print(f"  {tag} {name}" + (f" — {detail}" if detail else ""))
    results.append((name, ok, detail))


def section(title: str) -> None:
    print(f"\n{'='*55}")
    print(f"  {title}")
    print("=" * 55)


cfg = load_config()
print("Synapse full end-to-end test")
print(f"Vault : {cfg.vault_path}")
print(f"Key   : {cfg.gemini_api_key[:8]}...{cfg.gemini_api_key[-4:] if cfg.gemini_api_key else 'MISSING'}")

if not cfg.gemini_api_key:
    print("\nNo API key — run python setup.py first.")
    sys.exit(1)


# ── 1. memory.tree ──────────────────────────────────────────
section("1. memory.tree")
tree = memory_tree(cfg)
check("returns dict with type=folder", tree.get("type") == "folder")
check("has children list", isinstance(tree.get("children"), list))
print(f"     top-level folders: {[c['name'] for c in tree['children'] if c['type']=='folder']}")


# ── 2. memory.propose_update ────────────────────────────────
section("2. memory.propose_update")
patch = {
    "key": "test.e2e-run",
    "content": "Full end-to-end test run for Synapse v1.",
    "type": "note",
    "scope": "global",
    "weight": 0.5,
    "signal": "high_signal",
    "reason": "Automated test entry.",
}
proposed = memory_propose_update(cfg, patch)
patch_id = proposed.get("patch_id", "")
check("patch_id returned", bool(patch_id), patch_id)
check("diff present", bool(proposed.get("diff")))


# ── 3. memory.diff ──────────────────────────────────────────
section("3. memory.diff")
pending = memory_diff(cfg)
check("pending list non-empty", len(pending) > 0)
check("our patch in queue", any(p["patch_id"] == patch_id for p in pending))


# ── 4. memory.apply_update ──────────────────────────────────
section("4. memory.apply_update")
applied = memory_apply_update(cfg, patch_id)
check("status=applied", applied.get("status") == "applied")
check("key matches", applied.get("key") == "test.e2e-run")


# ── 5. memory.get ───────────────────────────────────────────
section("5. memory.get")
got = memory_get(cfg, "test.e2e-run")
check("key correct", got.get("key") == "test.e2e-run")
check("content present", "end-to-end" in got.get("content", ""))


# ── 6. memory.search ────────────────────────────────────────
section("6. memory.search")
results_search = memory_search_tool(cfg, "end-to-end test synapse")
check("returns list", isinstance(results_search, list))
check("finds our entry", any("e2e" in r.get("key", "") for r in results_search))


# ── 7. memory.conflicts ─────────────────────────────────────
section("7. memory.conflicts")
conflicts = memory_conflicts(cfg)
check("returns list", isinstance(conflicts, list))
print(f"     {len(conflicts)} conflict(s) detected")


# ── 8. memory.rebuild_index ─────────────────────────────────
section("8. memory.rebuild_index")
rebuilt = rebuild_index(cfg)
check("status=rebuilt", rebuilt.get("status") == "rebuilt")


# ── 9. memory.relink_all ────────────────────────────────────
section("9. memory.relink_all")
relinked = memory_relink_all(cfg)
check("status=relinked", relinked.get("status") == "relinked")
check("files count > 0", len(relinked.get("files", [])) > 0)
print(f"     {len(relinked['files'])} file(s) relinked")


# ── 10. memory.organize ─────────────────────────────────────
section("10. memory.organize")
organized = memory_organize_vault(cfg)
check("status=organized", organized.get("status") == "organized")
created = organized.get("indexes_created", [])
updated = organized.get("indexes_updated", [])
print(f"     indexes created={len(created)} updated={len(updated)} relinked={organized.get('files_relinked',0)}")
check("created or updated indexes", len(created) + len(updated) > 0)

# verify index files exist on disk
for key in (created + updated)[:3]:
    parts = key.replace(".index", "").split(".")
    idx_path = cfg.vault_path.joinpath(*parts) / "index.md"
    check(f"index.md exists: {key}", idx_path.exists())


# ── 11. memory.weekly_report ────────────────────────────────
section("11. memory.weekly_report")
report = generate_weekly_report(cfg)
check("status=generated", report.get("status") == "generated")
weekly_path = Path(report.get("file_path", ""))
check("_weekly.md written", weekly_path.exists())


# ── 12. memory.scan_project (small synthetic project) ───────
section("12. memory.scan_project")
with tempfile.TemporaryDirectory() as tmpdir:
    proj = Path(tmpdir) / "miniapp"
    proj.mkdir()
    (proj / "main.py").write_text(
        'def run(port=8080):\n    """Start HTTP server on given port."""\n    pass\n',
        encoding="utf-8",
    )
    (proj / "utils.py").write_text(
        'MAX_RETRIES = 3\n\ndef retry(fn):\n    """Retry fn up to MAX_RETRIES times."""\n    pass\n',
        encoding="utf-8",
    )

    scan = memory_scan_project(cfg, str(proj))
    check("no error key", "error" not in scan)
    check("project_name present", bool(scan.get("project_name")))
    check("files_analyzed > 0", scan.get("files_analyzed", 0) > 0)
    patches_proposed = scan.get("patches_proposed", 0)
    fn_nodes = scan.get("function_nodes", 0)
    print(f"     files={scan.get('files_analyzed')} patches={patches_proposed} fn_nodes={fn_nodes} stale_removed={scan.get('stale_removed',0)}")
    check("patches proposed", patches_proposed > 0)

    # apply all scan patches
    pending_after = memory_diff(cfg)
    scan_patches = [p for p in pending_after if p["key"].startswith("projects.miniapp")]
    applied_count = 0
    for p in scan_patches:
        try:
            memory_apply_update(cfg, p["patch_id"])
            applied_count += 1
        except Exception:
            pass
    check(f"applied {applied_count} scan patches", applied_count > 0)

    # cleanup scan vault nodes
    proj_dir = cfg.vault_path / "projects" / "miniapp"
    if proj_dir.exists():
        shutil.rmtree(proj_dir)
    print(f"     scan vault nodes cleaned up")


# ── 13. memory.start_watcher / stop_watcher ─────────────────
section("13. memory.watcher (file detection + Gemma processing)")
with tempfile.TemporaryDirectory() as tmpdir:
    watch_dir = Path(tmpdir) / "watchtest"
    watch_dir.mkdir()
    (watch_dir / "app.py").write_text(
        'def handler(request):\n    """Handle HTTP request and return JSON response."""\n    return {"ok": True}\n',
        encoding="utf-8",
    )

    started = memory_start_watcher(cfg, str(watch_dir))
    check("watcher started", started.get("status") == "started")

    time.sleep(3)
    (watch_dir / "app.py").write_text(
        'MAX_SIZE = 1024\n\ndef handler(request):\n    """Handle HTTP request, enforce MAX_SIZE limit, return JSON."""\n    return {"ok": True}\n',
        encoding="utf-8",
    )
    print("     modified app.py — waiting up to 90s for Gemma...")

    deadline = time.time() + 90
    processed = 0
    while time.time() < deadline:
        time.sleep(5)
        s = memory_watcher_status()
        processed = s.get("processed", 0)
        err = s.get("last_error", "")
        print(f"     processed={processed} queued={s.get('queued_files',[])} last_error={err or 'none'}")
        if processed > 0:
            break

    check("watcher processed file", processed > 0)

    stopped = memory_stop_watcher()
    check("watcher stopped", stopped.get("status") == "stopped")

    # cleanup watcher vault nodes
    wt_dir = cfg.vault_path / "projects" / "watchtest"
    if wt_dir.exists():
        shutil.rmtree(wt_dir)


# ── 14. cleanup test memory ──────────────────────────────────
section("14. Cleanup test entries")
test_file = cfg.vault_path / "test" / "e2e-run.md"
if test_file.exists():
    test_file.unlink()
    if test_file.parent.exists() and not any(test_file.parent.iterdir()):
        test_file.parent.rmdir()
rebuild_index(cfg)
check("test memory cleaned up", not test_file.exists())


# ── Summary ──────────────────────────────────────────────────
section("Summary")
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
print(f"\n  {passed} passed  {failed} failed  ({len(results)} total)\n")

if failed:
    print("  Failures:")
    for name, ok, detail in results:
        if not ok:
            print(f"    {FAIL} {name}" + (f" — {detail}" if detail else ""))

sys.exit(0 if failed == 0 else 1)
