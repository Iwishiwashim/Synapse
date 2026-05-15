"""
MCP server entry point. Keep this at the project root.
Claude Desktop and Claude Code MCP configs point here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.main import main

main()
