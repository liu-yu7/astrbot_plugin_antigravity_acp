#!/usr/bin/env python3
"""Create the Antigravity provider source / model entries in AstrBot's cmd_config.json.

The WebUI can do the same interactively (服务提供商 → 新增 → antigravity_acp);
this script is for headless setups. Run it while AstrBot is stopped: AstrBot
keeps the config in memory and rewrites the file. A timestamped backup is
written next to the config before any change.

    agy_configure.py --config ~/astrbot/data/cmd_config.json --source --proxy http://127.0.0.1:7890
    agy_configure.py --config ~/astrbot/data/cmd_config.json --model gemini-3.8-flash-medium
"""

import argparse
import json
import shutil
import time

SOURCE_ID = "antigravity"

ap = argparse.ArgumentParser()
ap.add_argument("--config", required=True, help="path to AstrBot's data/cmd_config.json")
ap.add_argument("--source", action="store_true", help="create/update the provider source")
ap.add_argument("--proxy", default=None, help="HTTP proxy for the agy process, e.g. http://127.0.0.1:7890")
ap.add_argument("--agy-path", default=None, help="absolute path of the agy binary (default: auto-detect)")
ap.add_argument("--thought-level", default=None, help="default thinking level: low | medium | high")
ap.add_argument("--model", action="append", default=[], help="create a model entry under the source (repeatable)")
ap.add_argument("--max-context", type=int, default=512000, help="initial max_context_tokens; the plugin synchronizes its configured context limit")
args = ap.parse_args()

with open(args.config, encoding="utf-8-sig") as handle:
    config = json.load(handle)
before = json.dumps(config, sort_keys=True)

if args.source:
    template = {
        "id": SOURCE_ID,
        "provider": "antigravity",
        "type": "antigravity_acp",
        "provider_type": "chat_completion",
        "enable": True,
        "key": [],
        "timeout": 300,
        "proxy": "",
        "agy_path": "",
        "agy_home": "",
        "agy_thought_level": "",
        "agy_expose_tools": True,
        "agy_max_sessions": 6,
        "agy_idle_ttl": 900,
    }
    sources = config.setdefault("provider_sources", [])
    source = next((s for s in sources if s.get("id") == SOURCE_ID), None)
    if source is None:
        source = template
        sources.append(source)
        print(f"added provider source {SOURCE_ID}")
    else:
        for key, value in template.items():
            source.setdefault(key, value)
        print(f"provider source {SOURCE_ID} already present")
    if args.proxy is not None:
        source["proxy"] = args.proxy
    if args.agy_path is not None:
        source["agy_path"] = args.agy_path
    if args.thought_level is not None:
        source["agy_thought_level"] = args.thought_level

for model in args.model:
    providers = config.setdefault("provider", [])
    entry_id = f"{SOURCE_ID}/{model}"
    if any(p.get("id") == entry_id for p in providers):
        print(f"model entry {entry_id} already present")
        continue
    providers.append(
        {
            "id": entry_id,
            "enable": True,
            "provider_source_id": SOURCE_ID,
            "model": model,
            "modalities": ["text", "image", "tool_use"],
            "custom_extra_body": {},
            "max_context_tokens": args.max_context,
        }
    )
    print(f"added model entry {entry_id}")

if json.dumps(config, sort_keys=True) == before:
    print("no changes")
else:
    backup = f"{args.config}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(args.config, backup)
    with open(args.config, "w", encoding="utf-8-sig") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False)
    print(f"config written (backup: {backup})")
