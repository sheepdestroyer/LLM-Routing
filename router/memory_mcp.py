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
    # Use first 12 chars of a basic hash for uniqueness within the same second
    h = str(hash(data + str(ts)))[:12].replace("-", "x")
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
    if not key.startswith(f"{PREFIX}:"):
        return False
    parts = key.split(":")
    if len(parts) < 3:
        return False
    return True


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


def _memory_entry(lmem: dict) -> dict | None:
    """Convert a litellm memory entry into our standard dictionary."""
    key = lmem.get("key")
    if not key or not _is_memory_key(key):
        return None

    raw_val = lmem.get("value")
    if not raw_val:
        return {
            "key": key,
            "data": "",
            "tags": [],
            "category": _parse_key(key)["category"],
            "scope": _parse_key(key)["scope"],
            "timestamp": _parse_key(key)["timestamp"],
            "memory_id": lmem.get("memory_id", ""),
        }

    parsed_val = _parse_memory_value(raw_val)
    # Handle the case where the JSON is not a dict
    if not isinstance(parsed_val, dict):
         parsed_val = {"data": parsed_val, "tags": []}

    meta = _parse_key(key)
    return {
        "key": key,
        "data": parsed_val.get("data", ""),
        "tags": parsed_val.get("tags", []),
        "category": meta["category"],
        "scope": meta["scope"],
        "timestamp": meta["timestamp"],
        "memory_id": lmem.get("memory_id", ""),
    }

# ---------------------------------------------------------------------------
# Core implementations for each tool
# ---------------------------------------------------------------------------

def imp_remember(category: str, data: str, tags: list | None, is_global: bool) -> str:
    """Save a new memory via Litellm add_memory."""
    key = _make_key(category, is_global, data)
    val = _memory_value(data, tags)
    payload = {
        "key": key,
        "value": val
    }
    
    try:
        res = httpx.post(f"{API_URL}/add", json=payload, timeout=10.0)
        res.raise_for_status()
        return f"Memory saved in category '{category}'."
    except Exception as e:
        return f"Failed to save memory: {e}"


def imp_retrieve(category: str | None, is_global: bool | None) -> str:
    """Retrieve matching memories via Litellm get_memory."""
    try:
        res = httpx.get(f"{API_URL}/get", timeout=10.0)
        res.raise_for_status()
        body = res.json()

        # litellm returns a list in memory_items or a dict directly.
        # It's usually a list of dicts.
        items = body.get("memories", []) if isinstance(body, dict) else body
        if not isinstance(items, list):
            return "Unexpected response from memory service."

        matches = []
        for lmem in items:
            entry = _memory_entry(lmem)
            if not entry:
                continue

            # Filter by is_global
            if is_global is True and entry["scope"] != SCOPE_GLOBAL:
                continue
            if is_global is False and entry["scope"] != SCOPE_LOCAL:
                continue

            # Filter by category
            if category and entry["category"] != category:
                continue

            matches.append(entry)

        if not matches:
            return f"No memories found matching the criteria."

        # Format output similar to original MCP
        out = []
        for m in matches:
            tag_str = f" [tags: {', '.join(m['tags'])}]" if m['tags'] else ""
            out.append(f"- {m['data']}{tag_str} (category: {m['category']})")
        return "\n".join(out)

    except Exception as e:
        return f"Failed to retrieve memories: {e}"


def imp_remove_category(category: str, is_global: bool | None) -> str:
    """Delete all memories in a category via Litellm delete_memory."""
    try:
        # First, retrieve all matches so we have their keys/ids
        res = httpx.get(f"{API_URL}/get", timeout=10.0)
        res.raise_for_status()
        body = res.json()
        items = body.get("memories", []) if isinstance(body, dict) else body

        to_delete = []
        for lmem in items:
            entry = _memory_entry(lmem)
            if not entry:
                continue
            if entry["category"] != category:
                continue
            if is_global is True and entry["scope"] != SCOPE_GLOBAL:
                continue
            if is_global is False and entry["scope"] != SCOPE_LOCAL:
                continue

            to_delete.append(entry["memory_id"])

        if not to_delete:
            return f"No memories found in category '{category}'."

        # Delete them all
        count = 0
        for mid in to_delete:
            if not mid: continue
            payload = {"memory_id": mid}
            d_res = httpx.post(f"{API_URL}/delete", json=payload, timeout=10.0)
            if d_res.status_code in (200, 204):
                count += 1

        return f"Deleted {count} memories from category '{category}'."

    except Exception as e:
        return f"Failed to clear category: {e}"


def imp_remove_specific(category: str, memory_content: str, is_global: bool | None) -> str:
    """Delete a specific memory by content substring."""
    try:
        res = httpx.get(f"{API_URL}/get", timeout=10.0)
        res.raise_for_status()
        body = res.json()
        items = body.get("memories", []) if isinstance(body, dict) else body

        mid_to_delete = None
        for lmem in items:
            entry = _memory_entry(lmem)
            if not entry:
                continue
            if entry["category"] != category:
                continue
            if is_global is True and entry["scope"] != SCOPE_GLOBAL:
                continue
            if is_global is False and entry["scope"] != SCOPE_LOCAL:
                continue

            # Substring match on the actual data text
            if memory_content in entry["data"]:
                mid_to_delete = entry["memory_id"]
                break

        if not mid_to_delete:
            return f"No memory containing '{memory_content}' found in category '{category}'."

        payload = {"memory_id": mid_to_delete}
        d_res = httpx.post(f"{API_URL}/delete", json=payload, timeout=10.0)
        d_res.raise_for_status()

        return f"Memory successfully deleted."

    except Exception as e:
        return f"Failed to delete memory: {e}"

# ---------------------------------------------------------------------------
# Protocol loop
# ---------------------------------------------------------------------------
def run_loop():
    """Main communication loop reading from stdin and writing to stdout."""
    def respond(msg_id: str | int, result_dict: dict | None = None, error_dict: dict | None = None):
        res = {"jsonrpc": "2.0", "id": msg_id}
        if result_dict is not None:
            res["result"] = result_dict
        elif error_dict is not None:
            res["error"] = error_dict
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue

        method = req.get("method")
        msg_id = req.get("id")

        if not method or msg_id is None:
            continue

        if method == "initialize":
            # Handshake
            respond(msg_id, result_dict={
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION
                },
                "capabilities": {
                    "tools": {}
                }
            })

        elif method == "notifications/initialized":
            pass

        elif method == "tools/list":
            # Define tools matching the exact signatures expected
            respond(msg_id, result_dict={
                "tools": [
                    {
                        "name": "remember_memory",
                        "description": "Store a new memory. Memories are shared across all sessions.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "category": {"type": "string", "description": "Category group name"},
                                "data": {"type": "string", "description": "The information to remember"},
                                "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags"},
                                "is_global": {"type": "boolean", "description": "Must be true"}
                            },
                            "required": ["category", "data"]
                        }
                    },
                    {
                        "name": "retrieve_memories",
                        "description": "Retrieve stored memories.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "category": {"type": "string"},
                                "is_global": {"type": "boolean"}
                            }
                        }
                    },
                    {
                        "name": "remove_memory_category",
                        "description": "Delete all memories in a category.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "category": {"type": "string"},
                                "is_global": {"type": "boolean"}
                            },
                            "required": ["category"]
                        }
                    },
                    {
                        "name": "remove_specific_memory",
                        "description": "Delete a specific memory.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "category": {"type": "string"},
                                "memory_content": {"type": "string"},
                                "is_global": {"type": "boolean"}
                            },
                            "required": ["category", "memory_content"]
                        }
                    }
                ]
            })

        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})

            if name == "remember_memory":
                out = imp_remember(
                    args.get("category", ""),
                    args.get("data", ""),
                    args.get("tags"),
                    args.get("is_global", True)
                )
                respond(msg_id, result_dict={"content": [{"type": "text", "text": out}]})

            elif name == "retrieve_memories":
                out = imp_retrieve(
                    args.get("category"),
                    args.get("is_global")
                )
                respond(msg_id, result_dict={"content": [{"type": "text", "text": out}]})

            elif name == "remove_memory_category":
                out = imp_remove_category(
                    args.get("category", ""),
                    args.get("is_global")
                )
                respond(msg_id, result_dict={"content": [{"type": "text", "text": out}]})

            elif name == "remove_specific_memory":
                out = imp_remove_specific(
                    args.get("category", ""),
                    args.get("memory_content", ""),
                    args.get("is_global")
                )
                respond(msg_id, result_dict={"content": [{"type": "text", "text": out}]})

            else:
                respond(msg_id, error_dict={"code": -32601, "message": f"Method not found: {name}"})

        else:
            respond(msg_id, error_dict={"code": -32601, "message": "Method not supported"})

if __name__ == "__main__":
    run_loop()
