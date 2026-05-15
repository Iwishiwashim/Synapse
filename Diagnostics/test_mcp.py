"""
MCP server smoke test — talks to the server exactly like Claude Desktop does.
Spawns the server over stdio, runs JSON-RPC calls, prints results.

Usage:
    python test_mcp.py              # tests non-AI tools only
    python test_mcp.py --full       # also calls memory.search (needs API key)
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent
PYTHON = ROOT / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / "python"
if sys.platform == "win32":
    PYTHON = Path(str(PYTHON) + ".exe")

FULL = "--full" in sys.argv

PASS = "[PASS]"
FAIL = "[FAIL]"
results: list[tuple[str, bool]] = []


# ---------------------------------------------------------------------------
# Minimal MCP client over stdio
# ---------------------------------------------------------------------------


class MCPClient:
    def __init__(self) -> None:
        self._proc = subprocess.Popen(
            [str(PYTHON), "-m", "server.main"],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._id = 0
        self._lock = threading.Lock()
        self._stderr_lines: list[str] = []
        # Drain stderr in background so it doesn't block
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        for line in self._proc.stderr:
            self._stderr_lines.append(line.rstrip())

    def _send(self, msg: dict) -> None:
        line = json.dumps(msg) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

    def _recv(self, timeout: float = 15.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if line.strip():
                return json.loads(line)
            time.sleep(0.05)
        raise TimeoutError(f"No response within {timeout}s")

    def request(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        with self._lock:
            self._id += 1
            req_id = self._id
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params:
            msg["params"] = params
        self._send(msg)
        resp = self._recv(timeout)
        return resp

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        self._send(msg)

    def close(self) -> None:
        try:
            self._proc.stdin.close()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()

    @property
    def stderr(self) -> list[str]:
        return self._stderr_lines


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def check(name: str, ok: bool, detail: str = "") -> None:
    tag = PASS if ok else FAIL
    suffix = f" -- {detail}" if detail else ""
    print(f"  {tag} {name}{suffix}")
    results.append((name, ok))


def section(title: str) -> None:
    print(f"\n{'='*55}")
    print(f"  {title}")
    print("=" * 55)


def tool_call(client: MCPClient, tool: str, args: dict = {}, timeout: float = 15.0):
    resp = client.request("tools/call", {"name": tool, "arguments": args}, timeout=timeout)
    if "error" in resp:
        return None
    result = resp.get("result", {})
    if result.get("isError"):
        return None
    # FastMCP 1.27+: use structuredContent.result when present (handles lists correctly)
    structured = result.get("structuredContent", {})
    if "result" in structured:
        return structured["result"]
    # Fallback: parse content text blocks
    texts = [b["text"] for b in result.get("content", []) if b.get("type") == "text"]
    if not texts:
        return {}
    try:
        return json.loads(texts[0])
    except json.JSONDecodeError:
        return {"raw": texts[0]}


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

print("Synapse MCP server test")
print(f"Python : {PYTHON}")
print(f"Server : python -m server.main")
print(f"Mode   : {'full (includes AI tools)' if FULL else 'basic (no AI calls)'}")

client = MCPClient()
time.sleep(1.0)  # let the server start

try:
    # ── 1. Handshake ────────────────────────────────────────
    section("1. Handshake (initialize)")
    resp = client.request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "synapse-test", "version": "1.0"},
        },
    )
    ok = "result" in resp
    server_name = resp.get("result", {}).get("serverInfo", {}).get("name", "")
    check("initialize succeeds", ok, server_name)

    client.notify("notifications/initialized")

    # ── 2. Tool discovery ────────────────────────────────────
    section("2. Tool discovery (tools/list)")
    resp = client.request("tools/list")
    tools = [t["name"] for t in resp.get("result", {}).get("tools", [])]
    print(f"     {len(tools)} tool(s) registered:")
    for t in sorted(tools):
        print(f"     - {t}")

    expected = [
        "memory_tree",
        "memory_get",
        "memory_search",
        "memory_propose_update",
        "memory_apply_update",
        "memory_reject_update",
        "memory_diff",
        "memory_conflicts",
        "memory_rebuild_index",
        "memory_relink_all",
        "memory_organize",
        "memory_scan_project",
        "memory_weekly_report",
        "memory_start_watcher",
        "memory_stop_watcher",
        "memory_watcher_status",
    ]
    for name in expected:
        check(f"tool registered: {name}", name in tools)

    # ── 3. memory.tree ───────────────────────────────────────
    section("3. memory.tree")
    result = tool_call(client, "memory_tree")
    check("returns result", result is not None)
    check("type=folder", result.get("type") == "folder")
    check("has children", isinstance(result.get("children"), list))
    folders = [c["name"] for c in result.get("children", []) if c.get("type") == "folder"]
    print(f"     top-level folders: {folders}")

    # ── 4. memory.diff ───────────────────────────────────────
    section("4. memory.diff (pending queue)")
    result = tool_call(client, "memory_diff")
    check("returns list", isinstance(result, list))
    print(f"     {len(result)} pending patch(es)")

    # ── 5. memory.watcher_status ─────────────────────────────
    section("5. memory.watcher_status")
    result = tool_call(client, "memory_watcher_status")
    check("returns result", result is not None)
    check("status field present", "status" in result)
    print(f"     watcher status: {result.get('status')}")

    # ── 6. memory.conflicts ──────────────────────────────────
    section("6. memory.conflicts")
    result = tool_call(client, "memory_conflicts")
    check("returns list", isinstance(result, list))
    print(f"     {len(result)} conflict(s)")

    # ── 7. memory.propose_update + apply + get ───────────────
    section("7. Write pipeline (propose -> apply -> get)")
    proposed = tool_call(
        client,
        "memory_propose_update",
        {
            "patch": {
                "key": "test.mcp-probe",
                "content": "MCP server probe entry. Safe to delete.",
                "type": "note",
                "scope": "global",
                "weight": 0.1,
                "signal": "high_signal",
                "reason": "MCP server test",
            }
        },
    )
    patch_id = proposed.get("patch_id", "") if proposed else ""
    check("propose returns patch_id", bool(patch_id), patch_id)

    if patch_id:
        applied = tool_call(client, "memory_apply_update", {"patch_id": patch_id})
        check("apply succeeds", applied.get("status") == "applied" if applied else False)

        got = tool_call(client, "memory_get", {"key": "test.mcp-probe"})
        check("get returns content", "probe" in (got.get("content", "") if got else ""))

        # Cleanup — propose overwrite then reject it so the file gets removed at end
        client.request(
            "tools/call",
            {
                "name": "memory_propose_update",
                "arguments": {
                    "patch": {
                        "key": "test.mcp-probe",
                        "content": "Deleted by MCP test cleanup.",
                        "signal": "high_signal",
                        "reason": "cleanup",
                    }
                },
            },
        )

    # ── 8. memory.search (optional, needs key) ───────────────
    if FULL:
        section("8. memory.search (FTS)")
        result = tool_call(client, "memory.search", {"query": "test mcp probe"}, timeout=20)
        check("search returns list", isinstance(result, list))
        print(f"     {len(result)} result(s)")

    # ── 9. Error handling — bad key ──────────────────────────
    section("9. Error handling")
    bad = tool_call(client, "memory_get", {"key": "does.not.exist"})
    # Should either return None (error block) or a dict with an error key
    is_error = bad is None or "error" in (bad or {})
    check("bad key returns error", is_error)

    # ── 10. Stderr clean ─────────────────────────────────────
    section("10. Server stderr (INFO logs are normal)")
    time.sleep(0.5)
    # FastMCP logs INFO messages to stderr — that's expected.
    # Only flag actual errors/tracebacks.
    all_stderr = [l for l in client.stderr if l.strip()]
    bad_stderr = [
        l
        for l in all_stderr
        if any(marker in l for marker in ("ERROR", "Traceback", "Exception", "Error:"))
    ]
    if all_stderr:
        print(f"     {len(all_stderr)} log line(s) (showing errors only):")
        for l in bad_stderr:
            print(f"     | {l}")
        if not bad_stderr:
            print("     (no errors — all INFO)")
    else:
        print("     (empty)")
    check("no error-level stderr", len(bad_stderr) == 0)

finally:
    client.close()

    # Remove probe file if left behind
    probe = ROOT / "vault" / "test" / "mcp-probe.md"
    if probe.exists():
        probe.unlink()
        if probe.parent.exists() and not any(probe.parent.iterdir()):
            probe.parent.rmdir()

# ── Summary ─────────────────────────────────────────────────
section("Summary")
passed = sum(1 for _, ok in results if ok)
failed = sum(1 for _, ok in results if not ok)
print(f"\n  {passed} passed  {failed} failed  ({len(results)} total)\n")
if failed:
    print("  Failures:")
    for name, ok in results:
        if not ok:
            print(f"    {FAIL} {name}")

sys.exit(0 if failed == 0 else 1)
