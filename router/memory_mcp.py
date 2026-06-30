#!/usr/bin/env python3
"""
Goose Memory MCP Server — PostgreSQL-backed via LiteLLM triage router.

Replaces the built-in file-based Memory extension with a persistent
PostgreSQL-backed version. Memories survive restarts and are accessible
across all Goose sessions.

Tool names match the built-in Memory MCP exactly:
  - remember_memory(category, data, tags, is_global)
  - retrieve_memories(category, is_global)
  - remove_memory_category(category, is_global)
  - remove_specific_memory(category, memory_content, is_global)
"""
import sys
import json
import time
import httpx
import hashlib

API_URL = "http://127.0.0.1:5000/v1/memory"
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "litellm-memory-bridge"
SERVER_VERSION = "2.0.0"

# ---------------------------------------------------------------------------
# Key helpers — encode memory attributes into a single LiteLLM key
# ---------------------------------------------------------------------------
# Format: memory:{scope}:{category}::{timestamp}:{content_hash}
# Examples:
#   memory:global:development_standards::1717612345::a1b2c3d4
#   memory:local:api_config::1717612389::e5f6g7h8

SCOPE_GLOBAL = "global"
SCOPE_LOCAL = "local"
PREFIX = "memory"


def _make_key(category: str, is_global: bool, data: str) -> str:
    """Build a unique key from memory attributes."""
    scope = SCOPE_GLOBAL if is_global else SCOPE_LOCAL
    ts = int(time.time() * 1000)
    # BLAKE2b: SOTA crypto hash, stdlib, faster than MD5, deterministic across restarts
    h = hashlib.blake2b(f"{data if data is not None else ''}{ts}".encode("utf-8"), digest_size=6).hexdigest()
    return f"{PREFIX}:{scope}:{category}::{ts}:{h}"


def _parse_key(key: str):
    """Parse a structured key back into (scope, category, timestamp, hash)."""
    try:
        parts = key.split("::")
        prefix = parts[0].split(":")  # memory:{scope}:{category}
        scope = prefix[1] if len(prefix) > 1 else ""
        category = prefix[2] if len(prefix) > 2 else ""
        ts_hash = parts[1] if len(parts) > 1 else ""
        ts = ts_hash.split(":")[0] if ts_hash else ""
        return {"scope": scope, "category": category, "timestamp": ts}
    except Exception:
        return {"scope": "", "category": "", "timestamp": ""}


# ---------------------------------------------------------------------------
# Helpers for "memory" keys — memories stored as a single key with
# json-encoded metadata so we can support tags and categories.
# ---------------------------------------------------------------------------

def _is_memory_key(key: str) -> bool:
    """Check if a key follows the memory:{scope}:{category}:: format."""
    return key.startswith(f"{PREFIX}:")


def _memory_value(data: str, tags: list | None) -> str:
    """Encode data + tags into the stored value JSON."""
    payload = {"data": data, "tags": tags or []}
    return json.dumps(payload, ensure_ascii=False)


def _parse_memory_value(raw: str) -> dict:
    """Decode stored value back into {data, tags}."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"data": raw, "tags": []}


# ---------------------------------------------------------------------------
# MCP handlers
# ---------------------------------------------------------------------------

async def _list_all_memories(client: httpx.AsyncClient) -> list[dict]:
    """Fetch all memories from LiteLLM."""
    r = await client.get(API_URL, timeout=10.0)
    if r.status_code != 200:
        return []
    data = r.json()
    return data.get("memories", [])


def _memory_entry(lmem: dict) -> dict | None:
    """Convert a LiteLLM memory entry into a structured MCP memory object.

    Returns None if the key isn't a 'memory:' key.
    """
    key = lmem.get("key", "")
    if not _is_memory_key(key):
        return None
    raw_value = lmem.get("value", "")
    parsed = _parse_key(key)
    meta = _parse_memory_value(raw_value)
    # Determine if this is the most recent entry for the category
    return {
        "key": key,
        "category": parsed["category"],
        "data": meta["data"],
        "tags": meta["tags"],
        "scope": parsed["scope"],
        "timestamp": parsed["timestamp"],
        "memory_id": lmem.get("memory_id", ""),
    }


async def handle_remember_memory(args: dict) -> str:
    """remember_memory(category, data, tags, is_global)"""
    category = args.get("category", "general")
    data = args.get("data", "")
    tags = args.get("tags")
    is_global = args.get("is_global", False)

    key = _make_key(category, is_global, data)
    value = _memory_value(data, tags)
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(API_URL, json={"key": key, "value": value})
        if r.status_code == 200:
            res = r.json()
            scope_label = "global" if is_global else "local"
            tag_str = f" with tags {tags}" if tags else ""
            return (
                f"Stored in:\n"
                f"    - Category: {category}\n"
                f"    - Tags: {tags or 'none'}\n"
                f"    - Scope: {scope_label}\n\n"
                f"I'll remember this and apply it when relevant in this scope."
            )
        else:
            return f"Error saving memory: {r.text}"


async def handle_retrieve_memories(args: dict) -> str:
    """retrieve_memories(category, is_global)

    Use category="*" to retrieve all memories.
    """
    category = args.get("category", "*")
    is_global = args.get("is_global", False)

    async with httpx.AsyncClient(timeout=10.0) as client:
        all_memories = await _list_all_memories(client)

    # Filter
    scope = SCOPE_GLOBAL if is_global else SCOPE_LOCAL
    results = []
    for m in all_memories:
        entry = _memory_entry(m)
        if entry is None:
            continue
        # Scope match
        if entry["scope"] != scope:
            continue
        # Category match: "*" means all, otherwise exact match
        if category != "*" and entry["category"] != category:
            continue
        results.append(entry)

    if not results:
        scope_label = "global" if is_global else "local"
        return f"No memories found for category '{category}' ({scope_label})."

    # Group by category for display
    by_category = {}
    for r in results:
        by_category.setdefault(r["category"], []).append(r)

    lines = []
    for cat, entries in sorted(by_category.items()):
        lines.append(f"\nCategory: {cat}")
        for e in entries:
            tag_str = f" [{', '.join(e['tags'])}]" if e.get("tags") else ""
            lines.append(f"  - {e['data']}{tag_str}")

    return "\n".join(lines).strip()


async def handle_remove_memory_category(args: dict) -> str:
    """remove_memory_category(category, is_global)

    Use category="*" to remove all memories in the scope.
    """
    category = args.get("category", "*")
    is_global = args.get("is_global", False)

    async with httpx.AsyncClient(timeout=10.0) as client:
        all_memories = await _list_all_memories(client)

    scope = SCOPE_GLOBAL if is_global else SCOPE_LOCAL
    to_delete = []
    for m in all_memories:
        entry = _memory_entry(m)
        if entry is None:
            continue
        if entry["scope"] != scope:
            continue
        if category == "*" or entry["category"] == category:
            to_delete.append(entry)

    if not to_delete:
        scope_label = "global" if is_global else "local"
        return f"No memories found to remove in category '{category}' ({scope_label})."

    async with httpx.AsyncClient(timeout=30.0) as client:
        for entry in to_delete:
            key = entry["key"]
            await client.delete(f"{API_URL}/{key}", timeout=5.0)

    scope_label = "global" if is_global else "local"
    cat_label = f"category '{category}'" if category != "*" else "all categories"
    return f"Removed {len(to_delete)} memory(ies) from {cat_label} ({scope_label})."


async def handle_remove_specific_memory(args: dict) -> str:
    """remove_specific_memory(category, memory_content, is_global)"""
    category = args.get("category", "")
    memory_content = args.get("memory_content", "")
    is_global = args.get("is_global", False)

    async with httpx.AsyncClient(timeout=10.0) as client:
        all_memories = await _list_all_memories(client)

    scope = SCOPE_GLOBAL if is_global else SCOPE_LOCAL
    target = None

    for m in all_memories:
        entry = _memory_entry(m)
        if entry is None:
            continue
        if entry["scope"] != scope:
            continue
        if category and entry["category"] != category:
            continue
        if entry["data"] == memory_content or memory_content in entry["data"]:
            target = entry
            break

    if not target:
        scope_label = "global" if is_global else "local"
        return (
            f"No matching memory found in category '{category}' ({scope_label}) "
            f"with content matching '{memory_content[:50]}...'."
        )

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.delete(f"{API_URL}/{target['key']}", timeout=5.0)
        if r.status_code == 200:
            return f"Removed memory in category '{category}' ({target['data'][:60]}...)."
        else:
            return f"Error removing memory: {r.text}"


# ---------------------------------------------------------------------------
# JSON-RPC dispatcher
# ---------------------------------------------------------------------------

def log(msg: str):
    sys.stderr.write(f"[memory-mcp] {msg}\n")
    sys.stderr.flush()


async def handle_request(req: dict) -> dict | None:
    method = req.get("method")
    params = req.get("params", {})

    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {}
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION
            }
        }

    elif method == "tools/list":
        return {
            "tools": [
                {
                    "name": "remember_memory",
                    "description": (
                        "Store information with a category, optional tags, "
                        "and scope (local/global). Memories are persisted "
                        "in PostgreSQL and survive restarts."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "description": "Category to store the memory under."
                            },
                            "data": {
                                "type": "string",
                                "description": "The content/value of the memory."
                            },
                            "tags": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional tags for categorization."
                            },
                            "is_global": {
                                "type": "boolean",
                                "description": "Global (true) or project-local (false)."
                            }
                        },
                        "required": ["category", "data"]
                    }
                },
                {
                    "name": "retrieve_memories",
                    "description": (
                        "Retrieve memories by category. Use \"*\" to retrieve all. "
                        "Memories are fetched from PostgreSQL."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "description": "Category to retrieve. Use \"*\" for all."
                            },
                            "is_global": {
                                "type": "boolean",
                                "description": "Global (true) or project-local (false)."
                            }
                        },
                        "required": ["category"]
                    }
                },
                {
                    "name": "remove_memory_category",
                    "description": (
                        "Remove all memories in a category. Use \"*\" to clear all."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "description": "Category to clear. Use \"*\" for all."
                            },
                            "is_global": {
                                "type": "boolean",
                                "description": "Global (true) or project-local (false)."
                            }
                        },
                        "required": ["category"]
                    }
                },
                {
                    "name": "remove_specific_memory",
                    "description": (
                        "Remove a single memory by matching its content within "
                        "a category."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "description": "Category the memory belongs to."
                            },
                            "memory_content": {
                                "type": "string",
                                "description": "Content text to match for deletion."
                            },
                            "is_global": {
                                "type": "boolean",
                                "description": "Global (true) or project-local (false)."
                            }
                        },
                        "required": ["category", "memory_content"]
                    }
                }
            ]
        }

    elif method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments", {})

        log(f"Calling tool: {tool_name}")

        try:
            if tool_name == "remember_memory":
                text = await handle_remember_memory(args)
            elif tool_name == "retrieve_memories":
                text = await handle_retrieve_memories(args)
            elif tool_name == "remove_memory_category":
                text = await handle_remove_memory_category(args)
            elif tool_name == "remove_specific_memory":
                text = await handle_remove_specific_memory(args)
            else:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}]
                }

            return {
                "content": [{"type": "text", "text": text}]
            }
        except Exception as e:
            log(f"Error executing {tool_name}: {e}")
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"Error: {e}"}]
            }

    return None


# ---------------------------------------------------------------------------
# Main loop — JSON-RPC over stdio
# ---------------------------------------------------------------------------

async def main_loop():
    log("LiteLLM Memory MCP Bridge v2 started (PostgreSQL-backed).")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            req_id = req.get("id")

            # Notifications have no ID — skip response
            if req_id is None:
                continue

            result = await handle_request(req)
            if result is not None:
                response = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": result
                }
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError as e:
            log(f"JSON parse error: {e}")
        except Exception as e:
            log(f"Unexpected error: {e}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main_loop())
