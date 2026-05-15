"""
Bulk apply all pending patches, merging duplicates by key.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")

from server.config import load_config
from server.diff import apply_update, reject_update, load_pending, save_pending
from server.memory_file import parse_memory_text

config = load_config()
pending = load_pending(config)
print(f"Total pending: {len(pending)}")

# Group by key
groups: dict[str, list[dict]] = {}
for p in pending:
    groups.setdefault(p["key"], []).append(p)

print(f"Unique keys: {len(groups)}")
dupes = {k: v for k, v in groups.items() if len(v) > 1}
print(f"Keys with duplicates: {len(dupes)}\n")


def merge_contents(patches: list[dict]) -> str:
    base = max(patches, key=lambda p: len(p.get("after", "")))
    if len(patches) == 1:
        return base["after"]
    _, base_content = parse_memory_text(base["after"])
    base_lines = set(base_content.splitlines())
    extra: list[str] = []
    for p in patches:
        if p["patch_id"] == base["patch_id"]:
            continue
        _, content = parse_memory_text(p.get("after", ""))
        for line in content.splitlines():
            s = line.strip()
            if s.startswith("-") and s not in base_lines and len(s) > 5:
                extra.append(line)
                base_lines.add(s)
    if not extra:
        return base["after"]
    after = base["after"]
    idx = after.lower().find("\nhistory:")
    if idx != -1:
        after = after[:idx] + "\n" + "\n".join(extra) + after[idx:]
    else:
        after = after.rstrip() + "\n" + "\n".join(extra)
    return after


applied = 0
errors = []

for key, patches in groups.items():
    try:
        if len(patches) == 1:
            apply_update(config, patches[0]["patch_id"])
        else:
            print(f"  Merging {len(patches)} patches for {key}")
            base = max(patches, key=lambda p: len(p.get("after", "")))
            merged = merge_contents(patches)

            # Write merged content back into pending
            all_pending = load_pending(config)
            for p in all_pending:
                if p["patch_id"] == base["patch_id"]:
                    p["after"] = merged
            save_pending(config, all_pending)

            # Apply merged base
            apply_update(config, base["patch_id"])

            # Reject the rest
            remaining = {p["patch_id"] for p in load_pending(config)}
            for p in patches:
                if p["patch_id"] != base["patch_id"] and p["patch_id"] in remaining:
                    reject_update(config, p["patch_id"], "merged into base patch")

        applied += 1
        print(f"  OK {key}")
    except Exception as e:
        errors.append({"key": key, "error": str(e)})
        print(f"  FAIL {key}: {e}")

print(f"\nDone -- applied: {applied}, errors: {len(errors)}")
for e in errors:
    print(f"  ERROR {e['key']}: {e['error']}")
