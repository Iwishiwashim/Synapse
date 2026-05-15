import sys, json
sys.stdout.reconfigure(encoding="utf-8")
from server.config import load_config
from server.ai_importer import import_ai_export

config = load_config()
result = import_ai_export(config, r"C:\Users\Sandy\Documents\ChatGPT_Memories")

if "error" in result:
    print("ERROR:", result["error"])
elif result.get("action_required"):
    print("ACTION NEEDED:", result["message"])
else:
    patches = result.get("proposals", [])
    provider = result["provider"]
    owner = result.get("owner_detected")
    chunks = result["chunks_processed"]
    print(f"Provider: {provider}")
    print(f"Owner: {owner}")
    print(f"Chunks: {chunks}")
    print(f"Patches extracted: {len(patches)}")
    with open("chatgpt_patches.json", "w", encoding="utf-8") as f:
        json.dump(patches, f, ensure_ascii=False, indent=2)
    print("Saved to chatgpt_patches.json")
