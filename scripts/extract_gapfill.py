"""Gap-fill extraction: pull longer/older prompts targeting complex+ tiers."""
import os, base64, json, urllib.request, time, re
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

# Load already-classified prompts to skip
existing = set()
dataset_path = Path(__file__).resolve().parent.parent / "data" / "classified_dataset.json"
if dataset_path.exists():
    with open(dataset_path) as f:
        existing_data = json.load(f)
    for p in existing_data.get('prompts', []):
        if 'prompt' in p and p['prompt']:
            existing.add(p['prompt'].strip().lower())

print(f"Already classified: {len(existing)} prompts")

def fetch_observations(page=1, limit=50):
    """Fetch DEFAULT-level litellm-acompletion observations."""
    url = f"{base_url}/api/public/observations?limit={limit}&page={page}&orderBy=timestamp.desc&level=DEFAULT&name=litellm-acompletion"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

def extract_user_prompt(obs):
    """Extract and parse the FIRST real user prompt from the observation input payload.

    Uses forward iteration (not reversed) to return the first user message,
    matching the semantics of extract_prompts.py. Skips pseudo-user system notes.
    """
    inp = obs.get('input')
    if not inp:
        return None
    if isinstance(inp, str):
        try: inp = json.loads(inp)
        except: return None
    if not isinstance(inp, dict):
        return None
    messages = inp.get('messages', [])
    if not messages:
        return None
    for msg in messages:  # forward iteration: first user message
        if isinstance(msg, dict) and msg.get('role') == 'user':
            content = msg.get('content', '')
            if not isinstance(content, str) or len(content.strip()) <= 3:
                continue
            stripped = content.strip()
            # Skip Hermes system notes injected as user messages
            if stripped.startswith('[System:') or stripped.startswith('[Note:'):
                continue
            return stripped
    return None

def is_trivial(prompt):
    """Check if the prompt matches a list of trivial test patterns to filter out."""
    lower = prompt.strip().lower()
    if len(lower) < 20:
        return True
    trivial = ["say hello", "hi", "test", "ping", "hello", "hey", "what's up",
               "how are you", "good morning", "what model are you", "who are you",
               "tell me a joke", "what is 2+2", "what is the capital"]
    for pat in trivial:
        if len(lower) < 50:
            escaped = re.escape(pat)
            if re.search(r'\b' + escaped + r'\b', lower):
                return True
    return False

print("Extracting gap-fill prompts (longer, older, targeting complex+)...")
prompts = []
seen = set()
page = 1
target = 80  # generous — workers will filter further
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
        
        prompt = extract_user_prompt(obs)
        if not prompt:
            continue
        if is_trivial(prompt):
            continue
        
        norm = prompt.strip().lower()
        if norm in seen:
            continue
        if norm in existing:
            continue
        
        # Bias toward complex: prefer longer prompts (>200 chars)
        # but don't exclude shorter ones entirely
        if len(prompt) < 100 and len(prompts) > 40:
            continue  # after 40 collected, only take substantial prompts
        
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
out_path = out_dir / "raw_prompts_gapfill.json"
with open(out_path, 'w') as f:
    json.dump(prompts, f, indent=2, ensure_ascii=False)

print(f"\nSaved {len(prompts)} gap-fill prompts to {out_path}")
lengths = [len(p['prompt']) for p in prompts]
if lengths:
    print(f"Length range: {min(lengths)}-{max(lengths)} chars, avg: {sum(lengths)/len(lengths):.0f}")
    print(f"Sample:")
    for p in prompts[:5]:
        print(f"  [{p['timestamp'][:19]}] ({len(p['prompt'])} chars) {p['prompt'][:100]}...")
else:
    print("No prompts collected.")
