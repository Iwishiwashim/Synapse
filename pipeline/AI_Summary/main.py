import json
import sys
import time
import re
import math
import threading
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv()

from groq import Groq
from openai import OpenAI

# ============================================================
# SYNAPSE FINAL AI SENDER
#
# Strategy:
# - Gemma (Gemini API) = primary for all conversations
# - OpenRouter = biggest conversations if Gemma fails
# - Groq = small/medium conversations fallback
# - Cerebras = fallback only when Groq rate-limit wait > 5 minutes
#
# Input: synapse_filtered_chats/filtered_conversations_jsonl/
# Blacklist: groq_blacklist_output/blacklist_ids.txt +
#            synapse_filtered_chats/redflagged_sensitive_chats/redflagged_conversation_ids.txt
#
# Resume-safe:
# - skips completed summaries
# - saves one JSON per conversation
# - appends all summaries to all_summaries.jsonl
# ============================================================


# ============================================================
# CONFIG
# ============================================================

FILTERED_FOLDER = "synapse_filtered_chats/filtered_conversations_jsonl"
OUTPUT_FOLDER = "synapse_ai_summaries"

BLACKLIST_FILE = "groq_blacklist_output/blacklist_ids.txt"
REDFLAG_FILE = "synapse_filtered_chats/redflagged_sensitive_chats/redflagged_conversation_ids.txt"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")

# -------------------------
# MODE
# -------------------------

TEST_MODE = False

TEST_GROQ_COUNT = 3
TEST_OPENROUTER_COUNT = 1

# -------------------------
# ROUTING
# -------------------------

OPENROUTER_MIN_CHARS = 150_000
GROQ_MAX_CHARS = 150_000

MAX_OPENROUTER_REQUESTS_PER_RUN = 49

# -------------------------
# GROQ SETTINGS
# -------------------------

GROQ_ONESHOT_CHAR_LIMIT = 85_000
GROQ_CHUNK_CHAR_LIMIT = 55_000

GROQ_WORKERS = 1
GROQ_WORKER_DELAY_SECONDS = 0.5
GROQ_CHUNK_DELAY_SECONDS = 1

GROQ_TO_CEREBRAS_WAIT_THRESHOLD_SECONDS = 300

# -------------------------
# CEREBRAS SETTINGS
# -------------------------

CEREBRAS_CHUNK_CHAR_LIMIT = 10_000
CEREBRAS_MIN_CHUNK_CHAR_LIMIT = 500
CEREBRAS_CHUNK_SHRINK_FACTOR = 0.70
CEREBRAS_CHUNK_DELAY_SECONDS = 1

# -------------------------
# OPENROUTER SETTINGS
# -------------------------

OPENROUTER_ONESHOT_CHAR_LIMIT = 500_000
OPENROUTER_CHUNK_CHAR_LIMIT = 500_000

MIN_CHUNK_CHAR_LIMIT = 120_000
CHUNK_SHRINK_FACTOR = 0.75

# -------------------------
# MODELS
# -------------------------

GEMMA_MODEL = "gemma-4-31b-it"
GROQ_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
OPENROUTER_MODEL = "inclusionai/ring-2.6-1t:free"
CEREBRAS_MODEL = "llama3.1-8b"

# -------------------------
# GEMMA SETTINGS
# -------------------------

GEMMA_ONESHOT_CHAR_LIMIT = 400_000
GEMMA_CHUNK_CHAR_LIMIT = 300_000
GEMMA_WORKERS = 8
GEMMA_REQUEST_DELAY_SECONDS = 0  # workers self-pace via 429 retry waits

# -------------------------
# RETRIES / DELAYS
# -------------------------

MAX_RETRIES = 5
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 60
REQUEST_DELAY_SECONDS = 2
OPENROUTER_REQUEST_DELAY_SECONDS = 3


# ============================================================
# GLOBAL LOCKS
# ============================================================

jsonl_write_lock = threading.Lock()
error_write_lock = threading.Lock()


# ============================================================
# KEY CHECKS
# ============================================================

if not GROQ_API_KEY:
    raise ValueError("Missing GROQ_API_KEY environment variable.")

if not OPENROUTER_API_KEY:
    raise ValueError("Missing OPENROUTER_API_KEY environment variable.")

if not CEREBRAS_API_KEY:
    raise ValueError("Missing CEREBRAS_API_KEY environment variable.")


# ============================================================
# CLIENTS
# ============================================================

groq_client = Groq(api_key=GROQ_API_KEY)

openrouter_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
    default_headers={
        "HTTP-Referer": "http://localhost",
        "X-Title": "Synapse",
    },
)

cerebras_client = OpenAI(
    base_url="https://api.cerebras.ai/v1",
    api_key=CEREBRAS_API_KEY,
)

if GEMINI_API_KEY:
    from google import genai as _genai
    from google.genai import types as _gtypes

    gemma_client = _genai.Client(api_key=GEMINI_API_KEY)
else:
    gemma_client = None


# ============================================================
# BLACKLIST
# ============================================================


def load_blacklist():
    ids = set()
    for path_str in [BLACKLIST_FILE, REDFLAG_FILE]:
        p = Path(path_str)
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                ids.add(line.split("|")[0].strip())
    print(f"Blacklist loaded: {len(ids)} conversation IDs will be skipped")
    return ids


# ============================================================
# PROMPTS
# ============================================================

SYSTEM_PROMPT = """
You are the Synapse Summarization Engine.

Analyze the provided ChatGPT conversation and return ONLY valid JSON.

No markdown.
No code fences.
No explanation.
No text before or after the JSON.

Return exactly one JSON object with this schema:

{
  "conversation_id": "string",
  "title": "string",
  "provider_used": "string",
  "model_used": "string",
  "summary_short": "string",
  "summary_deep": "string",
  "key_facts": ["string"],
  "decisions": ["string"],
  "tasks": ["string"],
  "projects": ["string"],
  "files_referenced": ["string"],
  "important_timestamps": ["string"],
  "tags": ["string"],
  "memory_candidates": ["string"],
  "search_keywords": ["string"]
}

Rules:
- Preserve important specific details.
- Include project names, scripts, tools, code decisions, study plans, technical setups, and user preferences.
- Include file references if present.
- Include dates/timestamps only when useful.
- Do not invent information.
- If something is not present, use an empty list or empty string.
- Keep the deep summary detailed but structured.
- Output must start with { and end with }.
"""

CHUNK_SYSTEM_PROMPT = """
You are the Synapse Chunk Summarization Engine.

Analyze this chunk from a larger ChatGPT conversation and return ONLY valid JSON.

No markdown.
No code fences.
No explanation.

Return exactly one JSON object:

{
  "conversation_id": "string",
  "title": "string",
  "chunk_summary": "string",
  "key_facts": ["string"],
  "decisions": ["string"],
  "tasks": ["string"],
  "projects": ["string"],
  "files_referenced": ["string"],
  "important_timestamps": ["string"],
  "tags": ["string"],
  "memory_candidates": ["string"],
  "search_keywords": ["string"]
}

Rules:
- Preserve specific details from this chunk.
- Do not invent information.
- Keep it compact but useful.
"""


# ============================================================
# LOAD DATA
# ============================================================


def load_extracted_conversations(filtered_folder):
    folder = Path(filtered_folder)

    if not folder.exists():
        raise FileNotFoundError(f"Missing folder: {folder}")

    blacklist = load_blacklist()
    conversations = []
    skipped = 0

    for file_path in sorted(folder.glob("*.jsonl")):
        print(f"Loading {file_path.name}...")
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                cid = rec.get("conversation_id", "")
                if cid in blacklist:
                    skipped += 1
                    continue
                conversations.append(rec)

    print(f"\nLoaded conversations: {len(conversations)} (blacklisted skipped: {skipped})")
    return conversations


# ============================================================
# OUTPUT HELPERS
# ============================================================


def safe_id(conversation_id):
    return str(conversation_id).replace("/", "_").replace("\\", "_")


def summary_path(output_folder, conversation_id):
    return output_folder / f"{safe_id(conversation_id)}.json"


def already_done(output_folder, conversation_id):
    return summary_path(output_folder, conversation_id).exists()


def save_summary(output_folder, summary):
    conversation_id = summary.get("conversation_id", "unknown-id")
    path = summary_path(output_folder, conversation_id)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    return path


def append_jsonl_threadsafe(path, record):
    with jsonl_write_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def save_error(output_folder, conversation, provider, error):
    error_record = {
        "conversation_id": conversation.get("conversation_id", "unknown-id"),
        "title": conversation.get("title", "Untitled Conversation"),
        "provider_used": provider,
        "error": str(error),
        "clean_chars": conversation.get("clean_chars", 0),
        "estimated_tokens": conversation.get("estimated_tokens", 0),
    }

    with error_write_lock:
        with open(output_folder / "errors.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(error_record, ensure_ascii=False) + "\n")


# ============================================================
# JSON PARSING
# ============================================================


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
        text = text[first : last + 1]

    return text


def safe_json_parse(raw, conversation_id, title, provider, model):
    cleaned = clean_json_text(raw)

    try:
        parsed = json.loads(cleaned)

        parsed["conversation_id"] = parsed.get("conversation_id") or conversation_id
        parsed["title"] = parsed.get("title") or title
        parsed["provider_used"] = provider
        parsed["model_used"] = model

        return parsed

    except json.JSONDecodeError:
        return {
            "conversation_id": conversation_id,
            "title": title,
            "provider_used": provider,
            "model_used": model,
            "summary_short": "",
            "summary_deep": "",
            "key_facts": [],
            "decisions": [],
            "tasks": [],
            "projects": [],
            "files_referenced": [],
            "important_timestamps": [],
            "tags": [],
            "memory_candidates": [],
            "search_keywords": [],
            "error": "Invalid JSON returned",
            "raw_output": raw,
            "cleaned_output": cleaned,
        }


# ============================================================
# ERROR / RATE-LIMIT HANDLING
# ============================================================


def get_response_headers(error):
    response = getattr(error, "response", None)

    if response is None:
        return {}

    headers = getattr(response, "headers", {})

    try:
        return dict(headers)
    except Exception:
        return {}


def parse_retry_after_from_headers(error):
    headers = get_response_headers(error)

    for key in [
        "retry-after",
        "Retry-After",
        "x-ratelimit-reset-after",
        "X-RateLimit-Reset-After",
    ]:
        value = headers.get(key)

        if value:
            try:
                return max(1, int(float(value)))
            except Exception:
                pass

    return None


def parse_wait_from_error_text(error):
    text = str(error).lower()

    minute_second_patterns = [
        r"try again in ([0-9.]+)m([0-9.]+)s",
        r"please try again in ([0-9.]+)m([0-9.]+)s",
    ]

    for pattern in minute_second_patterns:
        match = re.search(pattern, text)
        if match:
            minutes = float(match.group(1))
            seconds = float(match.group(2))
            return max(1, int(minutes * 60 + seconds) + 2)

    second_patterns = [
        r"try again in ([0-9.]+)s",
        r"try again in ([0-9.]+) seconds",
        r"retry after ([0-9.]+)s",
        r"retry after ([0-9.]+) seconds",
        r"please try again in ([0-9.]+)s",
        r"please try again in ([0-9.]+) seconds",
        r"please retry in ([0-9.]+)s",  # Gemini format
        r"please retry in ([0-9.]+) seconds",
    ]

    for pattern in second_patterns:
        match = re.search(pattern, text)
        if match:
            return max(1, int(float(match.group(1))) + 2)

    minute_patterns = [
        r"try again in ([0-9.]+)m",
        r"try again in ([0-9.]+) minutes",
        r"retry after ([0-9.]+)m",
        r"retry after ([0-9.]+) minutes",
        r"please try again in ([0-9.]+)m",
        r"please try again in ([0-9.]+) minutes",
    ]

    for pattern in minute_patterns:
        match = re.search(pattern, text)
        if match:
            return max(1, int(float(match.group(1)) * 60) + 2)

    millisecond_patterns = [
        r"try again in ([0-9.]+)ms",
        r"retry after ([0-9.]+)ms",
    ]

    for pattern in millisecond_patterns:
        match = re.search(pattern, text)
        if match:
            return max(1, int(float(match.group(1)) / 1000) + 2)

    return None


def is_rate_limit_error(error):
    text = str(error).lower()

    return (
        "429" in text
        or "rate limit" in text
        or "ratelimit" in text
        or "too many requests" in text
        or "rate_limit_exceeded" in text
    )


def is_context_error(error):
    text = str(error).lower()

    return (
        "context_length_exceeded" in text
        or "maximum context length" in text
        or "context length" in text
        or "too many tokens" in text
        or "reduce the length" in text
        or "current length" in text
        or "limit is 8192" in text
        or ("tokens" in text and "context" in text)
    )


def is_request_too_large_error(error):
    text = str(error).lower()

    return (
        "request too large" in text
        or "please reduce your message size" in text
        or ("limit 30000" in text and "requested" in text)
        or ("tpm" in text and "requested" in text and "limit" in text)
        or "error code: 413" in text
    )


def get_wait_seconds(error):
    return (
        parse_retry_after_from_headers(error)
        or parse_wait_from_error_text(error)
        or DEFAULT_RATE_LIMIT_WAIT_SECONDS
    )


# ============================================================
# CHUNKING
# ============================================================


def split_text_into_chunks(text, max_chars):
    chunks = []
    start = 0

    while start < len(text):
        end = min(start + max_chars, len(text))

        if end < len(text):
            boundary = text.rfind("-" * 100, start, end)

            if boundary != -1 and boundary > start:
                end = boundary
            else:
                paragraph = text.rfind("\n\n", start, end)
                if paragraph != -1 and paragraph > start + int(max_chars * 0.5):
                    end = paragraph

        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        if end <= start:
            break

        start = end

    return chunks


# ============================================================
# GEMMA
# ============================================================

_GEMMA_FALLBACK_MODEL = "gemma-4-26b-a4b-it"


def _gemma_generate(system, user):
    """Call Gemma via the Gemini API, return raw text. Falls back to smaller model on 503."""
    contents = [
        _gtypes.Content(role="user", parts=[_gtypes.Part.from_text(text=system)]),
        _gtypes.Content(role="model", parts=[_gtypes.Part.from_text(text="Understood.")]),
        _gtypes.Content(role="user", parts=[_gtypes.Part.from_text(text=user)]),
    ]
    for model in [GEMMA_MODEL, _GEMMA_FALLBACK_MODEL]:
        try:
            chunks = []
            for chunk in gemma_client.models.generate_content_stream(
                model=model, contents=contents
            ):
                if chunk.text:
                    chunks.append(chunk.text)
            return "".join(chunks)
        except Exception as e:
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                print(f"Gemma model {model} unavailable, trying fallback...")
                continue
            raise
    raise RuntimeError("All Gemma models unavailable")


def summarize_with_gemma_oneshot(conversation):
    conversation_id = conversation.get("conversation_id", "unknown-id")
    title = conversation.get("title", "Untitled Conversation")
    clean_text = conversation.get("clean_text", "")

    user = f"Conversation ID: {conversation_id}\nTitle: {title}\n\nConversation:\n{clean_text}"
    raw = _gemma_generate(SYSTEM_PROMPT, user)
    summary = safe_json_parse(raw, conversation_id, title, "gemma", GEMMA_MODEL)
    summary["chunked"] = False
    summary["chunk_count"] = 0
    return summary


def summarize_gemma_chunk(conversation_id, title, chunk_text, chunk_index, total_chunks):
    user = (
        f"This is chunk {chunk_index} of {total_chunks} from one ChatGPT conversation.\n\n"
        f"Conversation ID: {conversation_id}\nTitle: {title}\n\nChunk:\n{chunk_text}"
    )
    raw = _gemma_generate(CHUNK_SYSTEM_PROMPT, user)
    return safe_json_parse(raw, conversation_id, title, "gemma", GEMMA_MODEL)


def merge_gemma_chunk_summaries(conversation_id, title, chunk_summaries):
    summaries_text = json.dumps(chunk_summaries, ensure_ascii=False, indent=2)
    user = (
        f"You are merging summaries of chunks from the same ChatGPT conversation.\n\n"
        f"Conversation ID: {conversation_id}\nTitle: {title}\n\n"
        f"Chunk summaries:\n{summaries_text}\n\n"
        f"Create one final deep Synapse summary for the whole conversation.\nReturn valid compact JSON only."
    )
    raw = _gemma_generate(SYSTEM_PROMPT, user)
    final = safe_json_parse(raw, conversation_id, title, "gemma", GEMMA_MODEL)
    final["chunked"] = True
    final["chunk_count"] = len(chunk_summaries)
    return final


def summarize_with_gemma(conversation):
    conversation_id = conversation.get("conversation_id", "unknown-id")
    title = conversation.get("title", "Untitled Conversation")
    clean_text = conversation.get("clean_text", "")

    if len(clean_text) <= GEMMA_ONESHOT_CHAR_LIMIT:
        return summarize_with_gemma_oneshot(conversation)

    chunks = split_text_into_chunks(clean_text, GEMMA_CHUNK_CHAR_LIMIT)
    print(f"Gemma chunking: {len(chunks)} chunks")
    chunk_summaries = []
    for idx, chunk in enumerate(chunks, start=1):
        print(f"  Gemma chunk {idx}/{len(chunks)} ({len(chunk):,} chars)")
        chunk_summaries.append(
            summarize_gemma_chunk(conversation_id, title, chunk, idx, len(chunks))
        )
        time.sleep(1)
    return merge_gemma_chunk_summaries(conversation_id, title, chunk_summaries)


# ============================================================
# GROQ
# ============================================================


def summarize_with_groq_oneshot(conversation):
    conversation_id = conversation.get("conversation_id", "unknown-id")
    title = conversation.get("title", "Untitled Conversation")
    clean_text = conversation.get("clean_text", "")

    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""
Conversation ID: {conversation_id}
Title: {title}

Conversation:
{clean_text}
""",
            },
        ],
        temperature=0.2,
        max_completion_tokens=4096,
        top_p=1,
        stream=False,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content

    summary = safe_json_parse(
        raw=raw,
        conversation_id=conversation_id,
        title=title,
        provider="groq",
        model=GROQ_MODEL,
    )

    summary["chunked"] = False
    summary["chunk_count"] = 0

    return summary


def summarize_groq_chunk(conversation_id, title, chunk_text, chunk_index, total_chunks):
    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": CHUNK_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""
This is chunk {chunk_index} of {total_chunks} from one ChatGPT conversation.

Conversation ID: {conversation_id}
Title: {title}

Chunk:
{chunk_text}
""",
            },
        ],
        temperature=0.2,
        max_completion_tokens=2048,
        top_p=1,
        stream=False,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content

    return safe_json_parse(
        raw=raw,
        conversation_id=conversation_id,
        title=title,
        provider="groq",
        model=GROQ_MODEL,
    )


def merge_groq_chunk_summaries(conversation_id, title, chunk_summaries):
    summaries_text = json.dumps(chunk_summaries, ensure_ascii=False, indent=2)

    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""
You are merging summaries of chunks from the same ChatGPT conversation.

Conversation ID: {conversation_id}
Title: {title}

Chunk summaries:
{summaries_text}

Create one final deep Synapse summary for the whole conversation.
Return valid compact JSON only.
""",
            },
        ],
        temperature=0.2,
        max_completion_tokens=4096,
        top_p=1,
        stream=False,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content

    final_summary = safe_json_parse(
        raw=raw,
        conversation_id=conversation_id,
        title=title,
        provider="groq",
        model=GROQ_MODEL,
    )

    final_summary["chunked"] = True
    final_summary["chunk_count"] = len(chunk_summaries)

    return final_summary


def call_groq_chunk_with_retries(conversation_id, title, chunk, idx, total_chunks):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return summarize_groq_chunk(
                conversation_id=conversation_id,
                title=title,
                chunk_text=chunk,
                chunk_index=idx,
                total_chunks=total_chunks,
            )

        except Exception as e:
            print(f"Groq chunk ERROR: {e}")

            if is_rate_limit_error(e):
                wait = get_wait_seconds(e)

                if wait > GROQ_TO_CEREBRAS_WAIT_THRESHOLD_SECONDS:
                    raise RuntimeError(f"GROQ_LONG_RATE_LIMIT:{wait}")

                print(f"Groq rate limit on chunk. Waiting {wait}s...")
                time.sleep(wait)
                continue

            if is_request_too_large_error(e):
                raise RuntimeError(
                    f"Groq chunk too large at {len(chunk):,} chars. Lower GROQ_CHUNK_CHAR_LIMIT."
                )

            if attempt == MAX_RETRIES:
                raise

            wait = min(DEFAULT_RATE_LIMIT_WAIT_SECONDS * attempt, 300)
            print(f"Waiting {wait}s...")
            time.sleep(wait)

    raise RuntimeError("Groq chunk retry loop failed.")


def call_groq_merge_with_retries(conversation_id, title, chunk_summaries):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return merge_groq_chunk_summaries(
                conversation_id=conversation_id,
                title=title,
                chunk_summaries=chunk_summaries,
            )

        except Exception as e:
            print(f"Groq merge ERROR: {e}")

            if is_rate_limit_error(e):
                wait = get_wait_seconds(e)

                if wait > GROQ_TO_CEREBRAS_WAIT_THRESHOLD_SECONDS:
                    raise RuntimeError(f"GROQ_LONG_RATE_LIMIT:{wait}")

                print(f"Groq rate limit on merge. Waiting {wait}s...")
                time.sleep(wait)
                continue

            if is_request_too_large_error(e):
                raise RuntimeError(
                    "Groq merge request too large. Reduce chunk summary length or chunk count."
                )

            if attempt == MAX_RETRIES:
                raise

            wait = min(DEFAULT_RATE_LIMIT_WAIT_SECONDS * attempt, 300)
            print(f"Waiting {wait}s...")
            time.sleep(wait)

    raise RuntimeError("Groq merge retry loop failed.")


def summarize_with_groq_chunked(conversation):
    conversation_id = conversation.get("conversation_id", "unknown-id")
    title = conversation.get("title", "Untitled Conversation")
    clean_text = conversation.get("clean_text", "")

    chunks = split_text_into_chunks(clean_text, GROQ_CHUNK_CHAR_LIMIT)

    print("Groq chunking enabled.")
    print(f"Groq chunk size: {GROQ_CHUNK_CHAR_LIMIT:,} characters")
    print(f"Created {len(chunks)} Groq chunks.")

    chunk_summaries = []

    for idx, chunk in enumerate(chunks, start=1):
        print(f"Summarizing Groq chunk {idx}/{len(chunks)}...")
        print(f"Chunk characters: {len(chunk):,}")

        chunk_summary = call_groq_chunk_with_retries(
            conversation_id=conversation_id,
            title=title,
            chunk=chunk,
            idx=idx,
            total_chunks=len(chunks),
        )

        chunk_summaries.append(chunk_summary)
        time.sleep(GROQ_CHUNK_DELAY_SECONDS)

    print("Merging Groq chunk summaries...")

    return call_groq_merge_with_retries(
        conversation_id=conversation_id,
        title=title,
        chunk_summaries=chunk_summaries,
    )


def summarize_with_groq(conversation):
    clean_text = conversation.get("clean_text", "")

    if len(clean_text) > GROQ_ONESHOT_CHAR_LIMIT:
        return summarize_with_groq_chunked(conversation)

    try:
        return summarize_with_groq_oneshot(conversation)

    except Exception as e:
        if is_rate_limit_error(e):
            wait = get_wait_seconds(e)

            if wait > GROQ_TO_CEREBRAS_WAIT_THRESHOLD_SECONDS:
                raise RuntimeError(f"GROQ_LONG_RATE_LIMIT:{wait}")

            raise

        if is_request_too_large_error(e):
            print("Groq one-shot too large. Switching to Groq chunking...")
            return summarize_with_groq_chunked(conversation)

        raise


# ============================================================
# CEREBRAS FALLBACK
# ============================================================


def summarize_cerebras_chunk(conversation_id, title, chunk_text, chunk_index, total_chunks):
    response = cerebras_client.chat.completions.create(
        model=CEREBRAS_MODEL,
        messages=[
            {"role": "system", "content": CHUNK_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""
This is chunk {chunk_index} of {total_chunks} from one ChatGPT conversation.

Conversation ID: {conversation_id}
Title: {title}

Chunk:
{chunk_text}
""",
            },
        ],
        temperature=0.2,
        max_tokens=700,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content

    return safe_json_parse(
        raw=raw,
        conversation_id=conversation_id,
        title=title,
        provider="cerebras",
        model=CEREBRAS_MODEL,
    )


def merge_cerebras_chunk_summaries(conversation_id, title, chunk_summaries):
    summaries_text = json.dumps(chunk_summaries, ensure_ascii=False, indent=2)

    response = cerebras_client.chat.completions.create(
        model=CEREBRAS_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""
You are merging summaries of chunks from the same ChatGPT conversation.

Conversation ID: {conversation_id}
Title: {title}

Chunk summaries:
{summaries_text}

Create one final Synapse summary for the whole conversation.
Return valid compact JSON only.
""",
            },
        ],
        temperature=0.2,
        max_tokens=1400,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content

    final_summary = safe_json_parse(
        raw=raw,
        conversation_id=conversation_id,
        title=title,
        provider="cerebras",
        model=CEREBRAS_MODEL,
    )

    final_summary["chunked"] = True
    final_summary["chunk_count"] = len(chunk_summaries)

    return final_summary


def call_cerebras_chunk_with_retries(conversation_id, title, chunk, idx, total_chunks):
    current_chunk = chunk.strip()

    if not current_chunk:
        return {
            "conversation_id": conversation_id,
            "title": title,
            "provider_used": "cerebras",
            "model_used": CEREBRAS_MODEL,
            "chunk_summary": "",
            "key_facts": [],
            "decisions": [],
            "tasks": [],
            "projects": [],
            "files_referenced": [],
            "important_timestamps": [],
            "tags": [],
            "memory_candidates": [],
            "search_keywords": [],
        }

    while True:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return summarize_cerebras_chunk(
                    conversation_id=conversation_id,
                    title=title,
                    chunk_text=current_chunk,
                    chunk_index=idx,
                    total_chunks=total_chunks,
                )

            except Exception as e:
                print(f"Cerebras chunk ERROR: {e}")

                if is_context_error(e):
                    new_size = int(len(current_chunk) * CEREBRAS_CHUNK_SHRINK_FACTOR)

                    if new_size < 500:
                        raise RuntimeError(
                            f"Cerebras chunk still too large even after shrinking. "
                            f"Current chars: {len(current_chunk):,}"
                        )

                    print(
                        f"Cerebras context too long. Shrinking chunk from "
                        f"{len(current_chunk):,} to {new_size:,} characters..."
                    )

                    current_chunk = current_chunk[:new_size].strip()
                    break

                if is_rate_limit_error(e):
                    wait = get_wait_seconds(e)
                    print(f"Cerebras rate limit. Waiting {wait}s...")
                    time.sleep(wait)
                    continue

                if attempt == MAX_RETRIES:
                    raise

                wait = min(DEFAULT_RATE_LIMIT_WAIT_SECONDS * attempt, 300)
                print(f"Waiting {wait}s...")
                time.sleep(wait)


def summarize_with_cerebras_chunked(conversation):
    conversation_id = conversation.get("conversation_id", "unknown-id")
    title = conversation.get("title", "Untitled Conversation")
    clean_text = conversation.get("clean_text", "")

    current_chunk_limit = CEREBRAS_CHUNK_CHAR_LIMIT

    while current_chunk_limit >= CEREBRAS_MIN_CHUNK_CHAR_LIMIT:
        chunks = split_text_into_chunks(clean_text, current_chunk_limit)

        print("Cerebras fallback enabled.")
        print(f"Cerebras model: {CEREBRAS_MODEL}")
        print(f"Cerebras chunk size: {current_chunk_limit:,} characters")
        print(f"Created {len(chunks)} Cerebras chunks.")

        chunk_summaries = []

        try:
            for idx, chunk in enumerate(chunks, start=1):
                print(f"Summarizing Cerebras chunk {idx}/{len(chunks)}...")
                print(f"Chunk characters: {len(chunk):,}")

                chunk_summary = call_cerebras_chunk_with_retries(
                    conversation_id=conversation_id,
                    title=title,
                    chunk=chunk,
                    idx=idx,
                    total_chunks=len(chunks),
                )

                chunk_summaries.append(chunk_summary)
                time.sleep(CEREBRAS_CHUNK_DELAY_SECONDS)

            print("Merging Cerebras chunk summaries...")

            return merge_cerebras_chunk_summaries(
                conversation_id=conversation_id,
                title=title,
                chunk_summaries=chunk_summaries,
            )

        except Exception as e:
            if is_context_error(e):
                current_chunk_limit = int(current_chunk_limit * CEREBRAS_CHUNK_SHRINK_FACTOR)
                print(
                    f"Cerebras whole pass context issue. Retrying with chunk size {current_chunk_limit:,}..."
                )
                continue

            raise

    raise RuntimeError("Could not fit Cerebras conversation after shrinking chunks.")


# ============================================================
# OPENROUTER
# ============================================================


def summarize_openrouter_text(conversation_id, title, text, max_tokens=4096):
    response = openrouter_client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""
Conversation ID: {conversation_id}
Title: {title}

Conversation:
{text}
""",
            },
        ],
        temperature=0.2,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content

    return safe_json_parse(
        raw=raw,
        conversation_id=conversation_id,
        title=title,
        provider="openrouter",
        model=OPENROUTER_MODEL,
    )


def summarize_openrouter_chunk(conversation_id, title, chunk_text, chunk_index, total_chunks):
    response = openrouter_client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[
            {"role": "system", "content": CHUNK_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""
This is chunk {chunk_index} of {total_chunks} from one very large ChatGPT conversation.

Conversation ID: {conversation_id}
Title: {title}

Chunk:
{chunk_text}
""",
            },
        ],
        temperature=0.2,
        max_tokens=2048,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content

    return safe_json_parse(
        raw=raw,
        conversation_id=conversation_id,
        title=title,
        provider="openrouter",
        model=OPENROUTER_MODEL,
    )


def merge_openrouter_chunk_summaries(conversation_id, title, chunk_summaries):
    summaries_text = json.dumps(chunk_summaries, ensure_ascii=False, indent=2)

    response = openrouter_client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"""
You are merging summaries of chunks from the same long ChatGPT conversation.

Conversation ID: {conversation_id}
Title: {title}

Chunk summaries:
{summaries_text}

Create one final deep Synapse summary for the whole conversation.
Return valid compact JSON only.
""",
            },
        ],
        temperature=0.2,
        max_tokens=8192,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content

    final_summary = safe_json_parse(
        raw=raw,
        conversation_id=conversation_id,
        title=title,
        provider="openrouter",
        model=OPENROUTER_MODEL,
    )

    final_summary["chunked"] = True
    final_summary["chunk_count"] = len(chunk_summaries)

    return final_summary


def estimate_openrouter_requests(conversation):
    chars = conversation.get("clean_chars", 0)

    if chars <= OPENROUTER_ONESHOT_CHAR_LIMIT:
        return 1

    chunks = math.ceil(chars / OPENROUTER_CHUNK_CHAR_LIMIT)
    return chunks + 1


def summarize_with_openrouter(conversation):
    conversation_id = conversation.get("conversation_id", "unknown-id")
    title = conversation.get("title", "Untitled Conversation")
    clean_text = conversation.get("clean_text", "")

    if len(clean_text) <= OPENROUTER_ONESHOT_CHAR_LIMIT:
        try:
            print("Trying OpenRouter one-shot...")

            summary = summarize_openrouter_text(
                conversation_id=conversation_id,
                title=title,
                text=clean_text,
                max_tokens=8192,
            )

            summary["chunked"] = False
            summary["chunk_count"] = 0

            return summary

        except Exception as e:
            if not is_context_error(e):
                raise

            print("OpenRouter one-shot exceeded context. Switching to chunking...")

    current_chunk_limit = OPENROUTER_CHUNK_CHAR_LIMIT

    while current_chunk_limit >= MIN_CHUNK_CHAR_LIMIT:
        print(f"Trying OpenRouter chunk size: {current_chunk_limit:,}")

        chunks = split_text_into_chunks(clean_text, current_chunk_limit)
        print(f"Created {len(chunks)} OpenRouter chunks.")

        chunk_summaries = []

        try:
            for idx, chunk in enumerate(chunks, start=1):
                print(f"Summarizing OpenRouter chunk {idx}/{len(chunks)}...")
                print(f"Chunk characters: {len(chunk):,}")

                chunk_summary = summarize_openrouter_chunk(
                    conversation_id=conversation_id,
                    title=title,
                    chunk_text=chunk,
                    chunk_index=idx,
                    total_chunks=len(chunks),
                )

                chunk_summaries.append(chunk_summary)
                time.sleep(OPENROUTER_REQUEST_DELAY_SECONDS)

            print("Merging OpenRouter chunk summaries...")

            return merge_openrouter_chunk_summaries(
                conversation_id=conversation_id,
                title=title,
                chunk_summaries=chunk_summaries,
            )

        except Exception as e:
            if is_context_error(e):
                print(f"OpenRouter chunk size {current_chunk_limit:,} too large.")
                current_chunk_limit = int(current_chunk_limit * CHUNK_SHRINK_FACTOR)
                print(f"Shrinking to {current_chunk_limit:,}...")
                continue

            raise

    raise RuntimeError("Could not fit OpenRouter conversation even after chunk shrinking.")


# ============================================================
# RETRY WRAPPER
# ============================================================


def call_with_retries(provider, conversation):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"Attempt {attempt}/{MAX_RETRIES}...")

            if provider == "gemma":
                return summarize_with_gemma(conversation)

            if provider == "groq":
                return summarize_with_groq(conversation)

            if provider == "openrouter":
                return summarize_with_openrouter(conversation)

            if provider == "cerebras":
                return summarize_with_cerebras_chunked(conversation)

            raise ValueError(f"Unknown provider: {provider}")

        except Exception as e:
            print(f"ERROR: {e}")

            text = str(e)

            # Gemma 429 quota/rate-limit — must be checked BEFORE the "500" check
            # because Gemini 429 errors contain "limit: 1500" which matches "500"
            if provider == "gemma" and ("429" in text or "resource_exhausted" in text.lower()):
                # Daily quota: quotaId contains "PerDay" and limit is 1500
                is_daily = "perday" in text.lower() or "limit: 1500" in text
                if is_daily:
                    # Daily quota exhausted — no point retrying for hours
                    raise RuntimeError(f"Gemma daily quota exhausted. Stop and wait for reset. {e}")
                # RPM-style 429 — parse suggested wait and retry
                wait = get_wait_seconds(e) or 45
                print(f"Gemma 429 rate limit. Waiting {wait}s before retry...")
                time.sleep(wait)
                continue

            # Gemma 500s are transient — retry immediately with short delay, no long wait
            if provider == "gemma" and "500" in text and "429" not in text:
                if attempt < MAX_RETRIES:
                    print(f"Gemma 500 internal error. Quick retry in 5s...")
                    time.sleep(5)
                    continue
                raise

            if provider == "groq" and text.startswith("GROQ_LONG_RATE_LIMIT:"):
                wait = int(text.split(":")[1])
                print(
                    f"Groq wait is {wait}s "
                    f"(>{GROQ_TO_CEREBRAS_WAIT_THRESHOLD_SECONDS}s). "
                    "Switching this conversation to Cerebras fallback..."
                )
                return summarize_with_cerebras_chunked(conversation)

            if provider == "groq" and is_request_too_large_error(e):
                print("Groq request too large. Switching to Groq chunking immediately...")
                return summarize_with_groq_chunked(conversation)

            if is_context_error(e):
                print(
                    "Context-length error detected. Not waiting because waiting will not fix message length."
                )
                raise

            if is_rate_limit_error(e):
                wait = get_wait_seconds(e)

                if provider == "groq" and wait > GROQ_TO_CEREBRAS_WAIT_THRESHOLD_SECONDS:
                    print(
                        f"Groq rate limit wait is {wait}s "
                        f"(>{GROQ_TO_CEREBRAS_WAIT_THRESHOLD_SECONDS}s). "
                        "Switching this conversation to Cerebras fallback..."
                    )
                    return summarize_with_cerebras_chunked(conversation)

                print(f"Rate limit detected. Waiting {wait} seconds, then retrying...")
                time.sleep(wait)
                continue

            if attempt == MAX_RETRIES:
                raise

            wait = min(DEFAULT_RATE_LIMIT_WAIT_SECONDS * attempt, 300)
            print(f"Non-rate-limit error. Waiting {wait} seconds, then retrying...")
            time.sleep(wait)

    raise RuntimeError("Retry loop failed unexpectedly.")


# ============================================================
# QUEUES
# ============================================================


def build_queues(conversations, output_folder):
    ranked = sorted(conversations, key=lambda x: x.get("clean_chars", 0), reverse=True)

    openrouter_queue = []
    groq_queue = []

    openrouter_done = []
    groq_done = []

    for c in ranked:
        conversation_id = c.get("conversation_id", "unknown-id")
        chars = c.get("clean_chars", 0)

        # Route everything through Gemma; fall back to OpenRouter for very large convos if no Gemma key
        if not gemma_client and chars > OPENROUTER_MIN_CHARS:
            if already_done(output_folder, conversation_id):
                openrouter_done.append(c)
            else:
                openrouter_queue.append(c)
        else:
            if already_done(output_folder, conversation_id):
                groq_done.append(c)
            else:
                groq_queue.append(c)

    return ranked, groq_queue, openrouter_queue, groq_done, openrouter_done


def estimate_groq_chunked_jobs(groq_queue):
    oneshot = 0
    chunked = 0
    estimated_chunk_requests = 0

    for c in groq_queue:
        chars = c.get("clean_chars", 0)

        if chars <= GROQ_ONESHOT_CHAR_LIMIT:
            oneshot += 1
        else:
            chunked += 1
            chunks = math.ceil(chars / GROQ_CHUNK_CHAR_LIMIT)
            estimated_chunk_requests += chunks + 1

    return oneshot, chunked, estimated_chunk_requests


def print_plan(groq_queue, openrouter_queue, groq_done, openrouter_done):
    openrouter_requests_total = sum(estimate_openrouter_requests(c) for c in openrouter_queue)

    openrouter_jobs_this_run = []
    openrouter_requests_this_run = 0

    for c in openrouter_queue:
        needed = estimate_openrouter_requests(c)

        if openrouter_requests_this_run + needed > MAX_OPENROUTER_REQUESTS_PER_RUN:
            break

        openrouter_requests_this_run += needed
        openrouter_jobs_this_run.append(c)

    groq_oneshot, groq_chunked, groq_chunk_requests = estimate_groq_chunked_jobs(groq_queue)

    print("\nPLAN")
    print("=" * 100)
    print(f"Pending Groq/Cerebras-fallback jobs: {len(groq_queue)}")
    print(f"Pending OpenRouter jobs: {len(openrouter_queue)}")
    print(f"Completed Groq/Cerebras jobs already skipped: {len(groq_done)}")
    print(f"Completed OpenRouter jobs already skipped: {len(openrouter_done)}")
    print("-" * 100)
    print(f"Estimated OpenRouter requests remaining total: {openrouter_requests_total}")
    print(f"OpenRouter request budget this run: {MAX_OPENROUTER_REQUESTS_PER_RUN}")
    print(f"OpenRouter jobs selected this run: {len(openrouter_jobs_this_run)}")
    print(f"OpenRouter requests selected this run: {openrouter_requests_this_run}")
    print("-" * 100)
    print(f"Groq/Cerebras jobs selected this run: {len(groq_queue)}")
    print(f"Groq one-shot jobs: {groq_oneshot}")
    print(f"Groq chunked jobs: {groq_chunked}")
    print(f"Estimated Groq requests for chunked jobs: {groq_chunk_requests}")
    print(f"Groq workers: {GROQ_WORKERS}")
    print(f"Cerebras fallback if Groq wait > {GROQ_TO_CEREBRAS_WAIT_THRESHOLD_SECONDS}s")
    print(f"Cerebras chunk size: {CEREBRAS_CHUNK_CHAR_LIMIT:,}")
    print("=" * 100)

    return openrouter_jobs_this_run


def build_test_items(groq_queue, openrouter_queue):
    items = []

    for c in groq_queue[-TEST_GROQ_COUNT:]:
        items.append(("groq", c))

    for c in openrouter_queue[:TEST_OPENROUTER_COUNT]:
        items.append(("openrouter", c))

    return items


def build_full_items(groq_queue, openrouter_jobs_this_run):
    items = []

    for c in openrouter_jobs_this_run:
        items.append(("openrouter", c))

    provider = "gemma" if gemma_client else "groq"
    for c in groq_queue:
        items.append((provider, c))

    return items


# ============================================================
# PROCESSING
# ============================================================


def process_one(provider, conversation, output_folder, all_summaries_path, index, total):
    conversation_id = conversation.get("conversation_id", "unknown-id")
    title = conversation.get("title", "Untitled Conversation")
    chars = conversation.get("clean_chars", 0)
    tokens = conversation.get("estimated_tokens", 0)

    print("\n" + "=" * 100)
    print(f"[{index}/{total}] {title}")
    print(f"ID: {conversation_id}")
    print(f"Provider: {provider}")
    print(f"Characters: {chars:,}")
    print(f"Estimated tokens: {tokens:,}")

    if provider == "openrouter":
        print(f"Estimated OpenRouter requests: {estimate_openrouter_requests(conversation)}")

    if provider == "groq":
        if chars > GROQ_ONESHOT_CHAR_LIMIT:
            chunks = math.ceil(chars / GROQ_CHUNK_CHAR_LIMIT)
            print("Groq mode: chunked")
            print(f"Estimated Groq requests: {chunks + 1}")
            print("Fallback: Cerebras if Groq wait > 5 minutes")
        else:
            print("Groq mode: one-shot")
            print("Fallback: Cerebras if Groq wait > 5 minutes")

    if already_done(output_folder, conversation_id):
        print("Already summarized. Skipping.")
        return "skipped"

    try:
        summary = call_with_retries(provider, conversation)

        output_file = save_summary(output_folder, summary)
        append_jsonl_threadsafe(all_summaries_path, summary)

        print(f"Saved: {output_file}")
        return "success"

    except Exception as e:
        print(f"FAILED: {e}")
        save_error(output_folder, conversation, provider, e)
        return "error"


def process_groq_parallel(groq_items, output_folder, all_summaries_path):
    success = 0
    skipped = 0
    errors = 0

    total = len(groq_items)

    print("\n" + "=" * 100)
    print("STARTING GROQ PROCESSING WITH CEREBRAS FALLBACK")
    print(f"Groq workers: {GROQ_WORKERS}")
    print(f"Groq jobs: {total}")
    print(f"Cerebras fallback threshold: {GROQ_TO_CEREBRAS_WAIT_THRESHOLD_SECONDS}s")
    print("=" * 100)

    def worker(job):
        index, provider, conversation = job

        time.sleep(GROQ_WORKER_DELAY_SECONDS)

        return process_one(
            provider=provider,
            conversation=conversation,
            output_folder=output_folder,
            all_summaries_path=all_summaries_path,
            index=index,
            total=total,
        )

    jobs = [
        (index, provider, conversation)
        for index, (provider, conversation) in enumerate(groq_items, start=1)
    ]

    with ThreadPoolExecutor(max_workers=GROQ_WORKERS) as executor:
        future_to_job = {executor.submit(worker, job): job for job in jobs}

        for future in as_completed(future_to_job):
            try:
                result = future.result()

                if result == "success":
                    success += 1
                elif result == "skipped":
                    skipped += 1
                else:
                    errors += 1

            except Exception as e:
                print(f"THREAD ERROR: {e}")
                errors += 1

            completed = success + skipped + errors

            print(
                f"Groq/Cerebras progress: {completed}/{total} | "
                f"Success: {success} | Skipped: {skipped} | Errors: {errors}"
            )

    return {
        "success": success,
        "skipped": skipped,
        "errors": errors,
    }


def main():
    output_folder = Path(OUTPUT_FOLDER)
    output_folder.mkdir(parents=True, exist_ok=True)

    all_summaries_path = output_folder / "all_summaries.jsonl"

    conversations = load_extracted_conversations(FILTERED_FOLDER)

    ranked, groq_queue, openrouter_queue, groq_done, openrouter_done = build_queues(
        conversations,
        output_folder,
    )

    openrouter_jobs_this_run = print_plan(
        groq_queue=groq_queue,
        openrouter_queue=openrouter_queue,
        groq_done=groq_done,
        openrouter_done=openrouter_done,
    )

    if TEST_MODE:
        items = build_test_items(groq_queue, openrouter_queue)
        print(f"\nTEST MODE: {len(items)} total jobs.")
    else:
        items = build_full_items(
            groq_queue=groq_queue,
            openrouter_jobs_this_run=openrouter_jobs_this_run,
        )
        print(f"\nFULL MODE: {len(items)} total jobs this run.")

    print("\nRUN ORDER")
    print("=" * 100)
    print("1. OpenRouter selected biggest conversations sequentially")
    print("2. Groq remaining small/medium conversations")
    print("3. If Groq wait >5 min, that conversation switches to Cerebras")
    print("4. Groq automatically chunks jobs that exceed TPM")
    print("5. Cerebras automatically shrinks chunks instead of waiting on context errors")
    print("=" * 100)

    gemma_items = [(p, c) for p, c in items if p == "gemma"]
    total = len(gemma_items)
    success = 0
    skipped = 0
    errors = 0

    print(f"\nGEMMA PROCESSING: {total} conversations")
    print("=" * 100)

    def gemma_worker(job):
        index, provider, conversation = job
        result = process_one(
            provider=provider,
            conversation=conversation,
            output_folder=output_folder,
            all_summaries_path=all_summaries_path,
            index=index,
            total=total,
        )
        return result

    jobs = [(i, p, c) for i, (p, c) in enumerate(gemma_items, start=1)]

    with ThreadPoolExecutor(max_workers=GEMMA_WORKERS) as executor:
        future_to_job = {executor.submit(gemma_worker, job): job for job in jobs}
        for future in as_completed(future_to_job):
            try:
                result = future.result()
            except Exception as e:
                print(f"THREAD ERROR: {e}")
                result = "error"
            if result == "success":
                success += 1
            elif result == "skipped":
                skipped += 1
            else:
                errors += 1
            print(
                f"Progress: {success+skipped+errors}/{total} | Success: {success} | Skipped: {skipped} | Errors: {errors}"
            )

    print("\n" + "=" * 100)
    print("RUN COMPLETE")
    print("=" * 100)
    print(f"Total: {total} | Success: {success} | Skipped: {skipped} | Errors: {errors}")
    print(f"Output folder: {output_folder.resolve()}")
    print("=" * 100)


if __name__ == "__main__":
    main()
