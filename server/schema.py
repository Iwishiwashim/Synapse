from __future__ import annotations

from pathlib import Path
from typing import Any

from .memory_file import CONFIDENCE_VALUES, REQUIRED_FIELDS, parse_memory_text, path_to_key


def validate_memory(
    frontmatter: dict[str, Any], content: str, path: Path | None = None, vault: Path | None = None
) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED_FIELDS - set(frontmatter))
    if missing:
        errors.append(f"Missing frontmatter fields: {', '.join(missing)}")

    key = frontmatter.get("key")
    if not isinstance(key, str) or "." not in key:
        errors.append("key must be dot-notation")
    elif path and vault:
        expected = path_to_key(vault, path)
        if key != expected:
            errors.append(f"key {key!r} does not match file path key {expected!r}")

    weight = frontmatter.get("weight")
    if not isinstance(weight, (int, float)) or not 0 <= float(weight) <= 1:
        errors.append("weight must be a number from 0.0 to 1.0")

    confidence = frontmatter.get("confidence")
    if confidence not in CONFIDENCE_VALUES:
        errors.append("confidence must be one of proposed, confirmed, deprecated")

    version = frontmatter.get("version")
    if not isinstance(version, int) or version < 1:
        errors.append("version must be a positive integer")

    for field in ("triggers", "related"):
        if not isinstance(frontmatter.get(field), list):
            errors.append(f"{field} must be a list")

    if not content.strip():
        errors.append("content must not be empty")

    return errors


def validate_memory_file(path: Path, vault: Path) -> list[str]:
    frontmatter, content = parse_memory_text(path.read_text(encoding="utf-8"))
    return validate_memory(frontmatter, content, path, vault)


def validate_vault(vault: Path) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {}
    seen_keys: dict[str, Path] = {}
    for path in sorted(vault.rglob("*.md")):
        if path.name.startswith("_"):
            continue
        errors = validate_memory_file(path, vault)
        frontmatter, _ = parse_memory_text(path.read_text(encoding="utf-8"))
        key = str(frontmatter.get("key", ""))
        if key in seen_keys:
            errors.append(f"Duplicate key also used by {seen_keys[key].relative_to(vault)}")
        elif key:
            seen_keys[key] = path
        if errors:
            results[str(path.relative_to(vault)).replace("\\", "/")] = errors
    return results
