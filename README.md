# Synapse

A persistent memory vault for Claude, powered by Gemma.

Synapse is a local MCP server that gives Claude structured, searchable memory stored as Markdown files. It indexes 1997+ past conversations into a topic graph, supports tiered retrieval (581–9,000 tokens), and keeps every write behind a diff-review gate.

### Provider Overview

Synapse has two distinct parts with different provider requirements:

| Part | What it does | Providers used | Cost |
|------|-------------|----------------|------|
| **MCP Server** | Daily memory reads/writes, search, code indexing | Gemini API only | Free tier (15 RPM) |
| **Import Pipeline** | One-time bulk import of thousands of past conversations | Gemma (primary) · OpenRouter · Groq · Cerebras | All free tiers |

**Only the Gemini API key is required** to use Synapse day-to-day. The import pipeline keys (OpenRouter, Groq, Cerebras) are only needed if you want to bulk-import your full ChatGPT/Claude conversation history — and all of them have generous free tiers.

---

## Table of Contents

1. [What You Need](#1-what-you-need)
2. [Installation](#2-installation)
3. [Getting a Gemini API Key](#3-getting-a-gemini-api-key)
4. [Adding Your API Key](#4-adding-your-api-key)
5. [Connecting to Claude](#5-connecting-to-claude)
6. [Verifying the Install](#6-verifying-the-install)
7. [How Claude Should Use Synapse](#7-how-claude-should-use-synapse)
8. [All MCP Tools — In Depth](#8-all-mcp-tools--in-depth)
9. [The Write Pipeline](#9-the-write-pipeline)
10. [Importing Existing Memory](#10-importing-existing-memory)
11. [Scanning a Code Project](#11-scanning-a-code-project)
12. [The Incremental Watcher](#12-the-incremental-watcher)
13. [Vault Structure](#13-vault-structure)
14. [Configuration Reference](#14-configuration-reference)
15. [Obsidian Integration](#15-obsidian-integration)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. What You Need

- **Python 3.10+** — `python --version`
- **Claude Desktop** — [claude.ai/download](https://claude.ai/download)
- **A Gemini API key** — required for everything (free tier is enough, see Step 3)
- **Git** *(optional but recommended)* — auto-commits every memory write

### API Key Summary

| Key | Where to get | Required for |
|-----|-------------|--------------|
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | MCP server, summarizer, search — everything |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) | `Diagnostics/groq_blacklist.py` + `Diagnostics/Triage.py` only |
| `OPENROUTER_API_KEY` | [openrouter.ai/keys](https://openrouter.ai/keys) | `Diagnostics/Triage.py` only |

**Groq and OpenRouter are only needed if you are running the full ChatGPT export filtering pipeline.** Both have free tiers. If you just want to use Synapse as a memory server, only the Gemini key is needed.

---

## 2. Installation

```powershell
# Windows
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 3. Getting a Gemini API Key

1. Go to **https://aistudio.google.com/apikey**
2. Sign in with Google
3. Click **Create API Key**
4. Copy the key — starts with `AIza...`

The free tier allows 15 requests/minute, which is enough for all Synapse features.

---

## 4. Adding Your API Key

```bash
python setup.py
```

This saves your key to `.env` and writes the Claude Desktop MCP config automatically.

**Manual `.env` option:**
```
GEMINI_API_KEY=AIzaYourKeyHere
```

**Priority order:** environment variable → `.env` file → `config.yaml`

---

## 5. Connecting to Claude

If you ran `setup.py`, restart Claude Desktop — Synapse will appear in the tools list automatically.

**Manual MCP config location:**

| OS | Path |
|----|------|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "synapse": {
      "command": "C:\\path\\to\\Synapse\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\Synapse\\run_server.py"]
    }
  }
}
```

---

## 6. Verifying the Install

```bash
pytest tests/
```

Expected: `26 passed`. No API key needed for tests.

---

## 7. How Claude Should Use Synapse

Synapse uses tiered retrieval — Claude picks the depth that matches the question. Never load more than needed.

### Tier 1 — Every conversation (~581 tokens)

```
memory_context()
```

Always first. Returns `identity.profile` + `identity.communication` + `identity.location` + vault health. Sufficient for general questions and advice.

### Tier 2 — Project or topic questions (~2,000 tokens)

```
memory_context()  →  memory_search("query")  →  memory_get("key")
```

Use when the question references a specific project, skill, or preference. Run `memory_search` first — if the active vault has the answer, stop here.

### Tier 3 — "What did we discuss before?" (~9,000 tokens)

```
memory_context()  →  memory_deep_search("query")  →  memory_get_raw_chunks(chat_id, query)
```

Use when the user asks about a past conversation, or the vault is thin on a topic. `memory_deep_search` traverses a topic graph over 1997 chat summaries. `memory_get_raw_chunks` returns only the relevant message windows (~1–7k tokens instead of a full 35k conversation).

**Never call `memory_get_raw()` unless the user explicitly asks for the complete history of a chat.**

### Decision tree

```
Simple / general question?
  → Tier 1 only

Asks about a specific project, skill, or preference?
  → Tier 2 (check active vault first)
  → If vault is thin → Tier 3

Asks "what did we discuss / what was the code / what did we decide"?
  → Tier 3 directly
```

### Token cost reference

| Call | Tokens |
|------|--------|
| `memory_context()` | ~581 |
| `memory_search()` (4 results) | ~200–900 |
| `memory_get()` (single file) | ~200–500 |
| `memory_deep_search()` (8 results) | ~1,422 |
| `memory_get_raw_chunks()` (3 chunks) | ~1,000–7,149 |
| `memory_get_raw()` (full conversation) | ~5,000–35,000 |
| **Full Tier 3 chain** | **~9,000** |

**Hard ceiling: never exceed 15,000 tokens on memory retrieval alone.**

### What to never do

- **Never call `memory_tree`** — use `memory_list(folder)` instead
- **Never guess a key** — search first, then get
- **Never write without proposing first** — the diff review exists for a reason
- **Never call `memory_get_raw()`** when you have a query — use `memory_get_raw_chunks()`

---

## 8. All MCP Tools — In Depth

---

### `memory_context()`

Returns `identity.profile` + `identity.communication` + `identity.location` in one call. Call this first, every conversation.

---

### `memory_list(folder)`

Lists all memory keys in one vault folder. Use instead of `memory_tree`.

```python
memory_list("projects")   # ~82 tokens
memory_list("work")
memory_list("")           # root — all 5 top-level folders
```

---

### `memory_search(query)`

Hybrid FTS5 + semantic search over active vault files. Returns up to 4 results. High-scoring results include full content — you may not need `memory_get` afterward.

```python
memory_search("CTF pwn exploitation")
memory_search("Bali travel itinerary")
```

---

### `memory_get(key)`

Returns full frontmatter + content of one memory file.

```python
memory_get("work.cybersecurity")
memory_get("projects.atlas_ai")
```

Key format: `category.slug` → `vault/category/slug.md`

---

### `memory_deep_search(query)`

FTS5 search over 1997 chat summaries, then BFS traversal of the topic graph to surface related conversations. Returns 8 ranked results with summaries.

Use this when the active vault doesn't have the detail you need, or when the user asks about a past discussion.

```python
memory_deep_search("geolocation script RAT")
memory_deep_search("buffer overflow pwntools CTF")
```

---

### `memory_get_raw_chunks(chat_id, query)`

Extracts only the query-relevant message windows from a raw conversation. Typical cost: 1,000–7,000 tokens instead of 35,000 for the full conversation.

```python
memory_get_raw_chunks("abc123-uuid", "geolocation tracking")
```

Use the `conversation_id` from `memory_deep_search` results.

---

### `memory_get_raw(chat_id)`

Returns the full raw conversation. **Can be 35,000+ tokens.** Only use when the user explicitly asks for the complete history of a specific chat.

---

### `memory_search_raw(title_query)`

Fast title-only search over the raw archive index. Use when you know the conversation name.

```python
memory_search_raw("Synapse graph builder")
```

---

### `memory_build_graph()`

Builds or rebuilds the topic graph over `vault/chats/*.md`. Scores edges by project/tag/keyword overlap. Run after importing new chat summaries.

Result: a weighted graph JSON at `vault/metadata/topic_graph.json` + `## Related Conversations` wikilinks injected into each chat summary.

---

### `memory_propose_update(patch)`

Creates a pending patch with a unified diff preview. Does not write anything until `memory_apply_update` is called.

```json
{
  "key": "projects.synapse",
  "content": "## Project: Synapse\n- Stack: Python, SQLite FTS5, Gemini API\n- Status: active",
  "type": "note",
  "scope": "global",
  "weight": 0.9,
  "reason": "Core project details"
}
```

**Field guide:**

| Field | Required | Notes |
|-------|----------|-------|
| `key` | Yes | `category.slug` |
| `content` | Yes | Markdown, 150–400 words, `##` headers + bullets |
| `type` | No | `note` or `code` |
| `scope` | No | `global` or `project` |
| `weight` | No | 0.0–1.0 (0.9 = high importance) |
| `reason` | No | Shown in diff — be specific |

**Categories:** `identity.*` · `life.*` · `projects.*` · `patterns.*` · `work.*`

---

### `memory_diff()`

Lists all patches currently waiting for approval. Always call before applying anything.

---

### `memory_apply_update(patch_id)`

Writes the approved patch to disk, updates FTS5 index, re-embeds the file, refreshes wikilinks, commits to git.

---

### `memory_reject_update(patch_id, reason)`

Discards a pending patch and logs the rejection to `_rejections.jsonl`.

---

### `memory_conflicts()`

Pairwise scan for real contradictions across vault files (age, location, skill level, tech stack). Run after any bulk import.

---

### `memory_smart_merge(dry_run, threshold)`

Finds semantic duplicate files using embedding similarity, then merges each pair with Gemma.

```python
memory_smart_merge(dry_run=True)    # preview
memory_smart_merge(dry_run=False)   # execute
```

---

### `memory_deduplicate(auto_clean)`

Word-overlap duplicate detection + stray file + thin stub detection. Run before `memory_smart_merge`.

- `auto_clean=False` — report only (default)
- `auto_clean=True` — auto-delete strays and thin stubs

---

### `memory_import_ai_export(path)`

Imports a Claude.ai or ChatGPT data export and extracts memory patches using Gemma.

**Full workflow:**
```
memory_import_ai_export("/path/to/export/")
memory_diff()
memory_conflicts()
memory_smart_merge(dry_run=True)
memory_smart_merge(dry_run=False)
memory_relink_all()
```

---

### `memory_import_synapse_summaries(path)`

Imports pre-processed Synapse summary JSON files directly into `vault/chats/`. No LLM needed — use this instead of `memory_import_ai_export` when you already have Synapse-format summaries.

---

### `memory_ingest_text(text, label)`

Extracts memory patches from any pasted text using Gemma. Text over 14,000 characters is split and processed in parallel.

---

### `memory_scan_project(path)`

Scans a **code project** directory. Reads source files, extracts architecture descriptions, builds AST code graph.

**For code projects only.** Do not use on personal data or AI exports.

---

### `memory_relink_all()`

Recomputes `triggers` and `related` frontmatter for every vault file and injects `[[wikilinks]]`. Run after any bulk operation.

---

### `memory_rebuild_index()`

Drops and rebuilds the SQLite FTS5 index from all vault files. Run if search returns stale results.

---

### `memory_organize()`

Creates `index.md` hub files in every vault folder. Run after initial setup or bulk imports.

---

### `memory_weekly_report()`

Generates `vault/_weekly.md`: writes, pending patches, rejections, conflicts, and health stats.

---

### `memory_start_watcher(path)` / `memory_stop_watcher()` / `memory_watcher_status()`

Watches a project folder and auto-applies patches when source files change (no review step). Only enable on projects with an existing scan.

---

### `memory_tree()` — AVOID

Returns the complete vault directory tree. Costs thousands of tokens and grows linearly. Use `memory_list(folder)` instead.

---

## 9. The Write Pipeline

```
Claude learns something worth remembering
           |
           v
  memory_propose_update(patch)
    - Validates structure
    - Builds unified diff vs current file
    - Saves to _pending.json ONLY
    - Returns diff for review
           |
           v
     Review the diff
           |
        approve?
       /         \
     yes           no
      |             |
      v             v
  memory_apply_  memory_reject_
  update         update(reason)
      |               |
      v               v
  File written    Logged to
  Index updated   _rejections.jsonl
  Git committed   Removed from queue
```

Claude cannot silently overwrite your memories. Every write goes through this flow.

---

## 10. Importing Existing Memory

### From Claude.ai or ChatGPT

```
1. Download your data export
2. memory_import_ai_export("/path/to/export/")
3. memory_diff()                      — review proposals
4. Apply good ones, reject bad ones
5. memory_conflicts()                 — fix contradictions
6. memory_smart_merge(dry_run=True)   — find near-duplicates
7. memory_smart_merge(dry_run=False)  — merge them
8. memory_relink_all()                — rebuild wikilinks
9. memory_build_graph()               — rebuild topic graph over chats
```

### From raw notes

```
1. memory_ingest_text(text="...", label="source name")
2. memory_diff()
3. Apply / reject
```

---

## 11. Scanning a Code Project

```
1. memory_scan_project("/path/to/project")

2. memory_diff()
   Apply file-level memories (weight 0.8)
   Skip trivial function nodes

3. memory_organize()
4. memory_relink_all()
```

---

## 12. The Incremental Watcher

- Polls every 2 seconds, debounces 4 seconds after last write
- Fast Gemma mode — typically 20–40s per file
- Auto-applies without review step
- Retries on 429 with exponential backoff
- Skips: `node_modules`, `__pycache__`, `.venv`, `dist`, `build`, `.git`, `.next`

---

## 13. Vault Structure

```
vault/
  _pending.json          — patches waiting for approval
  _index.db              — SQLite FTS5 + embedding vectors
  _weekly.md             — weekly report
  _rejections.jsonl      — rejected patch log

  identity/              — who you are: profile, education, communication style
  life/                  — hobbies, travel, fitness, creative interests
  projects/              — every project: stack, status, key details
  patterns/              — recurring skills, workflows, learning approaches
  work/                  — career, tools, dev environment, domain expertise

  chats/                 — 1997+ summarised past conversations (searchable archive)
  metadata/
    topic_graph.json     — weighted graph linking chats by shared topics/projects
```

**Memory file format:**

```markdown
---
key: work.cybersecurity
type: note
scope: global
weight: 0.9
confidence: proposed
last_updated: 2026-05-02
triggers: [ctf, exploitation, pwn, buffer-overflow]
related: [work.python, projects.ctf_entrypoint]
---

## Technical Proficiency
- **CTF:** Competed in ENTRYPOINT CTF 2026. Solved Web, Crypto, Pwn, Reversing.
- **Binary Exploitation:** 64-bit buffer overflows, ret2win, ROP chains via pwntools.

Related: [[work/python]] | [[projects/ctf_entrypoint]]
```

---

## 14. Configuration Reference

```yaml
# config.yaml

vault_path: ./vault               # where memory files live
git_enabled: true                 # auto-commit every write
encryption: false                 # Fernet at-rest encryption (requires SYNAPSE_FERNET_KEY)
cloud_search: false               # Gemini LLM fallback when FTS5 + semantic return nothing
weekly_report_day: monday
pending_auto_expire_days: 7
gemini_api_key: ""                # prefer .env or environment variable
raw_archive_path: ./synapse_extracted   # path to raw conversation archive (optional)
```

---

## 15. Obsidian Integration

1. Open Obsidian → "Open folder as vault" → select `Synapse/vault/`
2. `memory_organize()` — creates hub index files in every folder
3. `memory_relink_all()` — populates all `[[wikilinks]]`

Graph view shows hub-and-spoke structure: each folder has a central index node, memory files as leaves, cross-links connecting related topics.

---

## 16. Troubleshooting

**Synapse tools don't appear in Claude Desktop**
- Restart Claude Desktop after editing MCP config
- Validate JSON syntax (no trailing commas)
- Python path must point to `.venv` Python, not system Python

**`gemini_api_key required`**
```bash
python setup.py
# or:
echo GEMINI_API_KEY=AIzaYourKey > .env
```

**Scan is slow / 429 errors**
Free tier: 15 RPM. Synapse uses 4 workers with auto-retry. A 20-file project takes 5–15 minutes.

**Search returns nothing**
```
memory_rebuild_index()
```

**`memory_deep_search` returns no results**
```
memory_build_graph()   # rebuilds topic graph over vault/chats
memory_rebuild_index() # rebuilds FTS5 index
```

**Conflicts detected after import**
Run `memory_conflicts()`. The newer file is almost always correct — reject the older patch or update the stale file.

**Obsidian graph is sparse**
```
memory_organize()
memory_relink_all()
```

**`pytest tests/` fails**
Activate `.venv` first. Run from the Synapse root directory.
