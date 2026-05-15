"""
Standalone launcher for Claude Code MCP config.
Adds the Synapse root to sys.path so 'server' is importable
regardless of what directory Claude Code is run from.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # project root

from server.main import main

main()
