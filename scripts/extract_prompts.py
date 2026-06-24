"""Re-extract prompts using FIRST user message (not last) — fixes truncation."""
import base64
import json
import urllib.request
import urllib.error
import time
import re
from pathlib import Path

env = {}
env_path = Path(__file__).resolve().parent.parent / ".env"
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

pk = env['LANGFUSE_PUBLIC_KEY']
sk = env['LANGFUSE_SECRET_KEY']
auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
base_url = "http://localhost:3001"

# Trivial test prompts to skip
TRIVIAL_PATTERNS = [
    "say hello", "hi", "test", "ping", "hello", "hey", "what's up",
    "how are you", "good morning", "good afternoon", "good evening",
    "what model are you", "what llm are you", "who are you",
    "tell me a joke", "what is 2+2", "what is the capital",
]

def is_trivial(prompt):
    """Filter out test pings and trivial prompts using word boundaries to avoid partial matches."""
    lower = prompt.strip().lower()
    if len(lower) < 20:
        return True
    for pat in TRIVIAL_PATTERNS:
        if len(lower) < 50:
            escaped = re.escape(pat)
            if re.search(r'\b' + escaped + r'\b', lower):
                return True
    return False

def fetch_observations(page=1, limit=50):
    """Fetch one page of DEFAULT-level litellm-acompletion observations."""
    url = f"{base_url}/api/public/observations?limit={limit}&page={page}&orderBy=timestamp.desc&level=DEFAULT&name=litellm-acompletion"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

def extract_first_user_prompt(obs):
    """Extract the FIRST real user message (skip system notes)."""
    inp = obs.get('input')
    if not inp:
        return None
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except Exception:
            return None
    if not isinstance(inp, dict):
        return None
    messages = inp.get('messages', [])
    if not messages:
        return None
    
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get('role') != 'user':
            continue
        content = msg.get('content', '')
        if not isinstance(content, str) or len(content.strip()) <= 3:
            continue
        # Skip Hermes system notes injected as user messages
        stripped = content.strip()
        if stripped.startswith('[System:') or stripped.startswith('[Note:'):
            continue
        if stripped.startswith('[IMPORTANT:'):
            # Skill invocations — keep these, they're real prompts
            pass
        return stripped
    return None

print("Re-extracting prompts using FIRST user message...")
prompts = []
seen = set()
page = 1
target = 300
max_pages = 100

while len(prompts) < target and page <= max_pages:
    try:
        data = fetch_observations(page=page, limit=50)
    except Exception as e:
        print(f"  Page {page} failed: {e}")
        break
    
    obs_list = data.get('data', [])
    if not obs_list:
        print(f"  Page {page}: empty, stopping")
        break
    
    added = 0
    for obs in obs_list:
        if len(prompts) >= target:
            break
        
        prompt = extract_first_user_prompt(obs)
        if not prompt:
            continue
        if is_trivial(prompt):
            continue
        
        norm = prompt.strip().lower()
        if norm in seen:
            continue
        seen.add(norm)
        
        prompts.append({
            "prompt": prompt,
            "observation_id": obs.get('id', ''),
            "trace_id": obs.get('traceId', ''),
            "timestamp": obs.get('startTime', ''),
            "model": obs.get('model', ''),
        })
        added += 1
    
    print(f"  Page {page}: +{added} new → {len(prompts)} total")
    page += 1
    time.sleep(0.1)

out_dir = Path(__file__).resolve().parent.parent / "data"
out_path = out_dir / "raw_prompts_v2.json"
with open(out_path, 'w') as f:
    json.dump(prompts, f, indent=2, ensure_ascii=False)

print(f"\nSaved {len(prompts)} prompts to {out_path}")

lengths = [len(p['prompt']) for p in prompts]
if lengths:
    print(f"Length: min={min(lengths)}, max={max(lengths)}, median={sorted(lengths)[len(lengths)//2]}, avg={sum(lengths)/len(lengths):.0f}")
    print(f"Short (<100 chars): {sum(1 for length in lengths if length < 100)}")
    print("\nSample (first 10):")
    for p in prompts[:10]:
        print(f"  [{p['timestamp'][:19]}] ({len(p['prompt'])}c) {p['prompt'][:120]}...")
