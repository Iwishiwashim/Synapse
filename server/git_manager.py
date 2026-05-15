from __future__ import annotations

from pathlib import Path

from git import InvalidGitRepositoryError, Repo

from .config import SynapseConfig


def commit_paths(config: SynapseConfig, paths: list[Path], message: str) -> dict[str, str]:
    if not config.git_enabled:
        return {"status": "skipped", "reason": "git disabled"}
    try:
        repo = Repo(config.root_path, search_parent_directories=True)
    except InvalidGitRepositoryError:
        return {"status": "skipped", "reason": "not a git repository"}

    repo.index.add([str(path) for path in paths])
    if not repo.index.diff("HEAD") and not repo.untracked_files:
        return {"status": "skipped", "reason": "no git changes"}
    commit = repo.index.commit(message)
    return {"status": "committed", "commit": commit.hexsha}
