from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import SynapseConfig
from .encryption import read_text
from .memory_file import parse_memory_text, path_to_key

_MIN_CONTENT_CHARS = 40  # files shorter than this are flagged as thin/empty
_TRIGGER_DUP_THRESHOLD = 0.55  # Jaccard on triggers
_CONTENT_DUP_THRESHOLD = 0.42  # Jaccard on word sets


def _jaccard(a: set, b: set) -> float:
    u = a | b
    return len(a & b) / len(u) if u else 0.0


def _load_all(config: SynapseConfig) -> list[dict[str, Any]]:
    vault = config.vault_path
    entries = []
    for md in sorted(vault.rglob("*.md")):
        rel = md.relative_to(vault)
        try:
            text = read_text(config, md)
            fm, content = parse_memory_text(text)
        except Exception:
            continue
        key = str(fm.get("key") or path_to_key(vault, md))
        triggers = set(str(t).lower() for t in (fm.get("triggers") or []))
        entries.append(
            {
                "key": key,
                "path": rel,
                "triggers": triggers,
                "content": content,
                "content_len": len(content.strip()),
                "type": fm.get("type", "note"),
            }
        )
    return entries


def _category(key: str) -> str:
    return key.split(".")[0] if key else ""


def memory_deduplicate(config: SynapseConfig, auto_clean: bool = False) -> dict[str, Any]:
    """
    Scans the vault for:
    - Stray files (test/ folder, orphaned root _ files)
    - Thin/empty files (< _MIN_CONTENT_CHARS characters of content)
    - Duplicate pairs (high trigger + content overlap within same category)

    If auto_clean=True, deletes stray and thin files automatically.
    Duplicate merging is always left for manual review — content merges need judgment.
    """
    vault = config.vault_path
    entries = _load_all(config)

    stray: list[dict] = []
    thin: list[dict] = []
    duplicate_groups: list[dict] = []
    deleted: list[str] = []

    # --- Stray files ---
    for e in entries:
        parts = e["path"].parts
        reason = None
        if parts[0] == "test":
            reason = "test/ folder — should not exist in vault"
        elif (
            e["path"].parent == Path(".")
            and e["path"].name.startswith("_")
            and e["path"].name
            not in {"_index.db", "_weekly.md", "_pending.json", "_rejections.jsonl"}
        ):
            reason = "orphaned root _ file"
        if reason:
            stray.append({"key": e["key"], "path": str(e["path"]), "reason": reason})

    # --- Thin files ---
    for e in entries:
        if (
            e["type"] != "index"
            and e["content_len"] < _MIN_CONTENT_CHARS
            and not e["key"].startswith("chats.")
        ):
            thin.append(
                {
                    "key": e["key"],
                    "path": str(e["path"]),
                    "content_len": e["content_len"],
                    "hint": "likely a stub — consider merging into a richer entry",
                }
            )

    # --- Duplicate detection within same top-level category ---
    by_cat: dict[str, list] = {}
    for e in entries:
        cat = _category(e["key"])
        by_cat.setdefault(cat, []).append(e)

    seen_pairs: set[frozenset] = set()
    for cat, group in by_cat.items():
        # Loophole has 185 deep nodes — only compare shallow ones (max depth 3)
        if cat == "projects":
            group = [e for e in group if len(e["key"].split(".")) <= 3]

        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                pair = frozenset([a["key"], b["key"]])
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                trigger_j = _jaccard(a["triggers"], b["triggers"])
                if trigger_j < _TRIGGER_DUP_THRESHOLD:
                    continue

                a_words = set(re.findall(r"\w+", a["content"].lower()))
                b_words = set(re.findall(r"\w+", b["content"].lower()))
                content_j = _jaccard(a_words, b_words)

                if content_j >= _CONTENT_DUP_THRESHOLD or trigger_j >= 0.75:
                    keep = a["key"] if len(a["key"]) <= len(b["key"]) else b["key"]
                    duplicate_groups.append(
                        {
                            "keys": [a["key"], b["key"]],
                            "trigger_similarity": round(trigger_j, 2),
                            "content_similarity": round(content_j, 2),
                            "suggestion": f"merge into {keep}",
                        }
                    )

    # --- Auto-clean stray + thin ---
    if auto_clean:
        stray_paths = {s["path"] for s in stray}
        thin_paths = {t["path"] for t in thin}
        for path_str in stray_paths | thin_paths:
            p = vault / path_str
            if p.exists():
                p.unlink()
                deleted.append(path_str)
        # remove newly empty directories
        for d in sorted(vault.rglob("*"), key=lambda x: -len(x.parts)):
            if d.is_dir() and d != vault and not any(d.iterdir()):
                d.rmdir()

    return {
        "stray_files": stray,
        "thin_files": thin,
        "duplicate_groups": duplicate_groups,
        "auto_deleted": deleted,
        "summary": {
            "stray": len(stray),
            "thin": len(thin),
            "duplicate_groups": len(duplicate_groups),
            "auto_deleted": len(deleted),
            "action": (
                "stray+thin deleted"
                if auto_clean
                else "report only — pass auto_clean=true to delete stray/thin automatically"
            ),
        },
    }
