from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .memory_file import content_preview, parse_memory_text, path_to_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,
  file_path TEXT NOT NULL,
  type TEXT,
  scope TEXT,
  weight REAL,
  confidence TEXT,
  last_used TEXT,
  triggers TEXT,
  content_preview TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  key,
  triggers,
  content_preview
);
"""


class MemoryIndex:
    def __init__(self, vault: Path, read_file: Callable[[Path], str] | None = None) -> None:
        self.vault = vault
        self.db_path = vault / "_index.db"
        self.read_file = read_file or (lambda path: path.read_text(encoding="utf-8"))

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA)
        except sqlite3.DatabaseError:
            conn.close()
            if self.db_path.exists():
                self.db_path.unlink()
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
        return conn

    def rebuild(self, api_key: str | None = None) -> None:
        self.vault.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("DROP TABLE IF EXISTS memories_fts")
            conn.execute("DROP TABLE IF EXISTS memories")
            conn.executescript(SCHEMA)
            conn.execute("DELETE FROM memories_fts")
            conn.execute("DELETE FROM memories")
            for path in sorted(self.vault.rglob("*.md")):
                if path.name.startswith("_"):
                    continue
                self.upsert_file(path, conn=conn)
            conn.commit()

        # Rebuild semantic embeddings if API key provided
        if api_key:
            from .embeddings import embed_document, upsert_vector
            for path in sorted(self.vault.rglob("*.md")):
                if path.name.startswith("_"):
                    continue
                try:
                    text = self.read_file(path)
                    key = path_to_key(self.vault, path)
                    vec = embed_document(api_key, key, text)
                    if vec:
                        upsert_vector(self.db_path, key, vec)
                except Exception:
                    pass

    def rebuild_if_out_of_sync(self) -> None:
        files = {
            str(path.relative_to(self.vault)).replace("\\", "/")
            for path in self.vault.rglob("*.md")
            if not path.name.startswith("_")
        }
        with self.connect() as conn:
            try:
                indexed = {row["file_path"] for row in conn.execute("SELECT file_path FROM memories")}
            except sqlite3.DatabaseError:
                indexed = set()
        if files != indexed:
            self.rebuild()

    def upsert_file(self, path: Path, conn: sqlite3.Connection | None = None) -> None:
        own_conn = conn is None
        db = conn or self.connect()
        try:
            frontmatter, content = parse_memory_text(self.read_file(path))
            key = str(frontmatter.get("key") or path_to_key(self.vault, path))
            triggers = json.dumps(frontmatter.get("triggers", []))
            # Chat summaries: index full content so deep sections (Key Facts, Decisions, etc.) are searchable
            is_chat = path.parts[-2] == "chats" if len(path.parts) >= 2 else False
            preview = content.strip() if is_chat else content_preview(content)
            db.execute(
                """
                INSERT INTO memories(key, file_path, type, scope, weight, confidence, last_used, triggers, content_preview)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  file_path=excluded.file_path,
                  type=excluded.type,
                  scope=excluded.scope,
                  weight=excluded.weight,
                  confidence=excluded.confidence,
                  last_used=excluded.last_used,
                  triggers=excluded.triggers,
                  content_preview=excluded.content_preview
                """,
                (
                    key,
                    str(path.relative_to(self.vault)).replace("\\", "/"),
                    frontmatter.get("type"),
                    frontmatter.get("scope"),
                    frontmatter.get("weight"),
                    frontmatter.get("confidence"),
                    frontmatter.get("last_used"),
                    triggers,
                    preview,
                ),
            )
            row = db.execute("SELECT id FROM memories WHERE key = ?", (key,)).fetchone()
            if row:
                db.execute("DELETE FROM memories_fts WHERE rowid = ?", (row["id"],))
                db.execute(
                    "INSERT INTO memories_fts(rowid, key, triggers, content_preview) VALUES (?, ?, ?, ?)",
                    (row["id"], key, " ".join(frontmatter.get("triggers", [])), preview),
                )
            if own_conn:
                db.commit()
        finally:
            if own_conn:
                db.close()

    def search(self, query: str, limit: int = 4) -> list[dict[str, Any]]:
        self.rebuild_if_out_of_sync()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT memories.key, memories.file_path, memories.type, memories.scope, memories.confidence,
                       memories.weight, memories.content_preview, bm25(memories_fts) AS bm25_score
                FROM memories_fts
                JOIN memories ON memories_fts.rowid = memories.id
                WHERE memories_fts MATCH ?
                ORDER BY bm25_score
                LIMIT ?
                """,
                (_fts_query(query), limit),
            ).fetchall()

        if not rows:
            return []

        # BM25 returns negative values; more negative = better match.
        # Flip signs and normalize to [0.0, 1.0] so higher = more relevant.
        raw_scores = [abs(row["bm25_score"]) for row in rows]
        max_score = max(raw_scores) if max(raw_scores) > 0 else 1.0

        results = []
        for row, raw in zip(rows, raw_scores):
            d = dict(row)
            file_weight = float(d.pop("weight") or 0.5)
            bm25_norm = raw / max_score                          # 0.0–1.0 relevance
            # Blend: 80% FTS relevance, 20% file importance weight
            d["score"] = round(bm25_norm * 0.8 + file_weight * 0.2, 4)
            d.pop("bm25_score", None)
            results.append(d)

        results.sort(key=lambda x: -x["score"])
        # Truncate content_preview in results — full text is indexed but callers only need a snippet
        for r in results:
            if r.get("content_preview") and len(r["content_preview"]) > 360:
                r["content_preview"] = r["content_preview"][:360] + "…"
        return results

    def stats(self) -> dict[str, int]:
        self.rebuild_if_out_of_sync()
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS count FROM memories").fetchone()["count"]
            rows = conn.execute("SELECT confidence, COUNT(*) AS count FROM memories GROUP BY confidence").fetchall()
        stats = {"total": total, "confirmed": 0, "proposed": 0, "deprecated": 0}
        for row in rows:
            if row["confidence"] in stats:
                stats[row["confidence"]] = row["count"]
        return stats


def _fts_query(query: str) -> str:
    terms = [term.strip().replace('"', "") for term in query.split() if term.strip()]
    if not terms:
        return '""'
    return " OR ".join(f'"{term}"' for term in terms)
