# Contributing to Synapse

## Setup

```bash
git clone https://github.com/Santhosh-Stalin/Synapse.git
cd Synapse
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pip install black pytest
```

## Running tests

```bash
pytest Diagnostics/test_core.py -v
```

All 37 tests run without a Gemini API key. No vault, no network.

## Code style

Formatted with [black](https://github.com/psf/black) at line length 100.

```bash
black server/ Diagnostics/ pipeline/ setup.py run_server.py --line-length 100
```

## Adding a new MCP tool

1. Add the function to `server/functions.py`
2. Register it in `server/main.py` with `@app.tool(name="memory_<name>")`
3. Add it to the tool table in `AGENT.md` so Claude knows when to use it
4. Add it to the tool table in `README.md`
5. Write at least one test in `Diagnostics/test_core.py`

## Adding a new import provider

1. Add detection logic in `server/ai_importer.py` → `_detect_provider()`
2. Write a `_preprocess_<provider>()` function that returns `{label: text}` chunks
3. Wire it into the dispatch block at the bottom of `import_ai_export()`
4. Test with a real export from that provider

## What not to touch

- `vault/` — personal memory data, gitignored, never committed
- `config.yaml` → `gemini_api_key` — use `.env` instead
- `_pending.json`, `_index.db` — runtime files, not source

## Submitting changes

Open a pull request against `master`. CI runs the test suite automatically on every push.
