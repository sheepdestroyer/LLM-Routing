"""Extract 300 meaningful coding prompts from Langfuse observations."""
import os, base64, json, urllib.request, urllib.error, sys, time
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
    """Filter out test pings and trivial prompts."""
    lower = prompt.strip().lower()
    if len(lower) < 20:
        return True
    for pat in TRIVIAL_PATTERNS:
        if pat in lower and len(lower) < 50:
            return True
    return False

def fetch_observations(page=1, limit=50):
    """Fetch one page of DEFAULT-level litellm-acompletion observations."""
    url = f"{base_url}/api/public/observations?limit={limit}&page={page}&orderBy=timestamp.desc&level=DEFAULT&name=litellm-acompletion"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

def extract_user_prompt(obs):
    """Extract the last user message from an observation's input."""
    inp = obs.get('input')
    if not inp:
        return None
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except:
            return None
    if not isinstance(inp, dict):
        return None
    messages = inp.get('messages', [])
    if not messages:
        return None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get('role') == 'user':
            content = msg.get('content', '')
            if isinstance(content, str) and len(content.strip()) > 3:
                return content.strip()
    return None

print("Extracting meaningful coding prompts from Langfuse observations...")
prompts = []
seen = set()
page = 1
target = 300
max_pages = 50  # 50 pages × 50 = 2500 observations

while len(prompts) < target and page <= max_pages:
    try:
        data = fetch_observations(page=page, limit=50)
    except Exception as e:
        print(f"  Page {page} failed: {e}")
        break
    
    obs_list = data.get('data', [])
    total_available = data.get('meta', {}).get('totalItems', 0)
    
    if not obs_list:
        print(f"  Page {page}: empty, stopping")
        break
    
    added_this_page = 0
    for obs in obs_list:
        if len(prompts) >= target:
            break
        
        prompt = extract_user_prompt(obs)
        if not prompt:
            continue
        
        # Skip trivial test pings
        if is_trivial(prompt):
            continue
        
        # Deduplicate
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
        added_this_page += 1
    
    print(f"  Page {page}: {len(obs_list)} obs, +{added_this_page} new → {len(prompts)} total (of {total_available} available)")
    page += 1
    time.sleep(0.1)  # gentle rate limit

# Save
out_dir = Path(__file__).resolve().parent.parent / "data"
out_dir.mkdir(exist_ok=True)
out_path = out_dir / "raw_prompts.json"

with open(out_path, 'w') as f:
    json.dump(prompts, f, indent=2, ensure_ascii=False)

print(f"\nSaved {len(prompts)} prompts to {out_path}")

# Stats
lengths = [len(p['prompt']) for p in prompts]
print(f"Length range: {min(lengths)}-{max(lengths)} chars")
print(f"Avg length: {sum(lengths)/len(lengths):.0f} chars")
print(f"\nSample (first 10):")
for p in prompts[:10]:
    print(f"  [{p['timestamp'][:19]}] {p['prompt'][:120]}...")
