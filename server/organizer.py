"""
Obsidian vault organizer — creates hub-and-spoke MOC (Map of Content) index
files for every folder so the Obsidian graph view has a clear structure.

Each subfolder in the vault gets an index.md that:
  - Wikilinks to every direct child .md file
  - Wikilinks to every direct subfolder's index
  - Gets indexed by SQLite so it's searchable

After building indexes, runs relink_all to refresh all cross-file wikilinks.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from .config import SynapseConfig
from .diff import relink_all
from .encryption import write_text
from .index import MemoryIndex
from .memory_file import path_to_key, render_memory_file


def organize_vault(config: SynapseConfig) -> dict[str, Any]:
    vault = config.vault_path
    if not vault.exists():
        return {"error": f"Vault not found: {vault}"}

    created: list[str] = []
    updated: list[str] = []

    # Walk every real subdirectory (skip _ and . prefixed names)
    dirs: list[Path] = []
    for p in sorted(vault.rglob("*")):
        if not p.is_dir():
            continue
        parts = p.relative_to(vault).parts
        if any(part.startswith("_") or part.startswith(".") for part in parts):
            continue
        dirs.append(p)

    for folder in dirs:
        rel_parts = folder.relative_to(vault).parts

        # Direct child .md files (skip _ files and existing index.md)
        children = sorted(
            [
                f
                for f in folder.iterdir()
                if f.is_file()
                and f.suffix == ".md"
                and not f.name.startswith("_")
                and f.stem != "index"
            ],
            key=lambda f: f.stem,
        )

        # Direct child subdirectories (skip _ and . prefixed)
        subdirs = sorted(
            [
                d
                for d in folder.iterdir()
                if d.is_dir() and not d.name.startswith("_") and not d.name.startswith(".")
            ],
            key=lambda d: d.name,
        )

        if not children and not subdirs:
            continue

        # Dot-notation key: projects.myapp -> projects.myapp.index
        key = ".".join(rel_parts) + ".index"
        title = " / ".join(p.replace("-", " ").title() for p in rel_parts)

        lines: list[str] = [f"# {title}\n"]

        if subdirs:
            lines.append("## Sections\n")
            for d in subdirs:
                sub_key = ".".join([*rel_parts, d.name, "index"])
                label = d.name.replace("-", " ").title()
                lines.append(f"- [[{sub_key.replace('.', '/')}|{label}]]")
            lines.append("")

        if children:
            lines.append("## Files\n")
            for f in children:
                child_key = path_to_key(vault, f)
                label = f.stem.replace("-", " ").title()
                lines.append(f"- [[{child_key.replace('.', '/')}|{label}]]")
            lines.append("")

        content = "\n".join(lines)

        related = [path_to_key(vault, f) for f in children[:8]]

        frontmatter = {
            "key": key,
            "type": "index",
            "scope": "global",
            "weight": 0.3,
            "confidence": "confirmed",
            "last_used": date.today().isoformat(),
            "last_updated": date.today().isoformat(),
            "version": 1,
            "triggers": list(rel_parts),
            "related": related,
        }

        index_path = folder / "index.md"
        existed = index_path.exists()
        write_text(config, index_path, render_memory_file(frontmatter, content))

        idx = MemoryIndex(vault, lambda p: p.read_text(encoding="utf-8"))
        idx.upsert_file(index_path)

        (updated if existed else created).append(key)

    relink_result = relink_all(config)

    return {
        "status": "organized",
        "indexes_created": created,
        "indexes_updated": updated,
        "files_relinked": len(relink_result.get("files", [])),
        "tip": "Open vault/ in Obsidian and enable Graph View to see the hub-and-spoke structure.",
    }
