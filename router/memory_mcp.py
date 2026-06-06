#!/usr/bin/env python3
import sys
import json
import httpx

# Logging to stderr so it doesn't pollute stdout (which is used for JSON-RPC)
def log(msg):
    sys.stderr.write(f"[memory-mcp] {msg}\n")
    sys.stderr.flush()

API_URL = "http://127.0.0.1:5000/v1/memory"

def handle_request(req):
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params", {})
    
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {}
            },
            "serverInfo": {
                "name": "litellm-memory-bridge",
                "version": "1.0.0"
            }
        }
        
    elif method == "tools/list":
        return {
            "tools": [
                {
                    "name": "rememberMemory",
                    "description": "Stores a new preference or factual memory.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "Unique key to store the memory under."},
                            "value": {"type": "string", "description": "The content/value of the memory."}
                        },
                        "required": ["key", "value"]
                    }
                },
                {
                    "name": "retrieveMemories",
                    "description": "Retrieves stored memories by prefix.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "keyPrefix": {"type": "string", "description": "Optional prefix to filter memory keys (e.g., 'user:')."}
                        }
                    }
                },
                {
                    "name": "removeSpecificMemory",
                    "description": "Deletes a specific stored memory by its key.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "The key of the memory to delete."}
                        },
                        "required": ["key"]
                    }
                }
            ]
        }
        
    elif method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments", {})
        
        try:
            if tool_name == "rememberMemory":
                key = args.get("key")
                value = args.get("value")
                # Make POST request to triage router
                r = httpx.post(API_URL, json={"key": key, "value": value}, timeout=10.0)
                if r.status_code == 200:
                    text = f"Successfully saved memory '{key}'."
                else:
                    text = f"Error saving memory: {r.text}"
                return {
                    "content": [{"type": "text", "text": text}]
                }
                
            elif tool_name == "retrieveMemories":
                prefix = args.get("keyPrefix", "")
                url = f"{API_URL}?key_prefix={prefix}" if prefix else API_URL
                r = httpx.get(url, timeout=10.0)
                if r.status_code == 200:
                    data = r.json()
                    memories = data.get("memories", [])
                    if not memories:
                        text = "No memories found."
                    else:
                        text = json.dumps(memories, indent=2)
                else:
                    text = f"Error retrieving memories: {r.text}"
                return {
                    "content": [{"type": "text", "text": text}]
                }
                
            elif tool_name == "removeSpecificMemory":
                key = args.get("key")
                url = f"{API_URL}/{key}"
                r = httpx.delete(url, timeout=10.0)
                if r.status_code == 200:
                    text = f"Successfully deleted memory '{key}'."
                else:
                    text = f"Error deleting memory: {r.text}"
                return {
                    "content": [{"type": "text", "text": text}]
                }
            else:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}]
                }
        except Exception as e:
            log(f"Error executing tool {tool_name}: {e}")
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"Error: {e}"}]
            }
            
    return None

def main():
    log("Memory MCP Server Bridge started.")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            req_id = req.get("id")
            
            # Notifications don't have IDs and don't expect responses
            if req_id is None:
                # Handle initialize notification
                continue
                
            res_result = handle_request(req)
            if res_result is not None:
                response = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": res_result
                }
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
        except Exception as e:
            log(f"Error parsing line: {e}")

if __name__ == "__main__":
    main()
