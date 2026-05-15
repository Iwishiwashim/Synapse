"""
Token reduction benchmark for Synapse.

Measures actual token cost of Synapse retrieval vs the naive baseline
(pasting raw vault files directly into Claude context). No LLM calls needed.

Run from project root:
    python Diagnostics/benchmark_tokens.py

Output: a table showing Synapse tokens, baseline tokens, and reduction % per
query tier, plus an aggregate. Writes results to Diagnostics/benchmark_results.json
so you can track changes over time.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import load_config
from server.functions import _estimate_tokens, memory_auto, memory_context

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_tokens(path: Path) -> int:
    try:
        return max(1, len(path.read_text(encoding="utf-8", errors="ignore")) // 4)
    except Exception:
        return 0


def _folder_tokens(vault: Path, *folders: str) -> int:
    total = 0
    for folder in folders:
        d = vault / folder
        if not d.exists():
            continue
        for f in d.rglob("*.md"):
            if not f.name.startswith("_"):
                total += _file_tokens(f)
    return total


def _vault_tokens(vault: Path) -> int:
    return _folder_tokens(vault, *[d.name for d in vault.iterdir() if d.is_dir()])


# ---------------------------------------------------------------------------
# Benchmark queries — one per retrieval tier
# ---------------------------------------------------------------------------

QUERIES = [
    {
        "label": "Who is this person",
        "query": "identity profile background",
        "tier": 1,
        "baseline_folders": ("identity",),
    },
    {
        "label": "Tech stack and tools",
        "query": "programming languages frameworks dev tools",
        "tier": 2,
        "baseline_folders": ("work", "patterns"),
    },
    {
        "label": "Active projects",
        "query": "current projects status goals",
        "tier": 2,
        "baseline_folders": ("projects",),
    },
    {
        "label": "Coding patterns and habits",
        "query": "coding habits workflow preferences",
        "tier": 2,
        "baseline_folders": ("work", "patterns", "projects"),
    },
    {
        "label": "Past conversation (deep)",
        "query": "past session debugging problem solved",
        "tier": 3,
        "baseline_folders": ("chats", "projects", "work"),
    },
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_benchmark(verbose: bool = True) -> dict[str, Any]:
    config = load_config()
    vault = config.vault_path

    if verbose:
        print("Synapse Token Reduction Benchmark")
        print("=" * 68)
        print(f"Vault : {vault}")
        print(f"Run   : {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print()

    # ── Vault baseline ──────────────────────────────────────────────────────
    full_vault_tokens = _vault_tokens(vault)
    folder_tokens: dict[str, int] = {}
    for d in sorted(vault.iterdir()):
        if d.is_dir() and not d.name.startswith("_"):
            t = _folder_tokens(vault, d.name)
            if t:
                folder_tokens[d.name] = t

    if verbose:
        print("Vault baseline (raw paste cost per folder):")
        for folder, toks in folder_tokens.items():
            print(f"  vault/{folder:<16} {toks:>8,} tokens")
        print(f"  {'TOTAL':<20} {full_vault_tokens:>8,} tokens")
        print()

    # ── memory_context standalone ───────────────────────────────────────────
    try:
        t0 = time.perf_counter()
        ctx = memory_context(config)
        ctx_ms = int((time.perf_counter() - t0) * 1000)
        ctx_tokens = ctx.get("_tokens", _estimate_tokens(ctx))
        identity_baseline = _folder_tokens(vault, "identity")
    except Exception as e:
        ctx_tokens, ctx_ms, identity_baseline = 0, 0, 0
        if verbose:
            print(f"  memory_context error: {e}")

    if verbose and ctx_tokens:
        reduction = (
            (identity_baseline - ctx_tokens) / identity_baseline * 100 if identity_baseline else 0
        )
        print(f"memory_context() alone:")
        print(f"  Synapse  : {ctx_tokens:,} tokens  ({ctx_ms} ms)")
        print(f"  Baseline : {identity_baseline:,} tokens  (raw identity/ folder)")
        print(f"  Saving   : {reduction:.0f}%")
        print()

    # ── Per-query table ─────────────────────────────────────────────────────
    if verbose:
        print(
            f"  {'Query':<32} {'Tier':<6} {'Synapse':>8}  {'Baseline':>9}  {'Saving':>7}  {'ms':>5}"
        )
        print("  " + "-" * 66)

    rows: list[dict[str, Any]] = []
    total_synapse = total_baseline = 0

    for q in QUERIES:
        try:
            t0 = time.perf_counter()
            result = memory_auto(config, q["query"])
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            synapse_tokens = result.get("_tokens", _estimate_tokens(result))
            baseline_tokens = _folder_tokens(vault, *q["baseline_folders"])

            # If vault folder is empty fall back to full vault so we don't show 0% saving
            if baseline_tokens == 0:
                baseline_tokens = full_vault_tokens

            reduction_pct = (
                (baseline_tokens - synapse_tokens) / baseline_tokens * 100
                if baseline_tokens > synapse_tokens > 0
                else 0.0
            )
            total_synapse += synapse_tokens
            total_baseline += baseline_tokens

            row = {
                "label": q["label"],
                "tier": q["tier"],
                "synapse_tokens": synapse_tokens,
                "baseline_tokens": baseline_tokens,
                "reduction_pct": round(reduction_pct, 1),
                "elapsed_ms": elapsed_ms,
            }
            rows.append(row)

            if verbose:
                print(
                    f"  {q['label']:<32} T{q['tier']:<5} "
                    f"{synapse_tokens:>8,}  {baseline_tokens:>9,}  "
                    f"{reduction_pct:>6.0f}%  {elapsed_ms:>4}ms"
                )
        except Exception as e:
            if verbose:
                print(f"  {q['label']:<32} ERROR: {e}")

    # ── Totals ──────────────────────────────────────────────────────────────
    overall_reduction = (
        (total_baseline - total_synapse) / total_baseline * 100
        if total_baseline > total_synapse > 0
        else 0.0
    )

    if verbose:
        print("  " + "-" * 66)
        print(
            f"  {'TOTAL (5 queries)':<32} {'':6} "
            f"{total_synapse:>8,}  {total_baseline:>9,}  "
            f"{overall_reduction:>6.0f}%"
        )
        print()
        print("Notes:")
        print("  Synapse tokens  = _tokens field in response  (JSON size ÷ 4)")
        print("  Baseline tokens = raw vault .md file size ÷ 4  (naive paste)")
        print("  Tier 1 = identity only  |  Tier 2 = active vault  |  Tier 3 = full vault + chats")

    summary: dict[str, Any] = {
        "run_at": datetime.now().isoformat(),
        "vault_total_tokens": full_vault_tokens,
        "memory_context_tokens": ctx_tokens,
        "identity_baseline_tokens": identity_baseline,
        "queries": rows,
        "total_synapse_tokens": total_synapse,
        "total_baseline_tokens": total_baseline,
        "overall_reduction_pct": round(overall_reduction, 1),
    }

    # ── Persist results ─────────────────────────────────────────────────────
    results_path = Path(__file__).parent / "benchmark_results.json"
    history: list[dict] = []
    if results_path.exists():
        try:
            history = json.loads(results_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    history.append(summary)
    results_path.write_text(json.dumps(history, indent=2), encoding="utf-8")

    if verbose:
        print()
        print(f"Results appended to {results_path.name}")

    return summary


if __name__ == "__main__":
    run_benchmark(verbose=True)
