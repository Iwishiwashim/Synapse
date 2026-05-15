import os
import re
import json
import time
from pathlib import Path
from groq import Groq

# ============================================================
# GROQ BLACKLIST FROM REDFLAG TXT
#
# Purpose:
# - Reads only redflagged_convo.txt
# - Sends ONLY metadata lines to Groq
# - Groq decides which conversation IDs must be blacklisted
# - Does NOT send full conversation text
# - Does NOT inspect clean_text
# - Produces blacklist files for your Gemma filter
# ============================================================


# ============================================================
# CONFIG
# ============================================================

REDFLAG_TXT = Path("redflagged_convo.txt")

OUTPUT_FOLDER = Path("groq_blacklist_output")
CHUNKS_FOLDER = OUTPUT_FOLDER / "chunks"
DECISIONS_FOLDER = OUTPUT_FOLDER / "decisions"

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.1-8b-instant"

MAX_CHUNK_CHARS = 60_000
MAX_RETRIES = 5
FORCE_REVIEW = False


# ============================================================
# CLIENT
# ============================================================

if not GROQ_API_KEY:
    raise ValueError("Missing GROQ_API_KEY environment variable.")

groq_client = Groq(api_key=GROQ_API_KEY)


# ============================================================
# PROMPT
# ============================================================

SYSTEM_PROMPT = """
You are the Synapse Blacklist Decision Engine.

You will receive ONLY metadata from a redflagged-conversation TXT file.

Each line has roughly this structure:
conversation_id | redflag_type | sensitivity_level | title | reason

You must decide whether each conversation ID should be blacklisted from all future model processing.

Return ONLY valid JSON.
No markdown.
No code fences.
No explanations outside JSON.

Schema:

{
  "decisions": [
    {
      "conversation_id": "string",
      "blacklist": true,
      "category": "secret | identity | financial | health | private | not_blacklisted",
      "reason": "string"
    }
  ]
}

Blacklist TRUE if the metadata indicates:
- actual API key, token, password, private key, session cookie, hardcoded credential
- bank account, IBAN, SWIFT, card number, salary slip, tax ID, financial account, portfolio values/holdings
- medical report, lab report, diagnosis with identifying context, prescription, hospital/doctor/patient record
- passport, Emirates ID, Aadhaar, SSN, visa document, birth certificate, driver license
- full name plus phone/email/address/school in resume/profile context
- property/legal documents with names and addresses
- live child location/image/audio
- private corporate breach investigation data

Blacklist FALSE if the metadata only indicates:
- coding project
- cybersecurity project
- RAT/malware/C2/UAC/reverse shell/exploit discussion
- phishing discussion
- security learning
- school work
- normal personal context
- generic health question without reports or identity
- generic finance question without account/portfolio details
- general schedule or timetable

Important:
- Do NOT blacklist just because something is malicious, unsafe, offensive, or cybersecurity-related.
- Blacklist only for catastrophic private/personal/financial/medical/identity/secret leakage risk.
- Do NOT include actual secrets or private details in the reason.
"""


# ============================================================
# HELPERS
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


def is_rate_limit_error(error):
    text = str(error).lower()

    return (
        "429" in text
        or "rate limit" in text
        or "rate_limit" in text
        or "rate_limit_exceeded" in text
        or "too many requests" in text
    )


def get_wait_seconds(error):
    text = str(error).lower()

    m = re.search(r"try again in ([0-9.]+)m([0-9.]+)s", text)
    if m:
        return int(float(m.group(1)) * 60 + float(m.group(2))) + 2

    m = re.search(r"try again in ([0-9.]+)s", text)
    if m:
        return int(float(m.group(1))) + 2

    m = re.search(r"try again in ([0-9.]+)m", text)
    if m:
        return int(float(m.group(1)) * 60) + 2

    return 60


def load_redflag_lines():
    if not REDFLAG_TXT.exists():
        raise FileNotFoundError(f"Missing file: {REDFLAG_TXT}")

    lines = []

    for raw_line in REDFLAG_TXT.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()

        if not line:
            continue

        # Keep only lines that begin with a UUID.
        if re.match(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\s*\|",
            line,
        ):
            lines.append(line)

    return lines


def split_lines_into_chunks(lines):
    CHUNKS_FOLDER.mkdir(parents=True, exist_ok=True)

    chunks = []
    current = []
    current_chars = 0

    for line in lines:
        if current and current_chars + len(line) > MAX_CHUNK_CHARS:
            chunks.append(current)
            current = []
            current_chars = 0

        current.append(line)
        current_chars += len(line)

    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks, start=1):
        path = CHUNKS_FOLDER / f"redflag_txt_chunk_{i:04d}.txt"
        path.write_text("\n".join(chunk), encoding="utf-8")

    return chunks


def decision_path(chunk_index):
    DECISIONS_FOLDER.mkdir(parents=True, exist_ok=True)
    return DECISIONS_FOLDER / f"groq_blacklist_decision_{chunk_index:04d}.json"


# ============================================================
# GROQ REVIEW
# ============================================================


def review_chunk_with_groq(chunk, chunk_index, total_chunks):
    path = decision_path(chunk_index)

    if path.exists() and not FORCE_REVIEW:
        print(f"Decision exists for chunk {chunk_index}. Loading.")
        return json.loads(path.read_text(encoding="utf-8"))

    user_payload = {
        "chunk_index": chunk_index,
        "total_chunks": total_chunks,
        "redflag_metadata_lines": chunk,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"Groq reviewing blacklist chunk {chunk_index}/{total_chunks}")
            print(f"Lines in chunk: {len(chunk)}")

            response = groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(user_payload, ensure_ascii=False),
                    },
                ],
                temperature=0.0,
                max_completion_tokens=8192,
                response_format={"type": "json_object"},
            )

            raw = response.choices[0].message.content
            cleaned = clean_json_text(raw)
            parsed = json.loads(cleaned)

            parsed["chunk_index"] = chunk_index
            parsed["total_chunks"] = total_chunks
            parsed["provider_used"] = "groq"
            parsed["model_used"] = GROQ_MODEL

            path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")

            return parsed

        except Exception as e:
            print(f"Groq error on chunk {chunk_index}, attempt {attempt}: {e}")

            if is_rate_limit_error(e):
                wait = get_wait_seconds(e)
                print(f"Groq rate-limited. Waiting {wait}s...")
                time.sleep(wait)
                continue

            if attempt == MAX_RETRIES:
                raise

            wait = min(30 * attempt, 180)
            print(f"Waiting {wait}s...")
            time.sleep(wait)

    raise RuntimeError("Groq review failed unexpectedly.")


# ============================================================
# OUTPUT
# ============================================================


def write_outputs(all_decisions):
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)

    blacklist = []
    not_blacklisted = []

    for d in all_decisions:
        conversation_id = d.get("conversation_id", "")
        if not conversation_id:
            continue

        if d.get("blacklist") is True:
            blacklist.append(d)
        else:
            not_blacklisted.append(d)

    blacklist_ids = sorted({d["conversation_id"] for d in blacklist})
    not_blacklisted_ids = sorted({d["conversation_id"] for d in not_blacklisted})

    (OUTPUT_FOLDER / "blacklist_ids.txt").write_text("\n".join(blacklist_ids), encoding="utf-8")

    (OUTPUT_FOLDER / "not_blacklisted_ids.txt").write_text(
        "\n".join(not_blacklisted_ids), encoding="utf-8"
    )

    (OUTPUT_FOLDER / "all_groq_blacklist_decisions.json").write_text(
        json.dumps(all_decisions, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary = {
        "total_decisions": len(all_decisions),
        "blacklisted": len(blacklist_ids),
        "not_blacklisted": len(not_blacklisted_ids),
        "blacklist_ids_file": str(OUTPUT_FOLDER / "blacklist_ids.txt"),
        "not_blacklisted_ids_file": str(OUTPUT_FOLDER / "not_blacklisted_ids.txt"),
    }

    (OUTPUT_FOLDER / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\nRESULTS")
    print("=" * 100)
    print(f"Total decisions: {len(all_decisions)}")
    print(f"Blacklisted: {len(blacklist_ids)}")
    print(f"Not blacklisted: {len(not_blacklisted_ids)}")
    print(f"Blacklist file: {OUTPUT_FOLDER / 'blacklist_ids.txt'}")
    print(f"Not blacklisted file: {OUTPUT_FOLDER / 'not_blacklisted_ids.txt'}")
    print("=" * 100)


# ============================================================
# MAIN
# ============================================================


def main():
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)

    print("\nGROQ BLACKLIST FROM REDFLAG TXT START")
    print("=" * 100)
    print(f"Input TXT: {REDFLAG_TXT}")
    print(f"Output folder: {OUTPUT_FOLDER}")
    print(f"Groq model: {GROQ_MODEL}")
    print("=" * 100)

    lines = load_redflag_lines()
    chunks = split_lines_into_chunks(lines)

    print(f"Loaded redflag metadata lines: {len(lines)}")
    print(f"Created chunks: {len(chunks)}")

    all_decisions = []

    for i, chunk in enumerate(chunks, start=1):
        result = review_chunk_with_groq(
            chunk=chunk,
            chunk_index=i,
            total_chunks=len(chunks),
        )

        decisions = result.get("decisions", [])
        all_decisions.extend(decisions)

    write_outputs(all_decisions)

    print("\nDONE")


if __name__ == "__main__":
    main()
