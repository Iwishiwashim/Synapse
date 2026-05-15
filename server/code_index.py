"""
SQLite-backed code graph index with FTS5 + semantic search.
Separate from the memory vault index (_index.db).
Stored at vault/_code_index.db.
"""

from __future__ import annotations

import math
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_EMBED_DIM = 3072
_VEC_FMT = f"{_EMBED_DIM}f"

SCHEMA = """
CREATE TABLE IF NOT EXISTS code_nodes (
  node_key   TEXT PRIMARY KEY,
  project    TEXT NOT NULL,
  node_id    TEXT NOT NULL,
  file       TEXT NOT NULL,
  type       TEXT NOT NULL,
  label      TEXT NOT NULL,
  signature  TEXT DEFAULT '',
  lineno     INTEGER DEFAULT 0,
  source     TEXT DEFAULT '',
  description TEXT DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
  node_key,
  label,
  signature,
  description
);

CREATE TABLE IF NOT EXISTS code_edges (
  project   TEXT NOT NULL,
  source    TEXT NOT NULL,
  target    TEXT NOT NULL,
  relation  TEXT NOT NULL,
  PRIMARY KEY (project, source, target, relation)
);

CREATE TABLE IF NOT EXISTS code_embeddings (
  node_key   TEXT PRIMARY KEY,
  vector     BLOB NOT NULL,
  updated_at TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def _db_path(vault_path: Path) -> Path:
    return vault_path / "_code_index.db"


def connect(vault_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(vault_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------


def upsert_node(conn: sqlite3.Connection, node: dict[str, Any]) -> None:
    nk = node["node_key"]
    conn.execute(
        """
        INSERT INTO code_nodes(node_key, project, node_id, file, type, label, signature, lineno, source, description)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(node_key) DO UPDATE SET
          file=excluded.file, type=excluded.type, label=excluded.label,
          signature=excluded.signature, lineno=excluded.lineno,
          source=excluded.source, description=excluded.description
        """,
        (
            nk,
            node["project"],
            node["node_id"],
            node["file"],
            node["type"],
            node["label"],
            node.get("signature", ""),
            node.get("lineno", 0),
            node.get("source", ""),
            node.get("description", ""),
        ),
    )
    # Sync FTS
    conn.execute("DELETE FROM code_fts WHERE node_key = ?", (nk,))
    conn.execute(
        "INSERT INTO code_fts(node_key, label, signature, description) VALUES (?,?,?,?)",
        (nk, node["label"], node.get("signature", ""), node.get("description", "")),
    )


def upsert_edge(
    conn: sqlite3.Connection, project: str, source: str, target: str, relation: str
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO code_edges(project, source, target, relation) VALUES (?,?,?,?)",
        (project, source, target, relation),
    )


def delete_project(conn: sqlite3.Connection, project: str) -> None:
    keys = [
        r[0]
        for r in conn.execute(
            "SELECT node_key FROM code_nodes WHERE project=?", (project,)
        ).fetchall()
    ]
    for k in keys:
        conn.execute("DELETE FROM code_fts WHERE node_key=?", (k,))
    conn.execute("DELETE FROM code_nodes WHERE project=?", (project,))
    conn.execute("DELETE FROM code_edges WHERE project=?", (project,))
    conn.execute("DELETE FROM code_embeddings WHERE node_key LIKE ?", (f"{project}/%",))


# ---------------------------------------------------------------------------
# Bulk index
# ---------------------------------------------------------------------------


def index_project(
    vault_path: Path,
    api_key: str | None,
    project: str,
    graph: dict[str, Any],
    descriptions: dict[str, str],
) -> int:
    """
    Insert all graph nodes + edges into the code index.
    Re-embeds nodes whose description changed.
    Returns count of nodes indexed.
    """
    with connect(vault_path) as conn:
        delete_project(conn, project)

        for gnode in graph.get("nodes", []):
            nid = gnode["id"]
            nk = f"{project}/{nid}"
            desc = descriptions.get(nid, "")
            node = {
                "node_key": nk,
                "project": project,
                "node_id": nid,
                "file": gnode.get("file", ""),
                "type": gnode.get("type", "file"),
                "label": gnode.get("label", nid),
                "signature": gnode.get("signature", ""),
                "lineno": gnode.get("lineno", 0),
                "source": gnode.get("source", ""),
                "description": desc,
            }
            upsert_node(conn, node)

        for edge in graph.get("edges", []):
            upsert_edge(conn, project, edge["source"], edge["target"], edge["relation"])

        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM code_nodes WHERE project=?", (project,)
        ).fetchone()[0]

    # Embed descriptions (outside transaction to avoid long locks)
    if api_key:
        _embed_project(vault_path, api_key, project, graph, descriptions)

    return count


def _embed_project(
    vault_path: Path,
    api_key: str,
    project: str,
    graph: dict[str, Any],
    descriptions: dict[str, str],
) -> None:
    try:
        from google import genai
        from google.genai import types as gtypes
    except ImportError:
        return

    client = genai.Client(api_key=api_key)
    db = _db_path(vault_path)

    for gnode in graph.get("nodes", []):
        nid = gnode["id"]
        nk = f"{project}/{nid}"
        desc = descriptions.get(nid, "")
        sig = gnode.get("signature", "")
        label = gnode.get("label", nid)
        text = f"{label}\n{sig}\n{desc}".strip()[:4000]
        if not text:
            continue
        try:
            result = client.models.embed_content(
                model="gemini-embedding-001",
                contents=text,
                config=gtypes.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT"),
            )
            vec = list(result.embeddings[0].values)
            _upsert_embedding(db, nk, vec)
        except Exception:
            continue


def _upsert_embedding(db_path: Path, node_key: str, vector: list[float]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    blob = struct.pack(_VEC_FMT, *vector)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute(
            "INSERT INTO code_embeddings(node_key,vector,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(node_key) DO UPDATE SET vector=excluded.vector, updated_at=excluded.updated_at",
            (node_key, blob, now),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def search_code(
    vault_path: Path,
    api_key: str | None,
    query: str,
    project: str = "",
    limit: int = 8,
) -> list[dict[str, Any]]:
    """
    Hybrid FTS5 + semantic search over code nodes.
    Returns list of node dicts with score and call edges.
    """
    fts_results = _fts_search(vault_path, query, project, limit * 2)
    sem_results = (
        _semantic_search(vault_path, api_key, query, project, limit * 2) if api_key else []
    )
    merged = _hybrid_merge(fts_results, sem_results, limit)
    return _enrich(vault_path, merged, project)


def _fts_search(vault_path: Path, query: str, project: str, limit: int) -> list[dict[str, Any]]:
    terms = [t.strip().replace('"', "") for t in query.split() if t.strip()]
    if not terms:
        return []
    fts_q = " OR ".join(f'"{t}"' for t in terms)
    with connect(vault_path) as conn:
        where = "WHERE code_fts MATCH ?"
        params: list[Any] = [fts_q]
        if project:
            where += " AND n.project = ?"
            params.append(project)
        rows = conn.execute(
            f"""
            SELECT n.node_key, n.project, n.node_id, n.file, n.type, n.label,
                   n.signature, n.lineno, n.description,
                   bm25(code_fts) AS score
            FROM code_fts
            JOIN code_nodes n ON code_fts.node_key = n.node_key
            {where}
            ORDER BY score
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()

    if not rows:
        return []

    raw_scores = [abs(r["score"]) for r in rows]
    max_s = max(raw_scores) or 1.0
    return [
        {**dict(r), "score": round(abs(r["score"]) / max_s * 0.8, 4), "search": "fts5"}
        for r in rows
    ]


def _semantic_search(
    vault_path: Path, api_key: str, query: str, project: str, limit: int
) -> list[dict[str, Any]]:
    try:
        from google import genai
        from google.genai import types as gtypes

        client = genai.Client(api_key=api_key)
        result = client.models.embed_content(
            model="gemini-embedding-001",
            contents=query,
            config=gtypes.EmbedContentConfig(task_type="RETRIEVAL_QUERY"),
        )
        q_vec = list(result.embeddings[0].values)
    except Exception:
        return []

    db = _db_path(vault_path)
    if not db.exists():
        return []

    conn = sqlite3.connect(db, timeout=30)
    try:
        rows = conn.execute(
            "SELECT node_key, vector FROM code_embeddings"
            + (" WHERE node_key LIKE ?" if project else ""),
            ([f"{project}/%"] if project else []),
        ).fetchall()
    finally:
        conn.close()

    scored = []
    for row in rows:
        vec = list(struct.unpack(_VEC_FMT, row[1]))
        sim = _cosine(q_vec, vec)
        scored.append({"node_key": row[0], "score": round(sim, 4)})

    scored.sort(key=lambda x: -x["score"])
    return [
        {"node_key": r["node_key"], "score": r["score"], "search": "semantic"}
        for r in scored[:limit]
    ]


def _hybrid_merge(
    fts: list[dict[str, Any]],
    sem: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    fts_map = {r["node_key"]: r for r in fts}
    sem_map = {r["node_key"]: r["score"] for r in sem}
    merged: dict[str, dict[str, Any]] = {}

    for r in fts:
        key = r["node_key"]
        sem_s = sem_map.get(key, 0.0)
        merged[key] = {
            **r,
            "score": round(r["score"] * 0.45 + sem_s * 0.55, 4),
            "search": "hybrid" if key in sem_map else "fts5",
        }

    for r in sem:
        key = r["node_key"]
        if key not in merged:
            fts_s = fts_map[key]["score"] if key in fts_map else 0.0
            merged[key] = {
                "node_key": key,
                "score": round(fts_s * 0.45 + r["score"] * 0.55, 4),
                "search": "semantic",
            }

    return sorted(merged.values(), key=lambda x: -x["score"])[:limit]


def _enrich(vault_path: Path, results: list[dict[str, Any]], project: str) -> list[dict[str, Any]]:
    """Fetch full node data and attach caller/callee counts."""
    if not results:
        return []
    with connect(vault_path) as conn:
        enriched = []
        for r in results:
            nk = r["node_key"]
            row = conn.execute("SELECT * FROM code_nodes WHERE node_key=?", (nk,)).fetchone()
            if not row:
                continue
            node = dict(row)
            nid = node["node_id"]
            proj = node["project"]
            callers = conn.execute(
                "SELECT COUNT(*) FROM code_edges WHERE project=? AND target=? AND relation='calls'",
                (proj, nid),
            ).fetchone()[0]
            callees = conn.execute(
                "SELECT target FROM code_edges WHERE project=? AND source=? AND relation='calls'",
                (proj, nid),
            ).fetchall()
            node.update(
                {
                    "score": r["score"],
                    "search": r.get("search", "fts5"),
                    "callers": callers,
                    "callees": [c[0] for c in callees],
                    "source": node.get("source", "")[:500],  # trim for response
                }
            )
            enriched.append(node)
    return enriched


def get_node(vault_path: Path, project: str, node_id: str) -> dict[str, Any] | None:
    """Fetch a single node with full source and edges."""
    with connect(vault_path) as conn:
        row = conn.execute(
            "SELECT * FROM code_nodes WHERE project=? AND node_id=?", (project, node_id)
        ).fetchone()
        if not row:
            return None
        node = dict(row)
        callers = conn.execute(
            "SELECT source FROM code_edges WHERE project=? AND target=? AND relation='calls'",
            (project, node_id),
        ).fetchall()
        callees = conn.execute(
            "SELECT target FROM code_edges WHERE project=? AND source=? AND relation='calls'",
            (project, node_id),
        ).fetchall()
        node["callers"] = [r[0] for r in callers]
        node["callees"] = [r[0] for r in callees]
        return node


def list_projects(vault_path: Path) -> list[str]:
    if not _db_path(vault_path).exists():
        return []
    with connect(vault_path) as conn:
        rows = conn.execute("SELECT DISTINCT project FROM code_nodes ORDER BY project").fetchall()
    return [r[0] for r in rows]


def project_stats(vault_path: Path, project: str) -> dict[str, Any]:
    with connect(vault_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM code_nodes WHERE project=?", (project,)
        ).fetchone()[0]
        by_type = conn.execute(
            "SELECT type, COUNT(*) FROM code_nodes WHERE project=? GROUP BY type", (project,)
        ).fetchall()
        edges = conn.execute(
            "SELECT COUNT(*) FROM code_edges WHERE project=?", (project,)
        ).fetchone()[0]
    return {"total_nodes": total, "edges": edges, "by_type": {r[0]: r[1] for r in by_type}}


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0
