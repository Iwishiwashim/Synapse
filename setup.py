"""
Interactive Synapse setup.
Run once after cloning:  python setup.py

  - Saves your Gemini API key to .env
  - Configures vault_path and raw_archive_path in config.yaml
  - Creates the vault directory if it does not exist
  - Writes Claude Desktop MCP config  (optional)
  - Writes Claude Code user MCP config (optional)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
CONFIG_FILE = ROOT / "config.yaml"

_VAULT_FOLDERS = ["identity", "life", "work", "projects", "patterns", "chats", "metadata"]


# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------


def _read_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    result: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def _write_env(env: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in env.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# config.yaml helpers
# ---------------------------------------------------------------------------


def _read_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _write_config(cfg: dict) -> None:
    lines = [
        f"vault_path: {cfg['vault_path']}",
        "",
        "# Path to synapse_extracted/ folder (raw conversation archive from memory_import_ai_export).",
        "# Leave blank if you are not using the raw archive features.",
        f"raw_archive_path: {cfg.get('raw_archive_path', '')}",
        "",
        f"encryption: {str(cfg.get('encryption', False)).lower()}",
        f"cloud_search: {str(cfg.get('cloud_search', False)).lower()}",
        f"git_enabled: {str(cfg.get('git_enabled', True)).lower()}",
        f"weekly_report_day: {cfg.get('weekly_report_day', 'monday')}",
        f"life_mode: {str(cfg.get('life_mode', False)).lower()}",
        f"pending_auto_expire_days: {cfg.get('pending_auto_expire_days', 7)}",
        "",
        "# Gemini API key — required for all AI processing (extraction, embeddings, semantic search).",
        "# Recommended: set via environment variable so this file stays commit-safe.",
        "#   GEMINI_API_KEY=your_key_here",
        "# Or paste below:",
        f"gemini_api_key: \"{cfg.get('gemini_api_key', '')}\"",
        "",
        "# How Claude handles memory writes.",
        "# review — Claude proposes a diff you approve before anything is written (default, safer).",
        "# auto   — Claude writes directly with no confirmation needed (faster).",
        f"write_mode: {cfg.get('write_mode', 'review')}",
    ]
    CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Vault initialisation
# ---------------------------------------------------------------------------


def _init_vault(vault_path: Path) -> list[str]:
    """Create vault directory and standard sub-folders. Returns list of created paths."""
    created = []
    if not vault_path.exists():
        vault_path.mkdir(parents=True, exist_ok=True)
        created.append(str(vault_path))
    for folder in _VAULT_FOLDERS:
        sub = vault_path / folder
        if not sub.exists():
            sub.mkdir(parents=True, exist_ok=True)
            created.append(str(sub))
    return created


# ---------------------------------------------------------------------------
# Claude Desktop config
# ---------------------------------------------------------------------------


def _desktop_config_path() -> Path | None:
    platform = sys.platform
    if platform == "win32":
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if localappdata:
            packages = Path(localappdata) / "Packages"
            if packages.exists():
                for entry in packages.iterdir():
                    if entry.name.startswith("Claude_"):
                        candidate = (
                            entry
                            / "LocalCache"
                            / "Roaming"
                            / "Claude"
                            / "claude_desktop_config.json"
                        )
                        if candidate.parent.exists():
                            return candidate
        appdata = os.environ.get("APPDATA", "")
        return (Path(appdata) / "Claude" / "claude_desktop_config.json") if appdata else None
    base = (
        Path.home() / "Library" / "Application Support"
        if platform == "darwin"
        else Path.home() / ".config"
    )
    return base / "Claude" / "claude_desktop_config.json"


def _write_desktop_config(api_key: str) -> str:
    config_path = _desktop_config_path()
    if not config_path:
        return "Could not detect Claude Desktop config location."

    python_exe = str(ROOT / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / "python")
    if sys.platform == "win32":
        python_exe += ".exe"

    entry = {
        "command": python_exe,
        "args": [str(ROOT / "run_server.py")],
        "env": {"GEMINI_API_KEY": api_key},
    }

    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    existing.setdefault("mcpServers", {})["synapse"] = entry
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return str(config_path)


# ---------------------------------------------------------------------------
# Claude Code config  (~/.claude.json, user scope = available in all projects)
# ---------------------------------------------------------------------------


def _claude_code_config_path() -> Path:
    return Path.home() / ".claude.json"


def _write_claude_code_config(api_key: str) -> str:
    config_path = _claude_code_config_path()
    python_exe = str(ROOT / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / "python")
    if sys.platform == "win32":
        python_exe += ".exe"
    launcher = str(ROOT / "run_server.py")

    entry = {
        "type": "stdio",
        "command": python_exe,
        "args": [launcher],
        "env": {"GEMINI_API_KEY": api_key},
    }

    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    existing.setdefault("mcpServers", {})["synapse"] = entry
    config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return str(config_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _ask(prompt: str, default: str = "") -> str:
    try:
        val = input(prompt).strip()
        return val if val else default
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        sys.exit(0)


def main() -> None:
    print("Synapse setup")
    print("-" * 40)
    print("Synapse is a local MCP memory server for Claude.")
    print("It stores structured memories as Markdown files and exposes them via 30+ tools.")
    print()
    print("Get a free Gemini API key at: https://aistudio.google.com/apikey")
    print()

    env = _read_env()
    cfg = _read_config()

    # --- Gemini API key ---
    current_key = env.get("GEMINI_API_KEY", "") or str(cfg.get("gemini_api_key", ""))
    if current_key:
        masked = current_key[:8] + "..." + current_key[-4:]
        prompt = f"Gemini API key [{masked}] (Enter to keep): "
    else:
        prompt = "Gemini API key: "

    new_key = _ask(prompt)
    api_key = new_key or current_key

    if not api_key:
        print("No key entered. Re-run setup.py when you have one.")
        sys.exit(0)

    if new_key and new_key != current_key:
        env["GEMINI_API_KEY"] = api_key
        _write_env(env)
        print(f"  Saved to {ENV_FILE.name}")
    else:
        print("  Key unchanged.")

    print()

    # --- Vault path ---
    current_vault = str(cfg.get("vault_path", "./vault"))
    vault_str = _ask(f"Vault path [{current_vault}] (Enter to keep): ") or current_vault
    cfg["vault_path"] = vault_str

    vault_path = Path(vault_str).expanduser()
    if not vault_path.is_absolute():
        vault_path = (ROOT / vault_path).resolve()

    created = _init_vault(vault_path)
    if created:
        print(f"  Created vault at {vault_path}")
        for sub in created[1:]:
            print(f"    {Path(sub).name}/")
    else:
        print(f"  Vault exists at {vault_path}")

    print()

    # --- Raw archive path ---
    current_archive = str(cfg.get("raw_archive_path", "./synapse_extracted"))
    print("Raw archive path: folder where synapse_extracted/ conversations are stored.")
    print("Leave blank to disable raw archive features (memory_get_raw, memory_search_raw).")
    archive_str = (
        _ask(f"Raw archive path [{current_archive}] (Enter to keep, '-' to disable): ")
        or current_archive
    )
    if archive_str == "-":
        archive_str = ""
    cfg["raw_archive_path"] = archive_str

    if archive_str:
        archive_path = Path(archive_str).expanduser()
        if not archive_path.is_absolute():
            archive_path = (ROOT / archive_path).resolve()
        if not archive_path.exists():
            print(
                f"  Note: {archive_path} does not exist yet — create it when you run memory_import_ai_export."
            )
        else:
            print(f"  Archive path: {archive_path}")
    else:
        print("  Raw archive disabled.")

    print()

    # --- Write mode ---
    print("Memory write mode:")
    print(
        "  review — Claude proposes a diff you approve before anything is written (default, safer)"
    )
    print("  auto   — Claude writes directly, no confirmation needed (faster)")
    current_mode = str(cfg.get("write_mode", "review"))
    mode_input = _ask(f"Write mode [{current_mode}] (Enter to keep): ") or current_mode
    cfg["write_mode"] = "auto" if mode_input.strip().lower() == "auto" else "review"
    print(f"  Write mode: {cfg['write_mode']}")
    print()

    # Write updated config.yaml
    _write_config(cfg)
    print(f"  config.yaml updated.")
    print()

    # --- Claude Desktop ---
    desktop_path = _desktop_config_path()
    if desktop_path:
        choice = _ask(f"Write Claude Desktop MCP config? [{desktop_path.name}] (Y/n): ", "y")
        if choice.lower() in ("", "y", "yes"):
            result = _write_desktop_config(api_key)
            print(f"  Written to {result}")
            print("  Restart Claude Desktop to pick up the changes.")
        else:
            print("  Skipped.")
    else:
        print("  Could not detect Claude Desktop config — skipping.")

    print()

    # --- Claude Code ---
    code_path = _claude_code_config_path()
    choice = _ask(f"Write Claude Code MCP config? [{code_path.name}] (Y/n): ", "y")
    if choice.lower() in ("", "y", "yes"):
        result = _write_claude_code_config(api_key)
        print(f"  Written to {result}")
        print("  Synapse is now available in all Claude Code sessions.")
    else:
        print("  Skipped.")

    print()
    print("Setup complete.")
    print()
    print("Key tools available after connecting:")
    print("  memory_context()         — load identity + vault health (call first every session)")
    print("  memory_search(query)     — FTS5 search across all memory files")
    print("  memory_get(key)          — fetch a specific memory file")
    print("  memory_propose_update()  — propose a memory write (previews diff)")
    print("  memory_save_chat()       — save this conversation as a searchable chat node")
    print("  memory_import_ai_export()— import ChatGPT/Claude data exports")
    print("  memory_scan_project()    — index a codebase with AI-extracted function nodes")
    print("  memory_code_search()     — hybrid FTS5 + semantic search over code nodes")
    print("  memory_deep_search()     — graph-guided chat search (run memory_build_graph first)")
    print()
    print("Run 'pytest tests/' to verify the install.")


if __name__ == "__main__":
    main()
