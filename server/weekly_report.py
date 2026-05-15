from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import SynapseConfig
from .diff import load_pending
from .encryption import read_text
from .index import MemoryIndex


def generate_weekly_report(config: SynapseConfig, today: date | None = None) -> dict[str, str]:
    current = today or date.today()
    week_start = current - timedelta(days=current.weekday())
    written = _git_written_this_week(config.root_path, week_start)
    pending = load_pending(config)
    rejected = _rejected_this_week(config.vault_path / "_rejections.jsonl", week_start)
    stats = MemoryIndex(config.vault_path, lambda path: read_text(config, path)).stats()
    report = _render_report(week_start, written, pending, rejected, stats)
    path = config.vault_path / "_weekly.md"
    path.write_text(report, encoding="utf-8")
    return {"status": "generated", "file_path": str(path), "week_start": week_start.isoformat()}


def _render_report(
    week_start: date,
    written: list[str],
    pending: list[dict],
    rejected: list[dict],
    stats: dict[str, int],
) -> str:
    lines = [f"# Synapse Report - Week of {week_start.isoformat()}", ""]
    lines += ["## Written this week", *_bullets(written, "No approved writes found."), ""]
    lines += [
        "## Pending approval",
        *_bullets(
            [f"{p['key']} - {p.get('reason', 'pending')}" for p in pending], "No pending approvals."
        ),
        "",
    ]
    lines += [
        "## Rejected this week",
        *_bullets(
            [f"{r['key']} - rejected, {r.get('reason') or 'no reason provided'}" for r in rejected],
            "No rejected patches.",
        ),
        "",
    ]
    lines += [
        "## Conflicts detected",
        "- Run `memory.conflicts` for current conflict analysis.",
        "",
    ]
    lines += [
        "## Memory health",
        f"- Total memories: {stats.get('total', 0)}",
        f"- Confirmed: {stats.get('confirmed', 0)}",
        f"- Proposed: {stats.get('proposed', 0)}",
        f"- Deprecated: {stats.get('deprecated', 0)}",
        "",
    ]
    return "\n".join(lines)


def _bullets(items: list[str], empty: str) -> list[str]:
    if not items:
        return [f"- {empty}"]
    return [f"- {item}" for item in items]


def _git_written_this_week(root: Path, week_start: date) -> list[str]:
    try:
        from git import InvalidGitRepositoryError, Repo

        repo = Repo(root, search_parent_directories=True)
    except Exception:
        return []

    since = datetime.combine(week_start, datetime.min.time()).isoformat()
    items: list[str] = []
    for commit in repo.iter_commits(paths="vault", since=since):
        if commit.message.startswith("Synapse memory update:"):
            items.append(commit.message.strip())
    return items


def _rejected_this_week(path: Path, week_start: date) -> list[dict]:
    if not path.exists():
        return []
    items: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        rejected_at = str(row.get("rejected_at", ""))[:10]
        if rejected_at >= week_start.isoformat():
            items.append(row)
    return items
