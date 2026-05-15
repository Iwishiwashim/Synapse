"""
Regression tests for core Synapse logic — no network, no LLM, no vault on disk.
Run with: pytest tests/
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# _parse_patch
# ---------------------------------------------------------------------------


def test_parse_patch_valid_json():
    from server.scanner import _parse_patch

    raw = '{"key": "projects.myapp.index", "content": "Main entry point.", "type": "code", "scope": "global", "weight": 0.8}'
    result = _parse_patch(raw)
    assert result is not None
    assert result["key"] == "projects.myapp.index"
    assert result["content"] == "Main entry point."


def test_parse_patch_strips_markdown_fences():
    from server.scanner import _parse_patch

    raw = '```json\n{"key": "a.b", "content": "Some content."}\n```'
    result = _parse_patch(raw)
    assert result is not None
    assert result["key"] == "a.b"


def test_parse_patch_extracts_embedded_object():
    from server.scanner import _parse_patch

    raw = 'Here is the result:\n{"key": "x.y", "content": "Desc."}\nDone.'
    result = _parse_patch(raw)
    assert result is not None
    assert result["key"] == "x.y"


def test_parse_patch_returns_none_for_garbage():
    from server.scanner import _parse_patch

    assert _parse_patch("not json at all") is None


def test_parse_patch_returns_none_missing_key():
    from server.scanner import _parse_patch

    assert _parse_patch('{"content": "No key field."}') is None


def test_parse_patch_returns_none_missing_content():
    from server.scanner import _parse_patch

    assert _parse_patch('{"key": "a.b"}') is None


# ---------------------------------------------------------------------------
# _is_vague
# ---------------------------------------------------------------------------


def test_is_vague_flags_short_generic():
    from server.scanner import _is_vague

    assert _is_vague("Handles the bridge process.", "def initBridge()") is True


def test_is_vague_flags_paraphrase_of_name():
    from server.scanner import _is_vague

    assert _is_vague("Starts watching the files.", "def startWatcher()") is True


def test_is_vague_passes_with_port_number():
    from server.scanner import _is_vague

    assert _is_vague("Listens on port 5056 for incoming requests.", "def startServer()") is False


def test_is_vague_passes_with_camelcase():
    from server.scanner import _is_vague

    assert _is_vague("Calls findFreePort then spawns bridgeProcess.", "def startBridge()") is False


def test_is_vague_passes_with_constant():
    from server.scanner import _is_vague

    assert (
        _is_vague("Reads MAX_FILE_BYTES from env and truncates input.", "def readFile()") is False
    )


def test_is_vague_passes_with_path():
    from server.scanner import _is_vague

    assert _is_vague("Writes output to ./vault/_graph.json.", "def saveGraph()") is False


def test_is_vague_passes_with_backtick():
    from server.scanner import _is_vague

    assert _is_vague("Returns `{'status': 'ok'}` after flushing queue.", "def flush()") is False


# ---------------------------------------------------------------------------
# _inject_wikilinks
# ---------------------------------------------------------------------------


def test_inject_wikilinks_appends_related():
    from server.diff import _inject_wikilinks

    result = _inject_wikilinks("Body text.", ["work.stack", "work.tools"])
    assert "Related:" in result
    assert "[[work/stack]]" in result
    assert "[[work/tools]]" in result


def test_inject_wikilinks_replaces_existing_related_line():
    from server.diff import _inject_wikilinks

    content = "Body\n\nRelated: [[old/link]]"
    result = _inject_wikilinks(content, ["new.link"])
    assert "old" not in result
    assert "[[new/link]]" in result


def test_inject_wikilinks_empty_list_removes_related():
    from server.diff import _inject_wikilinks

    content = "Body\n\nRelated: [[some/link]]"
    result = _inject_wikilinks(content, [])
    assert "Related:" not in result


def test_inject_wikilinks_uses_pipe_separator():
    from server.diff import _inject_wikilinks

    result = _inject_wikilinks("X", ["a.b", "c.d"])
    assert " | " in result


# ---------------------------------------------------------------------------
# load_pending resilience
# ---------------------------------------------------------------------------


def _make_config(vault: Path):
    from server.config import SynapseConfig

    return SynapseConfig(root_path=vault.parent, vault_path=vault)


def test_load_pending_missing_file_returns_empty():
    from server.diff import load_pending

    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir) / "vault"
        vault.mkdir()
        assert load_pending(_make_config(vault)) == []


def test_load_pending_empty_file_returns_empty():
    from server.diff import load_pending

    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir) / "vault"
        vault.mkdir()
        (vault / "_pending.json").write_text("", encoding="utf-8")
        assert load_pending(_make_config(vault)) == []


def test_load_pending_newline_only_returns_empty():
    from server.diff import load_pending

    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir) / "vault"
        vault.mkdir()
        (vault / "_pending.json").write_text("\n", encoding="utf-8")
        assert load_pending(_make_config(vault)) == []


def test_load_pending_corrupt_json_returns_empty():
    from server.diff import load_pending

    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir) / "vault"
        vault.mkdir()
        (vault / "_pending.json").write_text("{corrupted", encoding="utf-8")
        assert load_pending(_make_config(vault)) == []


def test_load_pending_valid_queue():
    from server.diff import load_pending

    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir) / "vault"
        vault.mkdir()
        queue = [{"patch_id": "abc123", "key": "work.stack", "status": "pending"}]
        (vault / "_pending.json").write_text(json.dumps(queue), encoding="utf-8")
        result = load_pending(_make_config(vault))
        assert len(result) == 1
        assert result[0]["patch_id"] == "abc123"


# ---------------------------------------------------------------------------
# cleanup_stale_nodes
# ---------------------------------------------------------------------------


def _write_md(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nkey: {key}\n---\n\ncontent\n", encoding="utf-8")


def test_cleanup_removes_stale_function_node():
    from server.diff import cleanup_stale_nodes

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        vault = Path(tmpdir) / "vault"
        fn_dir = vault / "projects" / "myapp" / "server"
        stale = fn_dir / "old-fn.md"
        _write_md(stale, "projects.myapp.server.old-fn")
        cfg = _make_config(vault)

        graph = {"nodes": [{"id": "server", "type": "file"}], "edges": []}
        result = cleanup_stale_nodes(cfg, "myapp", graph)

        assert result["count"] == 1
        assert not stale.exists()


def test_cleanup_keeps_current_function_node():
    from server.diff import cleanup_stale_nodes

    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir) / "vault"
        fn_dir = vault / "projects" / "myapp" / "server"
        keep = fn_dir / "get-data.md"
        _write_md(keep, "projects.myapp.server.get-data")
        cfg = _make_config(vault)

        graph = {
            "nodes": [
                {"id": "server", "type": "file"},
                {"id": "server-get-data", "type": "function", "parent": "server"},
            ],
            "edges": [],
        }
        result = cleanup_stale_nodes(cfg, "myapp", graph)

        assert result["count"] == 0
        assert keep.exists()


def test_cleanup_ignores_unscanned_file_dirs():
    from server.diff import cleanup_stale_nodes

    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir) / "vault"
        # 'utils' was NOT scanned — cleanup must leave it alone
        fn_dir = vault / "projects" / "myapp" / "utils"
        untouched = fn_dir / "helper.md"
        _write_md(untouched, "projects.myapp.utils.helper")
        cfg = _make_config(vault)

        graph = {"nodes": [{"id": "server", "type": "file"}], "edges": []}
        result = cleanup_stale_nodes(cfg, "myapp", graph)

        assert result["count"] == 0
        assert untouched.exists()


def test_cleanup_removes_empty_dir_after_last_node():
    from server.diff import cleanup_stale_nodes

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        vault = Path(tmpdir) / "vault"
        fn_dir = vault / "projects" / "myapp" / "server"
        only_fn = fn_dir / "gone.md"
        _write_md(only_fn, "projects.myapp.server.gone")
        cfg = _make_config(vault)

        graph = {"nodes": [{"id": "server", "type": "file"}], "edges": []}
        cleanup_stale_nodes(cfg, "myapp", graph)

        assert not fn_dir.exists()


# ---------------------------------------------------------------------------
# memory_save_chat
# ---------------------------------------------------------------------------


def test_save_chat_creates_file():
    from server.ai_importer import save_chat_memory

    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir) / "vault"
        cfg = _make_config(vault)
        result = save_chat_memory(
            cfg,
            title="Test Session",
            summary="We tested the save_chat pipeline end to end.",
            key_facts=["Vault creates chats/ dir", "file uses frontmatter"],
            decisions=["Use fixed chat_id in tests"],
            tags=["testing", "synapse"],
            chat_id="test-fixed-id",
        )
        chat_file = vault / "chats" / "test-fixed-id.md"
        assert chat_file.exists(), "chat file not written to vault/chats/"
        text = chat_file.read_text(encoding="utf-8")
        assert "key: chats.test-fixed-id" in text
        assert "Test Session" in text
        assert result.get("key") == "chats.test-fixed-id"


def test_save_chat_autogenerates_id():
    from server.ai_importer import save_chat_memory

    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir) / "vault"
        cfg = _make_config(vault)
        result = save_chat_memory(
            cfg,
            title="Auto ID Test",
            summary="No chat_id passed.",
            key_facts=["id is auto-generated"],
            decisions=[],
            tags=["test"],
        )
        cid = result.get("key", "").replace("chats.", "")
        assert cid and len(cid) > 4
        assert (vault / "chats" / f"{cid}.md").exists()


def test_save_chat_includes_key_facts():
    from server.ai_importer import save_chat_memory

    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir) / "vault"
        cfg = _make_config(vault)
        save_chat_memory(
            cfg,
            title="Facts Test",
            summary="Checking key facts appear in file body.",
            key_facts=["fact one", "fact two"],
            decisions=["decision alpha"],
            tags=["test"],
            chat_id="facts-test",
        )
        text = (vault / "chats" / "facts-test.md").read_text(encoding="utf-8")
        assert "fact one" in text
        assert "decision alpha" in text


# ---------------------------------------------------------------------------
# memory_code_search — no-index error path (no LLM, no vault writes needed)
# ---------------------------------------------------------------------------


def test_code_search_returns_error_without_index():
    from server.functions import memory_code_search

    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir) / "vault"
        vault.mkdir()
        cfg = _make_config(vault)
        result = memory_code_search(cfg, "some query")
        assert len(result) == 1
        assert "error" in result[0]
        assert "memory_scan_project" in result[0]["error"]


# ---------------------------------------------------------------------------
# memory_deep_search — no-graph error path (no LLM, no vault writes needed)
# ---------------------------------------------------------------------------


def test_deep_search_returns_error_without_graph():
    from server.functions import memory_deep_search

    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir) / "vault"
        (vault / "metadata").mkdir(parents=True)
        cfg = _make_config(vault)
        result = memory_deep_search(cfg, "some query")
        assert len(result) == 1
        assert "error" in result[0]
        assert "topic_graph.json" in result[0]["error"]


def test_deep_search_returns_results_with_graph():
    from server.functions import memory_deep_search
    import json as _json

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        vault = Path(tmpdir) / "vault"
        meta = vault / "metadata"
        meta.mkdir(parents=True)
        chats = vault / "chats"
        chats.mkdir()
        # Minimal graph
        graph = {"nodes": [{"id": "n1"}], "edges": []}
        (meta / "topic_graph.json").write_text(_json.dumps(graph), encoding="utf-8")
        # No FTS index → returns empty list, not an error dict
        cfg = _make_config(vault)
        result = memory_deep_search(cfg, "missing query")
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# memory_auto — escalation logic
# ---------------------------------------------------------------------------


def test_auto_escalates_when_no_vault_hits():
    """Empty vault → no search results → deep_results key present (graph error is acceptable)."""
    from server.functions import memory_auto

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        vault = Path(tmpdir) / "vault"
        vault.mkdir()
        (vault / "metadata").mkdir()
        cfg = _make_config(vault)
        result = memory_auto(cfg, "some obscure query")
        assert "vault_results" in result
        assert "deep_results" in result


def test_auto_no_escalate_when_confident(monkeypatch):
    """When vault search returns a high-confidence hit, deep search must NOT be called."""
    from server import functions as fn

    confident_hit = [{"key": "work.stack", "score": 0.9, "reason": "direct match"}]
    monkeypatch.setattr(fn, "memory_search_tool", lambda *_: confident_hit)
    monkeypatch.setattr(fn, "memory_context", lambda *_: {})
    monkeypatch.setattr(
        fn,
        "_deep_search",
        lambda *_: (_ for _ in ()).throw(AssertionError("deep_search must not be called")),
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir) / "vault"
        vault.mkdir()
        cfg = _make_config(vault)
        result = fn.memory_auto(cfg, "what is my stack")

    assert "deep_results" not in result
    assert result["vault_results"] == confident_hit


def test_auto_escalates_when_score_below_threshold(monkeypatch):
    """Vault returns results but all scores are weak → deep search fires."""
    from server import functions as fn

    weak_hits = [{"key": "work.stack", "score": 0.4, "reason": "weak"}]
    deep_called = []

    def fake_deep(*_):
        deep_called.append(True)
        return []

    monkeypatch.setattr(fn, "memory_search_tool", lambda *_: weak_hits)
    monkeypatch.setattr(fn, "memory_context", lambda *_: {})
    monkeypatch.setattr(fn, "_deep_search", fake_deep)

    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir) / "vault"
        vault.mkdir()
        cfg = _make_config(vault)
        result = fn.memory_auto(cfg, "fuzzy query")

    assert deep_called, "deep_search should have been called"
    assert "deep_results" in result


# ---------------------------------------------------------------------------
# memory_commit — write_mode behaviour
# ---------------------------------------------------------------------------


def _make_config_with_mode(vault: Path, mode: str):
    from server.config import SynapseConfig

    return SynapseConfig(root_path=vault.parent, vault_path=vault, write_mode=mode)


def test_commit_review_mode_leaves_patch_pending():
    """In review mode, memory_commit proposes but does not apply — patch_id in response, file not written."""
    from server.functions import memory_commit

    with tempfile.TemporaryDirectory() as tmpdir:
        vault = Path(tmpdir) / "vault"
        vault.mkdir()
        cfg = _make_config_with_mode(vault, "review")
        result = memory_commit(
            cfg,
            {
                "key": "work.testkey",
                "content": "test content",
                "type": "note",
                "scope": "global",
                "weight": 0.5,
                "reason": "unit test",
            },
        )
        assert "patch_id" in result
        assert "diff" in result
        # File must NOT exist yet — only proposed, not applied
        assert not (vault / "work" / "testkey.md").exists()


def test_commit_auto_mode_writes_file():
    """In auto mode, memory_commit applies immediately — file must exist after the call."""
    from server.functions import memory_commit

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        vault = Path(tmpdir) / "vault"
        vault.mkdir()
        cfg = _make_config_with_mode(vault, "auto")
        result = memory_commit(
            cfg,
            {
                "key": "work.autokey",
                "content": "auto written content",
                "type": "note",
                "scope": "global",
                "weight": 0.5,
                "reason": "unit test",
            },
        )
        assert result.get("status") == "applied"
        assert (vault / "work" / "autokey.md").exists()
