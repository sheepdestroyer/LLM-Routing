"""Final gap-fill: deep extraction targeting complex + advanced tiers only."""
import os, base64, json, urllib.request, time
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

existing = set()
dataset_path = Path(__file__).resolve().parent.parent / "data" / "classified_dataset.json"
if dataset_path.exists():
    try:
        with open(dataset_path) as f:
            existing_data = json.load(f)
        for p in existing_data.get('prompts', []):
            existing.add(p['prompt'].strip().lower())
    except Exception as e:
        print(f"Warning: Failed to load existing dataset: {e}")

print(f"Already classified: {len(existing)} prompts")

def fetch_observations(page=1, limit=50):
    url = f"{base_url}/api/public/observations?limit={limit}&page={page}&orderBy=timestamp.desc&level=DEFAULT&name=litellm-acompletion"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

def extract_user_prompt(obs):
    inp = obs.get('input')
    if not inp: return None
    if isinstance(inp, str):
        try: inp = json.loads(inp)
        except: return None
    if not isinstance(inp, dict): return None
    messages = inp.get('messages', [])
    if not messages: return None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get('role') == 'user':
            content = msg.get('content', '')
            if isinstance(content, str) and len(content.strip()) > 3:
                return content.strip()
    return None

# Keywords suggesting complex/advanced work
COMPLEX_KEYWORDS = [
    'refactor', 'architect', 'design', 'implement', 'migrate', 'debug',
    'diagnose', 'review', 'analyze', 'optimize', 'restructure', 'pipeline',
    'system', 'module', 'framework', 'pattern', 'strategy', 'algorithm',
    'multi-tenant', 'distributed', 'sharding', 'consensus', 'isolation',
    'security', 'vulnerability', 'deadlock', 'concurrent', 'scale',
    'infrastructure', 'deploy', 'orchestrate', 'integrate', 'protocol',
]

def looks_complex(prompt):
    """Heuristic: longer prompts with complex keywords."""
    lower = prompt.lower()
    if len(prompt) < 200:
        return False
    score = sum(1 for kw in COMPLEX_KEYWORDS if kw in lower)
    return score >= 2

print("Deep extraction: targeting complex/advanced prompts...")
prompts = []
seen = set()
page = 1
target = 50
max_pages = 200  # go deep into history

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
        if not prompt: continue
        norm = prompt.strip().lower()
        if norm in seen: continue
        if norm in existing: continue
        if not looks_complex(prompt): continue
        
        seen.add(norm)
        prompts.append({
            "prompt": prompt,
            "observation_id": obs.get('id', ''),
            "trace_id": obs.get('traceId', ''),
            "timestamp": obs.get('startTime', ''),
        })
        added += 1
    
    print(f"  Page {page}: +{added} new → {len(prompts)} total")
    page += 1
    time.sleep(0.1)

out_path = Path(__file__).resolve().parent.parent / "data" / "raw_prompts_complex.json"
with open(out_path, 'w') as f:
    json.dump(prompts, f, indent=2, ensure_ascii=False)

print(f"\nSaved {len(prompts)} complex prompts to {out_path}")
lengths = [len(p['prompt']) for p in prompts]
if lengths:
    print(f"Length range: {min(lengths)}-{max(lengths)} chars, avg: {sum(lengths)/len(lengths):.0f}")
    for p in prompts[:5]:
        print(f"  [{p['timestamp'][:19]}] ({len(p['prompt'])} chars) {p['prompt'][:120]}...")
else:
    print("No prompts collected.")
