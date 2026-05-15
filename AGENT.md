# Synapse — MCP Memory Server

Synapse is a persistent, structured memory system for Claude. It stores memories as Markdown files in a local vault, indexed by SQLite FTS5 and a topic graph over 1997 chat summaries, and exposes them via MCP tools.

---

## FIRST THING EVERY CONVERSATION

**Call `memory_context()` immediately** — returns `identity.profile` + `identity.communication` + `identity.location` + live dedup health check in one call. (~581 tokens). If `_vault_health.clean` is False, flag issues and offer to run `memory_deduplicate(auto_clean=True)`. Do this before any other tool call, every time.

---

## TIERED RETRIEVAL — pick the right depth

Do not blindly run all tools. Match retrieval depth to what the query actually needs.

### Tier 1 — Every conversation (always, ~581 tokens)
```
memory_context()
```
Covers identity, communication style, location, active project index. Sufficient for general questions, advice, and anything that doesn't reference past work.

### Tier 2 — Project or topic questions (~2,000 tokens total)
```
memory_context()  +  memory_search("query")  +  memory_get("key")
```
Use when the question is about a specific project, skill, or preference that might be in the active vault. Run `memory_search` first — if the active vault has the answer, stop here. Do not escalate to Tier 3 unless the vault is insufficient.

### Tier 3 — "What did we work on / discuss before?" (~9,000 tokens total)
```
memory_context()  +  memory_deep_search("query")  +  memory_get_raw_chunks(chat_id, query)
```
Use only when:
- The user explicitly asks about a past conversation
- The active vault doesn't have enough detail on a project
- You need to recover specific decisions, code, or reasoning from prior sessions

`memory_deep_search` returns ranked chat summaries via FTS5 + graph traversal. Pick the most relevant chat_id, then call `memory_get_raw_chunks` with the same query to get the relevant message windows (~1–7k tokens instead of the full ~35k).

**Never call `memory_get_raw()` (full conversation) unless the user explicitly asks for the complete history of a chat.** It can cost 35k+ tokens.

### Decision tree
```
Is the question simple / general?
  → Tier 1 only

Does it reference a specific project, skill, or preference?
  → Tier 2 (check active vault first)
  → If vault is thin on that topic → Tier 3

Does it ask "what did we discuss / what was the code / what did we decide"?
  → Tier 3 directly
```

---

## Vault structure

```
vault/
  identity/   — who Santhosh is: profile, education, philosophy, interaction style
  life/        — hobbies, fitness, travel, photography, cars, Blender
  projects/    — every project: stack, status, key technical details
  patterns/    — recurring skills: CTF techniques, security tools, prompting habits
  work/        — dev environment, tools, accounts, stack
  chats/       — 1997 summarised past conversations (passive archive, searchable)
  metadata/    — topic_graph.json linking chats by shared topics/projects
```

Each active vault file has YAML frontmatter (`key`, `type`, `triggers`, `related`) and Markdown content. Chat files have `tags`, `categories`, `related` (graph links), and sections: Deep Summary, Key Facts, Decisions, Memory Candidates.

---

## Tools — when to use each

### Conversation start (always)
| Tool | Tokens | When |
|---|---|---|
| `memory_context()` | ~581 | **First call every conversation.** Identity + active project index. |

### Active vault lookup (Tier 2)
| Tool | Tokens | When |
|---|---|---|
| `memory_search("query")` | ~200–900 | Find relevant active vault keys by content. Returns top 4. |
| `memory_get("some.key")` | ~200–500 | Fetch full content of a specific key. |
| `memory_list("projects")` | ~50–200 | List all keys in a folder. Use instead of memory_tree. |

**Pattern: context() → search() → get()**

### Chat archive lookup (Tier 3)
| Tool | Tokens | When |
|---|---|---|
| `memory_deep_search("query")` | ~1,000–2,000 | FTS5 + graph traversal over 1997 chat summaries. Returns 8 ranked results. |
| `memory_get_raw_chunks(id, query)` | ~1,000–7,000 | Relevant message windows from a raw conversation. Use this, not memory_get_raw. |
| `memory_get_raw(id)` | ~5,000–35,000 | Full raw conversation. Only when complete history is explicitly needed. |
| `memory_search_raw("title")` | ~200 | Fast title-only search over raw archive index. Use when you know the conversation name. |

### Writing memories
| Tool | When |
|---|---|
| `memory_propose_update(patch)` | Propose a new or updated memory. Returns a diff for review. |
| `memory_apply_update(patch_id)` | Approve and write a proposed patch. |
| `memory_reject_update(patch_id)` | Discard a proposed patch. |
| `memory_diff()` | List all pending patches awaiting approval. |

### Maintenance (run after bulk imports or scans)
| Tool | When |
|---|---|
| `memory_scan_project("path")` | Scan a code project — Gemma extracts memories. Do NOT use on AI exports. |
| `memory_import_ai_export("path")` | Import Claude.ai or ChatGPT data export. |
| `memory_import_synapse_summaries("path")` | Import synapse_ai_summaries JSON folder directly. No LLM needed. |
| `memory_ingest_text(text)` | Paste raw text — Gemma extracts patches from it. |
| `memory_build_graph()` | Build/rebuild topic graph over vault/chats. Run after new imports. |
| `memory_relink_all()` | Recompute triggers + related links for every vault file. Run after bulk imports. |
| `memory_rebuild_index()` | Rebuild the SQLite FTS5 index from scratch. Run if search feels stale. |
| `memory_deduplicate()` | Report stray files, thin stubs, and duplicate pairs. |
| `memory_smart_merge()` | Find and merge semantic duplicates using embedding similarity + Gemma. |
| `memory_organize()` | Rebuild MOC index files for every folder. |

### Conflict detection
| Tool | When |
|---|---|
| `memory_conflicts()` | Find contradictions between memory files. |

### File watcher
| Tool | When |
|---|---|
| `memory_start_watcher("path")` | Watch a project directory — auto-extracts changed files with Gemma. |
| `memory_stop_watcher()` | Stop the running watcher. |
| `memory_watcher_status()` | Check watcher state. |

### Reports
| Tool | When |
|---|---|
| `memory_weekly_report()` | Generate the weekly Synapse activity report. |
| `memory_tree()` | **NEVER use** — ~20k tokens. Use `memory_list` instead. |

---

## Token cost reference (live system)

| Call | Measured tokens |
|---|---|
| `memory_context()` | ~581 |
| `memory_deep_search()` (8 results) | ~1,422 |
| `memory_get_raw_chunks()` (3 chunks) | ~1,000–7,149 |
| `memory_search()` (4 results) | ~200–900 |
| `memory_get()` (single file) | ~200–500 |
| **Full Tier 3 chain** | **~9,152** |
| `memory_get_raw()` (full convo) | ~5,000–35,000 |

**Target budgets:**
- Tier 1 only: ~600 tokens
- Tier 2 session: ~2,000 tokens
- Tier 3 deep session: ~9,000 tokens
- Never exceed 15,000 tokens on memory retrieval alone

---

## Writing a patch

```json
{
  "key": "projects.atlas",
  "content": "## Project: Atlas\n- Stack: React/Vite, Firebase, Gemini, ElevenLabs",
  "type": "note",
  "scope": "global",
  "weight": 0.8,
  "reason": "Core project details"
}
```

Categories: `identity.*` · `life.*` · `projects.*` · `patterns.*` · `work.*`
