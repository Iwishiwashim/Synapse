import json
import re
import hashlib
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# =========================
# CONFIG
# =========================

EXPORT_FOLDER = r"C:\Users\Sandy\Documents\ChatGPT_Memories"

OUTPUT_FOLDER = "synapse_extracted"

# One Markdown file per conversation
WRITE_MARKDOWN = True

# Structured JSONL for AI sender
WRITE_JSONL = True


# =========================
# HELPERS
# =========================

def parse_timestamp(value):
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value)
    except Exception:
        return None


def format_time(dt):
    if not dt:
        return "Unknown time"
    return dt.strftime("%Y-%m-%d %H:%M")


def month_key(dt):
    if not dt:
        return "unknown-date"
    return dt.strftime("%Y-%m")


def safe_filename(text, max_length=90):
    text = str(text).strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text)
    text = text.strip("-")

    if not text:
        text = "untitled"

    return text[:max_length]


def short_hash(text, length=8):
    return hashlib.sha1(str(text).encode("utf-8", errors="ignore")).hexdigest()[:length]


def estimate_tokens(text):
    return max(1, len(text) // 4)


def load_conversations(export_path):
    single_file = export_path / "conversations.json"

    if single_file.exists():
        print("Loading conversations.json...")

        with open(single_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        print(f"Finished loading conversations.json ({len(data)} conversations)")
        return data

    split_files = sorted(export_path.glob("conversations-*.json"))

    if not split_files:
        raise FileNotFoundError(
            f"Could not find conversations.json or conversations-*.json in:\n{export_path}"
        )

    conversations = []

    for index, file_path in enumerate(split_files, start=1):
        print(f"[{index}/{len(split_files)}] Loading {file_path.name}...")

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            conversations.extend(data)
            count = len(data)
        else:
            conversations.append(data)
            count = 1

        print(f"[{index}/{len(split_files)}] Finished {file_path.name} ({count} conversations loaded)")

    print(f"\nFinished loading all files. Total conversations: {len(conversations)}")
    return conversations


def build_file_index(export_path):
    print("\nBuilding file index...")

    files = []

    for file in export_path.rglob("*"):
        if file.is_file():
            files.append(file.resolve())

    print(f"File index built. {len(files)} files found.")
    return files


def resolve_file_reference(file_index, ref):
    ref = str(ref)

    cleaned_refs = [
        ref,
        ref.replace("file-service://", ""),
        ref.replace("sandbox:/mnt/data/", ""),
        ref.replace("attachment://", ""),
    ]

    for cleaned in cleaned_refs:
        cleaned = cleaned.strip()

        if not cleaned:
            continue

        for file in file_index:
            if cleaned in file.name or file.name in cleaned:
                return str(file)

    return None


def extract_file_references(obj):
    refs = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in {"asset_pointer", "file_id", "name", "url"} and isinstance(value, str):
                refs.append(value)

            refs.extend(extract_file_references(value))

    elif isinstance(obj, list):
        for item in obj:
            refs.extend(extract_file_references(item))

    return refs


def extract_readable_text(obj):
    text_items = []

    if isinstance(obj, str):
        text_items.append(obj)

    elif isinstance(obj, list):
        for item in obj:
            text_items.extend(extract_readable_text(item))

    elif isinstance(obj, dict):
        for key, value in obj.items():
            if key in {"text", "content"} and isinstance(value, str):
                text_items.append(value)
            else:
                text_items.extend(extract_readable_text(value))

    return text_items


def get_role(message):
    author = message.get("author", {})
    role = author.get("role", "unknown")

    if role == "user":
        return "user"
    if role == "assistant":
        return "assistant"
    if role == "system":
        return "system"
    if role == "tool":
        return "tool"

    return str(role)


def readable_role(role):
    if role == "user":
        return "User"
    if role == "assistant":
        return "ChatGPT"
    if role == "system":
        return "System"
    if role == "tool":
        return "Tool"
    return str(role).title()


def extract_messages(conversation, file_index):
    mapping = conversation.get("mapping", {})
    messages = []

    for node_id, node in mapping.items():
        message = node.get("message")

        if not message:
            continue

        create_time = parse_timestamp(message.get("create_time"))
        content = message.get("content", {})
        content_type = content.get("content_type", "unknown")

        readable_parts = extract_readable_text(content)
        readable_text = "\n".join(
            str(part).strip()
            for part in readable_parts
            if str(part).strip()
        )

        if not readable_text:
            continue

        raw_refs = extract_file_references(message)
        files = []
        seen_paths = set()

        for ref in raw_refs:
            resolved_path = resolve_file_reference(file_index, ref)

            if resolved_path and resolved_path not in seen_paths:
                seen_paths.add(resolved_path)
                files.append({
                    "original_reference": ref,
                    "path": resolved_path
                })

        record = {
            "node_id": node_id,
            "timestamp": format_time(create_time),
            "timestamp_sort": create_time.timestamp() if create_time else 0,
            "role": get_role(message),
            "content_type": content_type,
            "content": readable_text,
            "files": files
        }

        messages.append(record)

    messages.sort(key=lambda x: x["timestamp_sort"])
    return messages


def build_clean_conversation_text(convo_record):
    lines = []

    lines.append(f"Conversation ID: {convo_record['conversation_id']}")
    lines.append(f"Title: {convo_record['title']}")
    lines.append(f"Created: {convo_record['created']}")
    lines.append(f"Updated: {convo_record['updated']}")
    lines.append("")

    for msg in convo_record["messages"]:
        role = readable_role(msg.get("role", "unknown"))
        timestamp = msg.get("timestamp", "Unknown time")
        content_type = msg.get("content_type", "unknown")
        node_id = msg.get("node_id", "unknown")
        content = msg.get("content", "").strip()

        lines.append(f"[{timestamp}] {role}")
        lines.append(f"Content type: {content_type}")
        lines.append(f"Node ID: {node_id}")
        lines.append(content)

        files = msg.get("files", [])
        if files:
            lines.append("")
            lines.append("Referenced files:")
            for file_info in files:
                lines.append(f"- Original reference: {file_info.get('original_reference', '')}")
                lines.append(f"  Actual export path: {file_info.get('path', '')}")

        lines.append("-" * 100)

    return "\n".join(lines)


def write_markdown_note(convo_record, clean_text, md_root):
    created_dt = convo_record["created_dt"]
    month = month_key(created_dt)

    month_folder = md_root / month
    month_folder.mkdir(parents=True, exist_ok=True)

    date_prefix = created_dt.strftime("%Y-%m-%d") if created_dt else "unknown-date"
    filename = f"{date_prefix}_{safe_filename(convo_record['title'])}_{short_hash(convo_record['conversation_id'])}.md"

    output_file = month_folder / filename

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("---\n")
        f.write('type: "chatgpt_conversation"\n')
        f.write(f'title: "{str(convo_record["title"]).replace(chr(34), chr(39))}"\n')
        f.write(f'conversation_id: "{convo_record["conversation_id"]}"\n')
        f.write(f'created: "{convo_record["created"]}"\n')
        f.write(f'updated: "{convo_record["updated"]}"\n')
        f.write(f'message_count: {len(convo_record["messages"])}\n')
        f.write(f'clean_chars: {len(clean_text)}\n')
        f.write(f'estimated_tokens: {estimate_tokens(clean_text)}\n')
        f.write("source: chatgpt_export\n")
        f.write("tags:\n")
        f.write("  - synapse\n")
        f.write("  - chatgpt-export\n")
        f.write("---\n\n")

        f.write(f"# {convo_record['title']}\n\n")
        f.write(f"**Conversation ID:** `{convo_record['conversation_id']}`  \n")
        f.write(f"**Created:** {convo_record['created']}  \n")
        f.write(f"**Updated:** {convo_record['updated']}  \n")
        f.write(f"**Messages:** {len(convo_record['messages'])}  \n")
        f.write(f"**Clean characters:** {len(clean_text):,}  \n")
        f.write(f"**Estimated tokens:** {estimate_tokens(clean_text):,}  \n\n")

        f.write("---\n\n")
        f.write(clean_text)
        f.write("\n")

    return str(output_file)


def main():
    export_path = Path(EXPORT_FOLDER).resolve()
    output_path = Path(OUTPUT_FOLDER).resolve()

    jsonl_folder = output_path / "conversations_jsonl"
    md_folder = output_path / "conversations_md"
    index_folder = output_path / "index"

    jsonl_folder.mkdir(parents=True, exist_ok=True)
    md_folder.mkdir(parents=True, exist_ok=True)
    index_folder.mkdir(parents=True, exist_ok=True)

    conversations = load_conversations(export_path)
    file_index = build_file_index(export_path)

    monthly_records = defaultdict(list)
    index_records = []

    total = len(conversations)

    print("\nExtracting conversations...")

    for index, conversation in enumerate(conversations, start=1):
        if index % 25 == 0:
            print(f"Processed {index}/{total} conversations...")

        conversation_id = conversation.get("id", "unknown-id")
        title = conversation.get("title", "Untitled Conversation")

        created_dt = parse_timestamp(conversation.get("create_time"))
        updated_dt = parse_timestamp(conversation.get("update_time"))

        messages = extract_messages(conversation, file_index)

        if not messages:
            continue

        convo_record = {
            "conversation_id": conversation_id,
            "title": title,
            "created": format_time(created_dt),
            "updated": format_time(updated_dt),
            "created_sort": created_dt.timestamp() if created_dt else 0,
            "updated_sort": updated_dt.timestamp() if updated_dt else 0,
            "created_dt": created_dt,
            "messages": messages
        }

        clean_text = build_clean_conversation_text(convo_record)

        convo_output_record = {
            "conversation_id": conversation_id,
            "title": title,
            "created": format_time(created_dt),
            "updated": format_time(updated_dt),
            "created_sort": created_dt.timestamp() if created_dt else 0,
            "updated_sort": updated_dt.timestamp() if updated_dt else 0,
            "message_count": len(messages),
            "clean_chars": len(clean_text),
            "estimated_tokens": estimate_tokens(clean_text),
            "clean_text": clean_text,
            "messages": messages
        }

        md_path = None
        if WRITE_MARKDOWN:
            md_path = write_markdown_note(convo_output_record | {"created_dt": created_dt}, clean_text, md_folder)

        month = month_key(created_dt)
        monthly_records[month].append(convo_output_record)

        index_records.append({
            "conversation_id": conversation_id,
            "title": title,
            "created": format_time(created_dt),
            "updated": format_time(updated_dt),
            "month": month,
            "message_count": len(messages),
            "clean_chars": len(clean_text),
            "estimated_tokens": estimate_tokens(clean_text),
            "markdown_path": md_path
        })

    print("\nFinished extracting.")
    print(f"Conversations extracted: {len(index_records)}")
    print(f"Months found: {len(monthly_records)}")

    if WRITE_JSONL:
        print("\nWriting monthly JSONL files...")

        for idx, (month, records) in enumerate(sorted(monthly_records.items()), start=1):
            output_file = jsonl_folder / f"{month}.jsonl"

            print(f"[{idx}/{len(monthly_records)}] Writing {output_file.name}...")

            with open(output_file, "w", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

            print(f"[{idx}/{len(monthly_records)}] FINISHED {output_file.name}")

    ranking = sorted(index_records, key=lambda x: x["clean_chars"], reverse=True)

    index_file = index_folder / "conversations_index.json"
    ranking_file = index_folder / "conversation_size_ranking.json"

    with open(index_file, "w", encoding="utf-8") as f:
        json.dump(index_records, f, ensure_ascii=False, indent=2)

    with open(ranking_file, "w", encoding="utf-8") as f:
        json.dump(ranking, f, ensure_ascii=False, indent=2)

    print("\nTOP 20 BIGGEST CONVERSATIONS")
    print("=" * 100)

    for i, item in enumerate(ranking[:20], start=1):
        print(
            f"{i}. {item['title']}\n"
            f"   ID: {item['conversation_id']}\n"
            f"   Characters: {item['clean_chars']:,}\n"
            f"   Estimated tokens: {item['estimated_tokens']:,}\n"
        )

    print("=" * 100)
    print("\nSYNAPSE EXTRACTION COMPLETE.")
    print(f"Output folder: {output_path}")
    print(f"JSONL folder: {jsonl_folder}")
    print(f"Markdown folder: {md_folder}")
    print(f"Index file: {index_file}")
    print(f"Ranking file: {ranking_file}")


if __name__ == "__main__":
    main()