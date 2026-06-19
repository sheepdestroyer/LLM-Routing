"""Retry the 94 failed prompts with 800-char truncation (safe for 4096-ctx model)."""
import json, urllib.request, time, subprocess, tempfile, os
from pathlib import Path
from collections import Counter

PROMPT_TEMPLATE = """Classify the coding task complexity. Output ONLY the tier name.

agent-simple-core: trivial one-liners, syntax fixes, single-line edits
agent-medium-core: single-function changes, light refactoring, simple tests
agent-complex-core: multi-file changes, algorithmic work, data pipelines
agent-reasoning-core: deep analysis, architecture decisions, debugging complex systems
agent-advanced-core: system-level architecture, cross-cutting concerns, novel design

Task: """

MAX_CHARS = 600  # proven safe across all prompt types in this dataset

def get_model_port():
    """Discover the gemma4-26a4b-routing model's direct port (bypass router prompt-cache bug)."""
    req = urllib.request.Request('http://127.0.0.1:8080/v1/models')
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
    for m in data.get('data', []):
        if 'gemma4-26a4b' in m.get('id', ''):
            status_obj = m.get('status') or {}
            args = status_obj.get('args', []) if isinstance(status_obj, dict) else []
            for i, v in enumerate(args):
                if v == '--port' and i + 1 < len(args):
                    return args[i + 1]
    raise RuntimeError("gemma4-26a4b-routing model port not found")

MODEL_PORT = get_model_port()
MODEL_URL = f"http://127.0.0.1:{MODEL_PORT}/v1/chat/completions"
print(f"Using model directly on port {MODEL_PORT}")

def classify(prompt):
    """Query the direct model port to classify the prompt complexity, handling truncations."""
    if len(prompt) > MAX_CHARS:
        prompt = prompt[:MAX_CHARS]
    payload = {
        "model": "gemma4-26a4b-routing",
        "messages": [{"role": "user", "content": PROMPT_TEMPLATE + prompt}],
        "temperature": 0.0,
        "max_tokens": 15,
    }
    req = urllib.request.Request(
        MODEL_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    choices = data.get("choices", [])
    content = ""
    if choices:
        content = choices[0].get("message", {}).get("content", "").strip()
    # Normalize: strip "tier:" prefix, extract just the tier name
    for tier in TIERS:
        if tier in content:
            return tier
    return "ERROR"

TIERS = ['agent-simple-core','agent-medium-core','agent-complex-core','agent-reasoning-core','agent-advanced-core']

data_dir = Path(__file__).resolve().parent.parent / "data"
with open(data_dir / "classified_dataset.json") as f:
    dataset = json.load(f)

def is_error_val(val):
    if not val:
        return False
    return str(val) == "ERROR" or str(val).startswith("ERROR:")

# Schema-aware: support both old schema ("tier") and new reclassify_all.py schema ("clf_tier")
error_indices = [
    i for i, p in enumerate(dataset.get('prompts', []))
    if is_error_val(p.get('tier')) or is_error_val(p.get('clf_tier'))
]
print(f"Retrying {len(error_indices)} failed prompts (max {MAX_CHARS} chars)...")

fixed = 0
errors = 0

for batch_start in range(0, len(error_indices), 5):
    batch = error_indices[batch_start:batch_start + 5]
    for idx in batch:
        prompts_list = dataset.get('prompts', [])
        prompt = prompts_list[idx].get('prompt') if idx < len(prompts_list) else ""
        try:
            tier = classify(prompt)
            if idx < len(prompts_list):
                prompts_list[idx]['tier'] = tier
                if 'clf_tier' in prompts_list[idx]:
                    prompts_list[idx]['clf_tier'] = tier
            if not is_error_val(tier):  # only count as fixed if classification succeeded
                fixed += 1
        except Exception as e:
            errors += 1
            print(f"  [{idx}] still failing: {str(e)[:80]}")
        time.sleep(3)  # single-slot server needs headroom
    if batch_start + 5 < len(error_indices):
        print(f"  batch {batch_start//5 + 1}/{(len(error_indices)+4)//5}: {fixed} fixed, {errors} errors")
        time.sleep(5)

from collections import Counter
new_counts = Counter(p.get('clf_tier') or p.get('tier') or p.get('llm_tier', 'ERROR') for p in dataset.get('prompts', []))
dataset['counts'] = {k: v for k, v in new_counts.items()}
dataset['gaps'] = [t for t in ['agent-simple-core','agent-medium-core','agent-complex-core','agent-reasoning-core','agent-advanced-core'] 
                   if new_counts.get(t, 0) < 20]

dest_path = data_dir / "classified_dataset.json"
with tempfile.NamedTemporaryFile("w", dir=str(data_dir), delete=False, encoding="utf-8") as tmp_f:
    json.dump(dataset, tmp_f, indent=2, ensure_ascii=False)
    tmp_name = tmp_f.name
os.replace(tmp_name, str(dest_path))

print(f"\nDone. Fixed: {fixed}, Errors: {errors}")
for tier in sorted(new_counts.keys()):
    print(f"  {tier:30s} {new_counts[tier]:3d}")