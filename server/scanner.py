from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .file_readers import SUPPORTED_EXTENSIONS, extract_text as _extract_file_text
from .graph import extract_code_graph

if TYPE_CHECKING:
    from .config import SynapseConfig

SKIP_DIRS = {
    "node_modules", "__pycache__", ".venv", "venv", ".git",
    "dist", "build", ".next", "release", "desktop-artifacts",
    "out", ".turbo", ".cache", "coverage",
}
SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".lock", ".ico", ".png", ".jpg", ".jpeg", ".gif",
    ".svg", ".woff", ".woff2", ".ttf", ".eot", ".map", ".db", ".sqlite",
    ".exe", ".dll", ".so", ".bin", ".zip", ".tar", ".gz",
}
PRIORITY_NAMES = {
    "README.md", "readme.md", "README.txt",
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
    "requirements.txt", "requirements-control.txt",
    "next.config.ts", "next.config.js", "vite.config.ts", "vite.config.js",
    "tsconfig.json", "config.yaml", "config.yml",
    "main.py", "server.py", "app.py",
    "index.ts", "index.js", "index.tsx",
}
PRIORITY_PATTERNS = [
    re.compile(r"^(main|index|app|server|entry|config|settings)\.(py|ts|js|tsx|jsx|yaml|yml|json)$"),
    re.compile(r"^README", re.IGNORECASE),
    re.compile(r"^(electron|src)/(main|preload)\.(cjs|js|ts)$"),
]
SOURCE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".cs"}

# Extended code extensions - rule-based extraction only, zero inference cost
EXTENDED_CODE_EXTENSIONS = {
    ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",  # C / C++
    ".rb",                                        # Ruby
    ".php",                                       # PHP
    ".swift",                                     # Swift
    ".kt", ".kts",                               # Kotlin
    ".dart",                                      # Dart
    ".scala",                                     # Scala
    ".r",                                         # R
    ".lua",                                       # Lua
    ".sh", ".bash", ".zsh",                      # Shell
    ".ps1",                                       # PowerShell
    ".sql",                                       # SQL
    ".ex", ".exs",                               # Elixir
    ".hs",                                        # Haskell
    ".clj",                                       # Clojure
    ".fs", ".fsx",                               # F#
    ".vim",                                       # Vim script
    ".toml", ".ini", ".cfg",                     # Config
}

# Plain text — stored directly, no AI needed
PLAIN_TEXT_EXTENSIONS = {".txt"}

DATA_EXTENSIONS = {".json", ".md", ".txt", ".yaml", ".yml"}

MAX_FILE_BYTES = 12_000
MAX_TOTAL_BYTES = 100_000
MAX_SOURCE_FILES = 20

_DATA_SYSTEM_PROMPT = """\
You are a personal memory extractor. Analyze this data file and output a JSON ARRAY of memory patches — one patch per distinct topic. No markdown fences, no explanation.

Each patch must follow this format:
{
  "key": "<category>.<slug>",
  "content": "<rich markdown — see depth rules below>",
  "type": "note",
  "scope": "global",
  "weight": 0.8,
  "signal": "high_signal",
  "reason": "<one sentence: why this memory matters>"
}

Categories and what goes in each:
- identity.* — who the person is, education, philosophy, interaction style, personal profile
- life.* — hobbies, travel, fitness, creative interests, cars, photography
- projects.* — each project gets its own patch with stack, status, features
- patterns.* — recurring skills, CTF techniques, security tools, coding patterns, academic methods
- work.* — dev environment, tools, accounts, stack

DEPTH RULES — the most important part:
Every patch must go 2 levels deep. Never write a vague one-liner summary.

Level 1 = the topic (e.g. "Blender")
Level 2 = the specific sub-components (e.g. rigging workflow, modifier stack order, specific addons, troubleshooting patterns)

For each sub-component, name the exact technique/tool/value — not a category.
  BAD:  "Uses various modifiers for character work."
  GOOD: "Modifier stack order: Subdivision → Shrinkwrap → CorrectiveSmooth → Armature. Uses Wiggle Bones addon for jiggle setups instead of Cloth modifier."

Do NOT create separate nested patches for sub-components. Keep all depth inside ONE patch per topic.
  BAD:  life.blender + life.blender-rigging + life.blender-modifiers (too nested)
  GOOD: life.blender — one rich file covering all aspects

Do NOT stay broad:
  BAD:  "Interested in cybersecurity and CTF competitions."
  GOOD: "ENTRYPOINT CTF 2026: solved 10 challenges. Pwn: 64-bit buffer overflow at offset 72, win function 0x401186. Web: SSRF via hex-encoded localhost bypass. Crypto: AES-ECB byte-at-a-time oracle."

STRUCTURE inside content:
- Use ## headers for each sub-component
- Use bullet points for specific facts
- Target 150-400 words per patch
- Always include: specific names, exact values, tool names, techniques, workflows

SPLITTING rules:
- One patch per distinct topic (not one massive patch)
- Extract EVERY project as its own patch
- Extract EVERY hobby/skill as its own life.* patch
- Minimum 6-10 patches for a rich data file
- Preserve verbatim: flags, port numbers, challenge names, API keys structure, emails

Output raw JSON array only. Start with [ and end with ].\
"""

_FILE_SYSTEM_PROMPT = """\
You are a developer knowledge extractor. Analyze the source file and output exactly one JSON object — nothing else. No markdown fences, no explanation.

Required format:
{
  "key": "projects.<project_slug>.<file_slug>",
  "content": "<markdown, 4-12 sentences>",
  "type": "code",
  "scope": "global",
  "weight": 0.8,
  "signal": "high_signal",
  "reason": "<one sentence: why this file matters architecturally>"
}

Rules for content — go 2 levels deep, never generic:
- Level 1: what the file does. Level 2: HOW it does it — name the exact mechanism.
- Name every exported function/class with its real name and what it actually does
- Name hardcoded values: ports (e.g. 5056), paths, env vars, constants, timeouts
- Name real imports from this project (e.g. "imports getFirebaseApp from @/lib/firebase")
- Name real external libraries used and WHY (not just "uses Flask" but "Flask routes /api/app/run to run whitelisted exes")
- If a function calls another, name the call chain: "startDesktopApp calls findFreePort then startBridge"
- Never write "this file handles X" — always name the actual mechanism behind X
- If trivial (re-export or stub), set weight 0.3 and write 1 sentence
  BAD:  "Handles authentication for the app."
  GOOD: "Exports useAuth hook: reads Firebase currentUser, wraps onAuthStateChanged, returns {user, loading, signOut}. Signs out via firebase/auth signOut() and redirects to /login."

project_slug: exact project name, lowercase, hyphens ok
file_slug: from path, hyphens as separators (electron/main.cjs → electron-main)

Output raw JSON only.\
"""



def scan_project(path_str: str) -> dict[str, Any]:
    """Raw file scan — returns tree + file contents, no AI extraction."""
    root = Path(path_str).expanduser().resolve()
    if not root.exists():
        return {"error": f"Path does not exist: {path_str}"}
    if not root.is_dir():
        return {"error": f"Path is not a directory: {path_str}"}

    result: dict[str, Any] = {
        "project_name": root.name,
        "root": str(root),
        "tree": _build_tree(root),
        "files": {},
        "detected": _detect_project(root),
    }

    budget = MAX_TOTAL_BYTES
    collected = 0

    for candidate in _walk_priority(root):
        rel = str(candidate.relative_to(root)).replace("\\", "/")
        if rel in result["files"]:
            continue
        text = _read_capped(candidate, MAX_FILE_BYTES)
        if text is None:
            continue
        result["files"][rel] = text
        budget -= len(text)
        collected += 1
        if budget <= 0:
            break

    if budget > 0:
        for candidate in _walk_source(root):
            if collected >= MAX_SOURCE_FILES + len(PRIORITY_NAMES):
                break
            rel = str(candidate.relative_to(root)).replace("\\", "/")
            if rel in result["files"]:
                continue
            ext = candidate.suffix.lower()
            if ext in SUPPORTED_EXTENSIONS:
                # Rich file — store a placeholder so _extract can re-read it properly
                text = f"[{ext.lstrip('.')} binary: {rel}]"
            else:
                text = _read_capped(candidate, min(MAX_FILE_BYTES, budget))
            if text is None:
                continue
            result["files"][rel] = text
            budget -= len(text)
            collected += 1
            if budget <= 0:
                break

    return result


_EXTRACT_WORKERS = 4  # stay under 15 RPM free tier


def scan_and_extract(config: "SynapseConfig", path_str: str) -> dict[str, Any]:
    """
    Scan project + use the configured inference provider to analyze every file and
    return a list of memory patch proposals - one per file.
    Runs up to _EXTRACT_WORKERS files concurrently.
    """
    root = Path(path_str).expanduser().resolve()
    raw = scan_project(path_str)
    if "error" in raw:
        return raw

    if not config.groq_api_key and not getattr(config, "cerebras_api_key", ""):
        return {**raw, "proposals": [], "error": "groq_api_key or cerebras_api_key required"}

    try:
        from .groq_client import best_complete as _complete
    except ImportError:
        return {**raw, "proposals": [], "error": "groq not installed (pip install groq)"}

    from concurrent.futures import ThreadPoolExecutor, as_completed
    project_name = raw["project_name"]
    files = raw["files"]

    def _extract(item: tuple[str, str]) -> list[dict[str, Any]]:
        rel_path, content = item
        ext = Path(rel_path).suffix.lower()
        # Rich binary formats: convert to text first, then extract
        if ext in SUPPORTED_EXTENSIONS:
            real_path = root / rel_path
            rich_text = _extract_file_text(real_path)
            if not rich_text:
                return []
            header = f"[{ext.lstrip('.')} file: {rel_path}]\n\n"
            return _ai_extract_data_file(config, project_name, rel_path, header + rich_text)
        # Data + plain text + extended code treated as data files (multi-patch extraction)
        if ext in DATA_EXTENSIONS or ext in PLAIN_TEXT_EXTENSIONS or ext in EXTENDED_CODE_EXTENSIONS:
            return _ai_extract_data_file(config, project_name, rel_path, content)
        # Core source files: single-patch architecture extraction
        patch = _ai_extract_file(config, project_name, rel_path, content)
        return [patch] if patch else []

    total_files = len(files)
    proposals: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=_EXTRACT_WORKERS) as pool:
        futures = {pool.submit(_extract, item): item[0] for item in files.items()}
        done = 0
        for future in as_completed(futures):
            patches = future.result()
            done += 1
            rel = futures[future]
            print(f"[Synapse] chunk {done}/{total_files} -> {len(patches)} patches: {rel}", flush=True)
            proposals.extend(patches)

    # Build project slug (mirrors path_to_node_id logic)
    project_slug = re.sub(r"[_\s]+", "-", project_name).lower()
    project_slug = re.sub(r"[^a-z0-9-]", "", project_slug).strip("-")
    project_key = f"projects.{project_slug}"

    # Extract structural code graph (AST-based, no API cost)
    code_graph = extract_code_graph(files)

    # Persist graph so diff.py can use structural edges for relinking
    if config.vault_path:
        graph_dir = config.vault_path / "projects" / project_slug
        graph_dir.mkdir(parents=True, exist_ok=True)
        (graph_dir / "_graph.json").write_text(
            json.dumps(code_graph, indent=2), encoding="utf-8"
        )

    # Remove vault nodes that no longer exist in the current graph
    from .diff import cleanup_stale_nodes
    cleanup_result = cleanup_stale_nodes(config, project_slug, code_graph)

    # Generate function/class proposals — parallel batches, same worker pool as file calls
    fn_nodes = [n for n in code_graph.get("nodes", []) if n.get("type") in ("function", "class")]
    fn_descriptions: dict[str, str] = {}
    batches = [fn_nodes[i: i + _FN_BATCH_SIZE] for i in range(0, len(fn_nodes), _FN_BATCH_SIZE)]

    def _describe_batch(batch: list[dict[str, Any]]) -> dict[str, str]:
        return _ai_describe_functions(config, project_name, batch)

    total_batches = len(batches)
    with ThreadPoolExecutor(max_workers=_EXTRACT_WORKERS) as pool:
        for i, result in enumerate(pool.map(_describe_batch, batches), 1):
            fn_descriptions.update(result)
            print(f"[Synapse] fn-batch {i}/{total_batches} done ({len(result)} descriptions)", flush=True)

    fn_proposals = _make_function_proposals(project_key, code_graph, fn_descriptions)
    proposals.extend(fn_proposals)

    return {
        "project_name": project_name,
        "root": raw["root"],
        "detected": raw["detected"],
        "file_count": len(files),
        "function_nodes": len(fn_proposals),
        "stale_removed": cleanup_result["count"],
        "proposals": proposals,
    }


_FN_BATCH_PROMPT = """\
You are a code analyst. For each function below, write a description that names SPECIFIC details — actual values, actual calls, actual side effects. Never paraphrase the function name.

Rules:
- Name what it CALLS (other functions, APIs, libraries)
- Name what it RETURNS or WRITES (type, shape, where it goes)
- Name any CONSTANTS, PORTS, PATHS, or CONFIG values it uses
- If it has side effects (writes file, mutates state, sends request), say so explicitly
- 1-2 sentences MAX. Dense, specific, no fluff.

BAD: "Initializes the bridge process to facilitate communication."
GOOD: "Spawns loophole-bridge.exe (or server_control.py in dev) as a child process on the port returned by findFreePort, storing the handle in bridgeProcess."

Output a JSON array — nothing else. No markdown fences.
Format: [{"id": "<node_id>", "description": "<specific description>"}]
Output raw JSON only.\
"""

_FN_BATCH_SIZE = 8


def _ai_describe_functions(
    config: "SynapseConfig", project_name: str, nodes: list[dict[str, Any]]
) -> dict[str, str]:
    """Inference call for function/class descriptions. Returns {node_id: description}."""
    from .groq_client import best_complete
    if not nodes:
        return {}

    fn_lines = "\n".join(
        f"- id: {n['id']}, sig: {n.get('signature') or n.get('label', n['id'])}, "
        f"file: {n.get('file','')}, doc: {n.get('docstring','')[:80]}"
        for n in nodes
    )
    user_msg = f"Project: {project_name}\n\nFunctions:\n{fn_lines}"

    try:
        raw = best_complete(config, _FN_BATCH_PROMPT, user_msg)
    except Exception:
        return {}

    text = re.sub(r"^```[a-z]*\n?", "", raw)
    text = re.sub(r"\n?```$", "", text).strip()
    if not text.startswith("["):
        m = re.search(r"\[[\s\S]*\]", text)
        if not m:
            return {}
        text = m.group()

    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        return {}

    return {
        str(item["id"]): str(item["description"])
        for item in items
        if isinstance(item, dict) and item.get("id") and item.get("description")
    }


def _ai_extract_file_fast(
    config: "SynapseConfig", project_name: str, rel_path: str, content: str
) -> dict[str, Any] | None:
    """Groq extraction for incremental watcher (single-patch)."""
    from .groq_client import best_complete
    ext = Path(rel_path).suffix.lower()
    prompt = _DATA_SYSTEM_PROMPT if ext in DATA_EXTENSIONS else _FILE_SYSTEM_PROMPT
    user_msg = f"Project: {project_name}\nFile: {rel_path}\n\n```\n{content}\n```"
    try:
        raw = best_complete(config, prompt, user_msg)
        return _parse_patch(raw) if raw else None
    except Exception:
        return None


def _ai_extract_data_file(config: "SynapseConfig", project_name: str, rel_path: str, content: str) -> list[dict[str, Any]]:
    """Call Groq on a data/personal file and return a LIST of patches (one per topic)."""
    from .groq_client import best_complete, parse_json_patches
    user_msg = f"Project: {project_name}\nFile: {rel_path}\n\n```\n{content}\n```"
    try:
        raw = best_complete(config, _DATA_SYSTEM_PROMPT, user_msg)
        return parse_json_patches(raw)
    except Exception:
        return []


def _ai_extract_file(config: "SynapseConfig", project_name: str, rel_path: str, content: str) -> dict[str, Any] | None:
    from .groq_client import best_complete
    ext = Path(rel_path).suffix.lower()
    prompt = _DATA_SYSTEM_PROMPT if ext in DATA_EXTENSIONS else _FILE_SYSTEM_PROMPT
    user_msg = f"Project: {project_name}\nFile: {rel_path}\n\n```\n{content}\n```"
    try:
        raw = best_complete(config, prompt, user_msg)
        return _parse_patch(raw) if raw else None
    except Exception:
        return None


def _parse_patch(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    # Strip markdown fences if model ignored instructions
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = text.strip()

    # If model echoed the primed '{"key":' prefix before returning full JSON, fix it
    if not text.startswith("{"):
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        text = match.group()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return None

    if not isinstance(data, dict):
        return None
    key = str(data.get("key", "")).strip()
    content = str(data.get("content", "")).strip()
    if not key or not content:
        return None

    return {
        "key": key,
        "content": content,
        "type": str(data.get("type", "code")),
        "scope": str(data.get("scope", "global")),
        "weight": float(data.get("weight", 0.8)),
        "signal": str(data.get("signal", "high_signal")),
        "reason": str(data.get("reason", "File extracted from project scan.")),
    }


def _build_tree(root: Path, depth: int = 0, max_depth: int = 3) -> list[dict[str, Any]]:
    if depth >= max_depth:
        return []
    entries = []
    try:
        items = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return []
    for item in items:
        if item.name.startswith(".") or item.name in SKIP_DIRS:
            continue
        if item.is_dir():
            children = _build_tree(item, depth + 1, max_depth)
            entries.append({"name": item.name, "type": "dir", "children": children})
        elif item.suffix.lower() not in SKIP_EXTENSIONS:
            entries.append({"name": item.name, "type": "file"})
    return entries


def _detect_project(root: Path) -> dict[str, Any]:
    detected: dict[str, Any] = {"type": "unknown", "languages": [], "frameworks": []}
    ext_counts: dict[str, int] = {}
    for f in root.rglob("*"):
        if f.is_file() and not _in_skip_dir(f, root):
            ext = f.suffix.lower()
            if ext in SOURCE_EXTENSIONS:
                ext_counts[ext] = ext_counts.get(ext, 0) + 1

    if ext_counts:
        detected["languages"] = sorted(ext_counts, key=lambda e: -ext_counts[e])

    if (root / "package.json").exists():
        detected["frameworks"].append("node")
        pkg = _read_capped(root / "package.json", 8000) or ""
        if '"next"' in pkg:
            detected["frameworks"].append("nextjs")
        if '"electron"' in pkg:
            detected["frameworks"].append("electron")
        if '"react"' in pkg:
            detected["frameworks"].append("react")
        if '"vite"' in pkg:
            detected["frameworks"].append("vite")
        if '"firebase"' in pkg:
            detected["frameworks"].append("firebase")

    if (root / "requirements.txt").exists() or any(root.glob("*.py")):
        detected["frameworks"].append("python")
    if (root / "pyproject.toml").exists():
        detected["frameworks"].append("python")
    if (root / "Cargo.toml").exists():
        detected["frameworks"].append("rust")

    if detected["frameworks"]:
        detected["type"] = detected["frameworks"][0]

    return detected


def _walk_priority(root: Path):
    seen = set()
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if _in_skip_dir(f, root):
            continue
        rel = str(f.relative_to(root)).replace("\\", "/")
        name = f.name
        depth = len(f.relative_to(root).parts)
        is_priority = (
            name in PRIORITY_NAMES
            or any(p.match(rel) for p in PRIORITY_PATTERNS)
            or (depth <= 2 and name in PRIORITY_NAMES)
        )
        if is_priority and rel not in seen:
            seen.add(rel)
            yield f


def _walk_source(root: Path):
    candidates = []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if _in_skip_dir(f, root):
            continue
        ext = f.suffix.lower()
        if ext not in SOURCE_EXTENSIONS and ext not in DATA_EXTENSIONS and ext not in SUPPORTED_EXTENSIONS and ext not in EXTENDED_CODE_EXTENSIONS and ext not in PLAIN_TEXT_EXTENSIONS:
            continue
        depth = len(f.relative_to(root).parts)
        size = f.stat().st_size
        # Prefer source files over data files; within each group sort by depth+size
        type_score = 0 if ext in SOURCE_EXTENSIONS else 1
        score = type_score * 10_000_000 + depth * 10000 + size
        candidates.append((score, f))
    candidates.sort(key=lambda x: x[0])
    for _, f in candidates:
        yield f


def _read_capped(path: Path, limit: int) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return text[:limit]
    except Exception:
        return None


def _in_skip_dir(path: Path, root: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.relative_to(root).parts)


def _is_vague(description: str, signature: str) -> bool:
    """True if description contains no specific technical markers."""
    # Extract function name from signature to exclude it from specificity check
    fn_name = re.sub(r"[^a-zA-Z0-9]", "", signature.split("(")[0].split()[-1]).lower()

    # Specific markers: backtick expressions, camelCase, UPPER_CASE, numbers, paths
    has_backtick = "`" in description
    has_camel = bool(re.search(r"[a-z][A-Z]", description))
    has_constant = bool(re.search(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", description))
    has_number = bool(re.search(r"\b\d+\b", description))
    has_path = bool(re.search(r"[\./\\][a-zA-Z]", description))

    if has_backtick or has_camel or has_constant or has_number or has_path:
        return False

    # Check if it's just paraphrasing the function name
    words = re.findall(r"[a-z]+", description.lower())
    fn_parts = set(re.findall(r"[a-z]+", fn_name))
    overlap = sum(1 for w in words if w in fn_parts and len(w) > 3)
    if len(fn_parts) > 0 and overlap / len(fn_parts) >= 0.6:
        return True

    # Short descriptions with no specifics are vague
    return len(description.strip()) < 60


def _make_function_proposals(
    project_key: str, graph: dict[str, Any], descriptions: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """Auto-generate vault proposals for function/class nodes extracted by the code graph."""
    proposals: list[dict[str, Any]] = []

    # Index edges by source for fast lookup
    edges_from: dict[str, list[dict[str, Any]]] = {}
    for edge in graph.get("edges", []):
        edges_from.setdefault(edge["source"], []).append(edge)

    for node in graph.get("nodes", []):
        ntype = node.get("type")
        if ntype not in ("function", "class"):
            continue

        file_id = node.get("parent", "")
        node_id = node["id"]

        # fn_slug = node_id minus the file_id prefix
        if node_id.startswith(file_id + "-"):
            fn_slug = node_id[len(file_id) + 1:]
        else:
            fn_slug = node_id

        # vault key: projects.myapp.server-control.ddg-search
        # dots are the only hierarchy separators; hyphens stay within each segment
        vault_key = f"{project_key}.{file_id}.{fn_slug}"

        # Related: parent file + any call targets
        parent_key = f"{project_key}.{file_id}"
        related: list[str] = [parent_key]
        for edge in edges_from.get(node_id, []):
            if edge.get("relation") == "calls":
                tgt = edge["target"]
                related.append(f"{project_key}.{tgt}")
        related = list(dict.fromkeys(related))[:5]  # deduplicate, keep order

        sig = node.get("signature") or node.get("label", node_id)
        doc = node.get("docstring", "").strip()
        lineno = node.get("lineno", "?")
        rel_file = node.get("file", "")
        ai_desc = (descriptions or {}).get(node_id, "")

        lines = [f"**{sig}**"]
        if ai_desc:
            flagged = _is_vague(ai_desc, sig)
            desc_line = f"\n[LOW DETAIL - check source] {ai_desc}" if flagged else f"\n{ai_desc}"
            lines.append(desc_line)
        elif doc:
            lines.append(f"\n{doc}")
        lines.append(f"\nDefined in `{rel_file}` at line {lineno}.")

        proposals.append({
            "key": vault_key,
            "content": "\n".join(lines),
            "type": "code",
            "scope": "global",
            "weight": 0.6,
            "signal": "high_signal",
            "reason": f"{ntype} node extracted by AST from {rel_file}",
            "related": related,
        })

    return proposals
