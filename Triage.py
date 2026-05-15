import os
import re
import csv
import json
import time
import math
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI
from groq import Groq


# ============================================================
# SYNAPSE CHAT FILTER / TRIAGE INDEXER
#
# What it does:
# 1. Reads conversations from:
#    synapse_extracted/conversations_jsonl
#
# 2. Builds an index using:
#    - source filename
#    - source line
#    - conversation_id
#    - title
#    - character count
#    - estimated token count
#    - first N chars
#    - last N chars
#
# 3. Saves local index tables:
#    - JSONL
#    - JSON
#    - CSV
#    - Markdown table
#
# 4. Splits index into chunks.
#
# 5. Sends chunks to OpenRouter in parallel workers.
#
# 6. If OpenRouter rate-limits, that chunk switches to Groq fallback.
#
# 7. AI decides:
#    - keep_full
#    - keep_short
#    - skip
#    - redflag_sensitive
#    - redflag_secret
#    - redflag_identity
#    - redflag_financial
#    - redflag_health
#
# 8. Creates separate folders:
#    - useful filtered chats
#    - redflagged sensitive chats
#
# Resume-safe:
# - each chunk decision is saved separately
# - rerun skips completed decision chunks unless FORCE_REVIEW=True
# ============================================================


# ============================================================
# CONFIG
# ============================================================

EXTRACTED_FOLDER = "synapse_extracted"
CONVERSATIONS_JSONL_FOLDER = Path(EXTRACTED_FOLDER) / "conversations_jsonl"

OUTPUT_FOLDER = Path("synapse_filtered_chats")

INDEX_FOLDER = OUTPUT_FOLDER / "index"
INDEX_CHUNKS_FOLDER = INDEX_FOLDER / "chunks"
DECISIONS_FOLDER = OUTPUT_FOLDER / "openrouter_decisions"

FILTERED_JSON_FOLDER = OUTPUT_FOLDER / "filtered_conversations_json"
FILTERED_JSONL_FOLDER = OUTPUT_FOLDER / "filtered_conversations_jsonl"

REDFLAG_FOLDER = OUTPUT_FOLDER / "redflagged_sensitive_chats"
REDFLAG_JSON_FOLDER = REDFLAG_FOLDER / "json"
REDFLAG_JSONL_FOLDER = REDFLAG_FOLDER / "jsonl"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# Primary OpenRouter model.
OPENROUTER_MODEL = "mistralai/mistral-small-3.2-24b-instruct"

# Groq fallback model.
GROQ_MODEL = "llama-3.1-8b-instant"

# How much of each conversation goes into the index review.
FIRST_CHARS = 3000
LAST_CHARS = 1500

# Lower = more chunks, safer.
# Higher = fewer chunks, but more risk of context/output issues.
MAX_INDEX_CHUNK_CHARS = 120_000

# Parallel OpenRouter workers.
# If rate limits happen often, reduce to 2.
OPENROUTER_WORKERS = 3

REQUEST_DELAY_SECONDS = 0
MAX_RETRIES = 5
GROQ_MAX_RETRIES = 5

KEEP_ACTIONS = {"keep_full", "keep_short"}

REDFLAG_ACTIONS = {
    "redflag_sensitive",
    "redflag_secret",
    "redflag_identity",
    "redflag_financial",
    "redflag_health",
}

# If True, reprocesses even existing decision chunks.
FORCE_REVIEW = False


# ============================================================
# CLIENTS
# ============================================================

if not OPENROUTER_API_KEY:
    raise ValueError(
        "Missing OPENROUTER_API_KEY environment variable. "
        "Set it before running this script."
    )

if not GROQ_API_KEY:
    raise ValueError(
        "Missing GROQ_API_KEY environment variable. "
        "Set it before running this script."
    )

openrouter_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
    default_headers={
        "HTTP-Referer": "http://localhost",
        "X-Title": "Synapse Filter",
    },
)

groq_client = Groq(api_key=GROQ_API_KEY)


# ============================================================
# PROMPT
# ============================================================

FILTER_SYSTEM_PROMPT = """
You are the Synapse Conversation Filter and Sensitive Data Detector.

You will receive an index of ChatGPT conversations.
Each item contains:
- source_file
- source_line
- conversation_id
- title
- character count
- estimated token count
- first part of the conversation
- last part of the conversation

Your job has TWO parts:

1. Decide whether the conversation is worth preserving.
2. Detect whether the conversation contains sensitive, dangerous, private, or catastrophic-if-leaked information.

Return ONLY valid JSON.
No markdown.
No code fences.
No explanation outside JSON.

Return this schema exactly:

{
  "decisions": [
    {
      "conversation_id": "string",
      "title": "string",
      "source_file": "string",
      "source_line": 0,
      "action": "keep_full | keep_short | skip | redflag_sensitive | redflag_secret | redflag_identity | redflag_financial | redflag_health",
      "importance": "high | medium | low | useless | sensitive",
      "sensitivity_level": "none | low | medium | high | critical",
      "sensitivity_types": ["string"],
      "reason": "string",
      "tags": ["string"]
    }
  ]
}

Normal action rules:

keep_full:
- Important long-term memory
- Major coding project
- School/IB/EE/TOK/IA/exam planning
- University/career planning
- Health, family, travel, finance, project architecture
- Serious debugging or technical decisions
- Personal preferences that may matter later
- Important long-running technical workflow

keep_short:
- Some useful content, but not worth deep summarization
- One-off homework explanation
- Simple coding fix
- Short but potentially useful context
- Basic troubleshooting
- Small academic question

skip:
- Random tests
- Repeated screenshots with no meaningful content
- Casual filler
- Empty chats
- Broken links with no context
- Very minor one-off questions
- Spam, accidental input, or useless logs
- Conversations with no lasting value

Red-flag action rules:

Use redflag_secret if the conversation includes:
- API keys
- access tokens
- refresh tokens
- private keys
- passwords
- database credentials
- cloud credentials
- GitHub tokens
- .env contents
- SSH keys
- authentication headers
- session cookies
- hardcoded credentials

Use redflag_financial if the conversation includes:
- bank account details
- card numbers
- IBAN
- SWIFT
- salary slips
- tax IDs
- financial account screenshots
- investment account login data
- payment credentials
- banking information

Use redflag_identity if the conversation includes:
- passport numbers
- Emirates ID
- Aadhaar
- Social Security number
- visa documents
- birth certificate
- driver license
- home address
- phone number plus identifying context
- school ID
- full legal identity documents

Use redflag_health if the conversation includes:
- medical reports
- lab reports
- diagnoses
- prescriptions
- insurance claims
- doctor notes
- hospital documents

Use redflag_sensitive for other sensitive material that is not covered above but could cause serious harm if leaked.

Important:
- If a conversation has sensitive data, choose a redflag_* action instead of keep_full or keep_short.
- Do not include the actual secret, password, ID number, bank number, address, token, or private data in the reason.
- The reason should describe the category only.
- Do not overvalue a conversation just because it is long.
- Do not skip something only because it is short.
- If unsure and it may contain sensitive data, choose redflag_sensitive.
- If unsure and it does not seem sensitive, choose keep_short.
- Preserve project names and technical topics in tags.
"""


# ============================================================
# HELPERS
# ============================================================

def safe_filename(text, max_len=100):
    text = str(text).strip()

    bad_chars = '<>:"/\\|?*'
    for ch in bad_chars:
        text = text.replace(ch, "_")

    text = " ".join(text.split())

    if not text:
        text = "untitled"

    return text[:max_len]


def clean_json_text(raw):
    text = str(raw).strip()

    if text.startswith("```json"):
        text = text.removeprefix("```json").strip()

    if text.startswith("```"):
        text = text.removeprefix("```").strip()

    if text.endswith("```"):
        text = text[:-3].strip()

    first = text.find("{")
    last = text.rfind("}")

    if first != -1 and last != -1 and last > first:
        text = text[first:last + 1]

    return text


def is_rate_limit_error(error):
    text = str(error).lower()

    return (
        "429" in text
        or "rate limit" in text
        or "rate_limit" in text
        or "rate_limit_exceeded" in text
        or "ratelimit" in text
        or "too many requests" in text
    )


def get_wait_seconds_from_error(error):
    text = str(error).lower()

    minute_second_match = re.search(r"try again in ([0-9.]+)m([0-9.]+)s", text)
    if minute_second_match:
        minutes = float(minute_second_match.group(1))
        seconds = float(minute_second_match.group(2))
        return int(minutes * 60 + seconds) + 2

    second_match = re.search(r"try again in ([0-9.]+)s", text)
    if second_match:
        return int(float(second_match.group(1))) + 2

    minute_match = re.search(r"try again in ([0-9.]+)m", text)
    if minute_match:
        return int(float(minute_match.group(1)) * 60) + 2

    return 60


def load_all_conversations():
    if not CONVERSATIONS_JSONL_FOLDER.exists():
        raise FileNotFoundError(f"Missing folder: {CONVERSATIONS_JSONL_FOLDER}")

    conversations = []

    for file_path in sorted(CONVERSATIONS_JSONL_FOLDER.glob("*.jsonl")):
        print(f"Loading {file_path.name}...")

        with open(file_path, "r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, start=1):
                if not line.strip():
                    continue

                c = json.loads(line)
                c["_source_file"] = file_path.name
                c["_source_line"] = line_number

                conversations.append(c)

    print(f"\nLoaded conversations: {len(conversations)}")
    return conversations


def make_index_record(conversation):
    clean_text = conversation.get("clean_text", "")
    conversation_id = conversation.get("conversation_id", "unknown-id")
    title = conversation.get("title", "Untitled Conversation")
    source_file = conversation.get("_source_file", "unknown-source")
    source_line = conversation.get("_source_line", 0)

    first_part = clean_text[:FIRST_CHARS]

    if len(clean_text) > FIRST_CHARS + LAST_CHARS:
        last_part = clean_text[-LAST_CHARS:]
    else:
        last_part = ""

    return {
        "source_file": source_file,
        "source_line": source_line,
        "conversation_id": conversation_id,
        "title": title,
        "clean_chars": conversation.get("clean_chars", len(clean_text)),
        "estimated_tokens": conversation.get(
            "estimated_tokens",
            math.ceil(len(clean_text) / 4),
        ),
        "first_chars": first_part,
        "last_chars": last_part,
    }


def build_index(conversations):
    INDEX_FOLDER.mkdir(parents=True, exist_ok=True)

    records = [make_index_record(c) for c in conversations]

    jsonl_path = INDEX_FOLDER / "conversation_index.jsonl"
    json_path = INDEX_FOLDER / "conversation_index.json"
    csv_path = INDEX_FOLDER / "conversation_index_table.csv"
    md_path = INDEX_FOLDER / "conversation_index_table.md"

    with open(jsonl_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "source_file",
                "source_line",
                "conversation_id",
                "title",
                "clean_chars",
                "estimated_tokens",
            ],
        )

        writer.writeheader()

        for r in records:
            writer.writerow({
                "source_file": r["source_file"],
                "source_line": r["source_line"],
                "conversation_id": r["conversation_id"],
                "title": r["title"],
                "clean_chars": r["clean_chars"],
                "estimated_tokens": r["estimated_tokens"],
            })

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("| source_file | line | conversation_id | title | chars | est_tokens |\n")
        f.write("|---|---:|---|---|---:|---:|\n")

        for r in records:
            title = str(r["title"]).replace("|", "\\|")
            f.write(
                f"| {r['source_file']} "
                f"| {r['source_line']} "
                f"| {r['conversation_id']} "
                f"| {title} "
                f"| {r['clean_chars']} "
                f"| {r['estimated_tokens']} |\n"
            )

    print(f"\nSaved index JSONL: {jsonl_path}")
    print(f"Saved index JSON: {json_path}")
    print(f"Saved table CSV: {csv_path}")
    print(f"Saved table Markdown: {md_path}")

    return records


def split_index_into_chunks(records):
    INDEX_CHUNKS_FOLDER.mkdir(parents=True, exist_ok=True)

    chunks = []
    current = []
    current_chars = 0

    for r in records:
        compact = {
            "source_file": r["source_file"],
            "source_line": r["source_line"],
            "conversation_id": r["conversation_id"],
            "title": r["title"],
            "clean_chars": r["clean_chars"],
            "estimated_tokens": r["estimated_tokens"],
            "first_chars": r["first_chars"],
            "last_chars": r["last_chars"],
        }

        record_text = json.dumps(compact, ensure_ascii=False)

        if current and current_chars + len(record_text) > MAX_INDEX_CHUNK_CHARS:
            chunks.append(current)
            current = []
            current_chars = 0

        current.append(compact)
        current_chars += len(record_text)

    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks, start=1):
        path = INDEX_CHUNKS_FOLDER / f"index_chunk_{i:04d}.json"

        with open(path, "w", encoding="utf-8") as f:
            json.dump(chunk, f, ensure_ascii=False, indent=2)

    print(f"\nCreated index chunks: {len(chunks)}")
    print(f"Saved chunks to: {INDEX_CHUNKS_FOLDER}")

    return chunks


def decision_path(chunk_index):
    DECISIONS_FOLDER.mkdir(parents=True, exist_ok=True)
    return DECISIONS_FOLDER / f"decisions_chunk_{chunk_index:04d}.json"


# ============================================================
# MODEL REVIEW FUNCTIONS
# ============================================================

def review_index_chunk_with_groq(chunk, chunk_index, total_chunks):
    payload = {
        "chunk_index": chunk_index,
        "total_chunks": total_chunks,
        "conversations": chunk,
    }

    for attempt in range(1, GROQ_MAX_RETRIES + 1):
        try:
            print(f"Reviewing index chunk {chunk_index}/{total_chunks} with Groq fallback...")
            print(f"Items in chunk: {len(chunk)}")

            response = groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": FILTER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                temperature=0.1,
                max_completion_tokens=8192,
                response_format={"type": "json_object"},
            )

            raw = response.choices[0].message.content
            cleaned = clean_json_text(raw)
            parsed = json.loads(cleaned)

            parsed["chunk_index"] = chunk_index
            parsed["total_chunks"] = total_chunks
            parsed["model_used"] = GROQ_MODEL
            parsed["provider_used"] = "groq"

            return parsed

        except Exception as e:
            print(f"Groq fallback error on chunk {chunk_index}, attempt {attempt}: {e}")

            if is_rate_limit_error(e):
                wait = get_wait_seconds_from_error(e)
                print(f"Groq fallback rate-limited. Waiting {wait}s...")
                time.sleep(wait)
                continue

            if attempt == GROQ_MAX_RETRIES:
                raise

            wait = min(30 * attempt, 180)
            print(f"Waiting {wait}s...")
            time.sleep(wait)

    raise RuntimeError("Groq fallback failed unexpectedly.")


def review_index_chunk_with_openrouter(chunk, chunk_index, total_chunks):
    path = decision_path(chunk_index)

    if path.exists() and not FORCE_REVIEW:
        print(f"Decision already exists for chunk {chunk_index}. Loading existing decision.")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    payload = {
        "chunk_index": chunk_index,
        "total_chunks": total_chunks,
        "conversations": chunk,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"Reviewing index chunk {chunk_index}/{total_chunks} with OpenRouter...")
            print(f"Items in chunk: {len(chunk)}")

            response = openrouter_client.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=[
                    {"role": "system", "content": FILTER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False),
                    },
                ],
                temperature=0.1,
                max_tokens=8192,
                response_format={"type": "json_object"},
            )

            raw = response.choices[0].message.content
            cleaned = clean_json_text(raw)
            parsed = json.loads(cleaned)

            parsed["chunk_index"] = chunk_index
            parsed["total_chunks"] = total_chunks
            parsed["model_used"] = OPENROUTER_MODEL
            parsed["provider_used"] = "openrouter"

            with open(path, "w", encoding="utf-8") as f:
                json.dump(parsed, f, ensure_ascii=False, indent=2)

            return parsed

        except Exception as e:
            print(f"OpenRouter review error on chunk {chunk_index}, attempt {attempt}: {e}")

            if is_rate_limit_error(e):
                print("OpenRouter rate-limited. Switching this chunk to Groq fallback...")

                parsed = review_index_chunk_with_groq(
                    chunk=chunk,
                    chunk_index=chunk_index,
                    total_chunks=total_chunks,
                )

                with open(path, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, ensure_ascii=False, indent=2)

                return parsed

            if attempt == MAX_RETRIES:
                raise

            wait = min(30 * attempt, 180)
            print(f"Waiting {wait}s...")
            time.sleep(wait)

    raise RuntimeError("OpenRouter review failed unexpectedly.")


def review_all_chunks(chunks):
    all_decisions = []
    total_chunks = len(chunks)

    print("\nSTARTING OPENROUTER PARALLEL REVIEW")
    print("=" * 100)
    print(f"Total chunks: {total_chunks}")
    print(f"OpenRouter workers: {OPENROUTER_WORKERS}")
    print(f"OpenRouter model: {OPENROUTER_MODEL}")
    print(f"Groq fallback model: {GROQ_MODEL}")
    print("=" * 100)

    def worker(chunk_job):
        chunk_index, chunk = chunk_job

        result = review_index_chunk_with_openrouter(
            chunk=chunk,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
        )

        return chunk_index, result

    jobs = [
        (i, chunk)
        for i, chunk in enumerate(chunks, start=1)
    ]

    completed = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=OPENROUTER_WORKERS) as executor:
        future_to_job = {
            executor.submit(worker, job): job
            for job in jobs
        }

        for future in as_completed(future_to_job):
            chunk_index, _chunk = future_to_job[future]

            try:
                finished_chunk_index, result = future.result()
                decisions = result.get("decisions", [])
                all_decisions.extend(decisions)

                completed += 1

                print(
                    f"Finished chunk {finished_chunk_index}/{total_chunks} | "
                    f"Provider: {result.get('provider_used', 'unknown')} | "
                    f"Decisions: {len(decisions)} | "
                    f"Progress: {completed}/{total_chunks} | "
                    f"Errors: {errors}"
                )

            except Exception as e:
                errors += 1
                print(
                    f"FAILED chunk {chunk_index}/{total_chunks}: {e} | "
                    f"Progress: {completed}/{total_chunks} | "
                    f"Errors: {errors}"
                )

            if REQUEST_DELAY_SECONDS > 0:
                time.sleep(REQUEST_DELAY_SECONDS)

    all_decisions.sort(
        key=lambda d: (
            str(d.get("source_file", "")),
            int(d.get("source_line", 0)) if str(d.get("source_line", "0")).isdigit() else 0,
            str(d.get("conversation_id", "")),
        )
    )

    merged_path = OUTPUT_FOLDER / "all_filter_decisions.json"

    with open(merged_path, "w", encoding="utf-8") as f:
        json.dump(all_decisions, f, ensure_ascii=False, indent=2)

    print(f"\nSaved merged decisions: {merged_path}")
    print(f"Total decisions: {len(all_decisions)}")
    print(f"Chunk errors: {errors}")

    return all_decisions


# ============================================================
# OUTPUT TABLES
# ============================================================

def write_decision_tables(decisions):
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)

    csv_path = OUTPUT_FOLDER / "filter_decisions_table.csv"
    md_path = OUTPUT_FOLDER / "filter_decisions_table.md"

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "action",
                "importance",
                "sensitivity_level",
                "sensitivity_types",
                "source_file",
                "source_line",
                "conversation_id",
                "title",
                "reason",
                "tags",
            ],
        )

        writer.writeheader()

        for d in decisions:
            writer.writerow({
                "action": d.get("action", ""),
                "importance": d.get("importance", ""),
                "sensitivity_level": d.get("sensitivity_level", ""),
                "sensitivity_types": ", ".join(d.get("sensitivity_types", [])),
                "source_file": d.get("source_file", ""),
                "source_line": d.get("source_line", ""),
                "conversation_id": d.get("conversation_id", ""),
                "title": d.get("title", ""),
                "reason": d.get("reason", ""),
                "tags": ", ".join(d.get("tags", [])),
            })

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(
            "| action | importance | sensitivity | types | source_file | line | conversation_id | title | reason | tags |\n"
        )
        f.write("|---|---|---|---|---|---:|---|---|---|---|\n")

        for d in decisions:
            title = str(d.get("title", "")).replace("|", "\\|")
            reason = str(d.get("reason", "")).replace("|", "\\|")
            tags = ", ".join(d.get("tags", [])).replace("|", "\\|")
            sensitivity_types = ", ".join(d.get("sensitivity_types", [])).replace("|", "\\|")

            f.write(
                f"| {d.get('action', '')} "
                f"| {d.get('importance', '')} "
                f"| {d.get('sensitivity_level', '')} "
                f"| {sensitivity_types} "
                f"| {d.get('source_file', '')} "
                f"| {d.get('source_line', '')} "
                f"| {d.get('conversation_id', '')} "
                f"| {title} "
                f"| {reason} "
                f"| {tags} |\n"
            )

    print(f"Saved decision CSV: {csv_path}")
    print(f"Saved decision Markdown: {md_path}")


# ============================================================
# COPY FILTERED / REDFLAGGED CHATS
# ============================================================

def copy_filtered_chats(conversations, decisions):
    FILTERED_JSON_FOLDER.mkdir(parents=True, exist_ok=True)
    FILTERED_JSONL_FOLDER.mkdir(parents=True, exist_ok=True)

    REDFLAG_JSON_FOLDER.mkdir(parents=True, exist_ok=True)
    REDFLAG_JSONL_FOLDER.mkdir(parents=True, exist_ok=True)

    decision_by_id = {
        d.get("conversation_id"): d
        for d in decisions
        if d.get("conversation_id")
    }

    kept = []
    skipped = []
    redflagged = []

    source_jsonl_outputs = {}
    redflag_jsonl_outputs = {}

    for c in conversations:
        conversation_id = c.get("conversation_id", "unknown-id")
        decision = decision_by_id.get(conversation_id)

        if not decision:
            skipped.append((c, {"action": "skip", "reason": "No decision returned by AI."}))
            continue

        action = decision.get("action", "skip")

        title = c.get("title", "Untitled Conversation")
        filename = f"{safe_filename(title)}__{safe_filename(conversation_id, 60)}.json"

        output_record = dict(c)
        output_record["_filter_decision"] = decision

        if action in REDFLAG_ACTIONS:
            redflagged.append((c, decision))

            json_path = REDFLAG_JSON_FOLDER / filename

            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(output_record, f, ensure_ascii=False, indent=2)

            source_file = c.get("_source_file", "unknown.jsonl")
            out_jsonl_path = REDFLAG_JSONL_FOLDER / source_file

            if out_jsonl_path not in redflag_jsonl_outputs:
                redflag_jsonl_outputs[out_jsonl_path] = open(
                    out_jsonl_path,
                    "w",
                    encoding="utf-8",
                )

            redflag_jsonl_outputs[out_jsonl_path].write(
                json.dumps(output_record, ensure_ascii=False) + "\n"
            )
            continue

        if action not in KEEP_ACTIONS:
            skipped.append((c, decision))
            continue

        kept.append((c, decision))

        json_path = FILTERED_JSON_FOLDER / filename

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(output_record, f, ensure_ascii=False, indent=2)

        source_file = c.get("_source_file", "unknown.jsonl")
        out_jsonl_path = FILTERED_JSONL_FOLDER / source_file

        if out_jsonl_path not in source_jsonl_outputs:
            source_jsonl_outputs[out_jsonl_path] = open(
                out_jsonl_path,
                "w",
                encoding="utf-8",
            )

        source_jsonl_outputs[out_jsonl_path].write(
            json.dumps(output_record, ensure_ascii=False) + "\n"
        )

    for f in source_jsonl_outputs.values():
        f.close()

    for f in redflag_jsonl_outputs.values():
        f.close()

    kept_ids_path = OUTPUT_FOLDER / "kept_conversation_ids.txt"
    skipped_ids_path = OUTPUT_FOLDER / "skipped_conversation_ids.txt"
    redflag_ids_path = REDFLAG_FOLDER / "redflagged_conversation_ids.txt"

    with open(kept_ids_path, "w", encoding="utf-8") as f:
        for c, d in kept:
            f.write(
                f"{c.get('conversation_id')} | "
                f"{d.get('action')} | "
                f"{c.get('title')}\n"
            )

    with open(skipped_ids_path, "w", encoding="utf-8") as f:
        for c, d in skipped:
            f.write(
                f"{c.get('conversation_id')} | "
                f"{d.get('action')} | "
                f"{c.get('title')} | "
                f"{d.get('reason', '')}\n"
            )

    with open(redflag_ids_path, "w", encoding="utf-8") as f:
        for c, d in redflagged:
            f.write(
                f"{c.get('conversation_id')} | "
                f"{d.get('action')} | "
                f"{d.get('sensitivity_level')} | "
                f"{c.get('title')} | "
                f"{d.get('reason', '')}\n"
            )

    print("\nFILTER RESULTS")
    print("=" * 100)
    print(f"Kept conversations: {len(kept)}")
    print(f"Skipped conversations: {len(skipped)}")
    print(f"Redflagged sensitive conversations: {len(redflagged)}")
    print(f"Filtered JSON folder: {FILTERED_JSON_FOLDER}")
    print(f"Filtered JSONL folder: {FILTERED_JSONL_FOLDER}")
    print(f"Redflagged folder: {REDFLAG_FOLDER}")
    print(f"Kept IDs: {kept_ids_path}")
    print(f"Skipped IDs: {skipped_ids_path}")
    print(f"Redflagged IDs: {redflag_ids_path}")
    print("=" * 100)

    return kept, skipped, redflagged


# ============================================================
# FINAL STRUCTURE
# ============================================================

def print_final_structure():
    print("\nOUTPUT STRUCTURE")
    print("=" * 100)
    print("synapse_filtered_chats/")
    print("  index/")
    print("    conversation_index.jsonl")
    print("    conversation_index.json")
    print("    conversation_index_table.csv")
    print("    conversation_index_table.md")
    print("    chunks/")
    print("      index_chunk_0001.json")
    print("      index_chunk_0002.json")
    print("")
    print("  openrouter_decisions/")
    print("    decisions_chunk_0001.json")
    print("    decisions_chunk_0002.json")
    print("")
    print("  filtered_conversations_json/")
    print("    useful selected chats as individual JSON files")
    print("")
    print("  filtered_conversations_jsonl/")
    print("    useful selected chats grouped back into monthly JSONL files")
    print("")
    print("  redflagged_sensitive_chats/")
    print("    json/")
    print("    jsonl/")
    print("    redflagged_conversation_ids.txt")
    print("")
    print("  all_filter_decisions.json")
    print("  filter_decisions_table.csv")
    print("  filter_decisions_table.md")
    print("  kept_conversation_ids.txt")
    print("  skipped_conversation_ids.txt")
    print("=" * 100)


# ============================================================
# MAIN
# ============================================================

def main():
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)

    print("\nSYNAPSE FILTER START")
    print("=" * 100)
    print(f"Input folder: {CONVERSATIONS_JSONL_FOLDER}")
    print(f"Output folder: {OUTPUT_FOLDER}")
    print(f"OpenRouter model: {OPENROUTER_MODEL}")
    print(f"Groq fallback model: {GROQ_MODEL}")
    print(f"OpenRouter workers: {OPENROUTER_WORKERS}")
    print(f"Max index chunk chars: {MAX_INDEX_CHUNK_CHARS:,}")
    print("=" * 100)

    conversations = load_all_conversations()

    records = build_index(conversations)

    chunks = split_index_into_chunks(records)

    decisions = review_all_chunks(chunks)

    write_decision_tables(decisions)

    copy_filtered_chats(conversations, decisions)

    print_final_structure()

    print("\nDONE")
    print(f"Output folder: {OUTPUT_FOLDER.resolve()}")


if __name__ == "__main__":
    main()