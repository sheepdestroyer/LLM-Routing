"""Direct classification of Hermes prompts using gemma4-26a4b-routing."""
import os
import json, urllib.request, time
from pathlib import Path

# Shared chat response parser (used by verification scripts too)
from scripts.chat_helpers import parse_chat_response

PROMPT_TEMPLATE = """Classify the coding task complexity. Output ONLY the tier name.

agent-simple-core: trivial one-liners, syntax fixes, single-line edits
agent-medium-core: single-function changes, light refactoring, simple tests
agent-complex-core: multi-file changes, algorithmic work, data pipelines
agent-reasoning-core: deep analysis, architecture decisions, debugging complex systems
agent-advanced-core: system-level architecture, cross-cutting concerns, novel design

Task: """
LLAMA_SERVER_URL = "http://127.0.0.1:8080/v1/chat/completions"
TIERS = {
    "agent-simple-core", "agent-medium-core", "agent-complex-core",
    "agent-reasoning-core", "agent-advanced-core"
}

def classify(prompt):
    """Query the llama-server to classify the prompt task complexity."""
    payload = {
        "model": "gemma4-26a4b-routing",
        "messages": [{"role": "user", "content": PROMPT_TEMPLATE + prompt}],
        "temperature": 0.0,
        "max_tokens": 15,
    }
    req = urllib.request.Request(
        LLAMA_SERVER_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.environ.get('ROUTER_API_KEY', 'local-token')}"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    content, _ = parse_chat_response(data)
    return content if content else "ERROR"

# Load prompts
data_dir = Path(__file__).resolve().parent.parent / "data"
with open(data_dir / "raw_prompts_hermes.json") as f:
    prompts = json.load(f)

print(f"Classifying {len(prompts)} prompts with gemma4-26a4b-routing...")

results = []
errors = 0
for i, p in enumerate(prompts):
    prompt = p['prompt']
    # Truncate very long prompts to classifier context window (~3500 chars safe margin)
    if len(prompt) > 3500:
        prompt = prompt[:3500]
    
    try:
        raw_tier = classify(prompt)
        if raw_tier in TIERS:
            tier = raw_tier
            extra = {}
        else:
            tier = "ERROR"
            extra = {"raw_output": raw_tier}
            errors += 1
        results.append({"id": i, "tier": tier, "prompt_snippet": prompt[:60], **extra})
    except Exception as e:
        tier = f"ERROR"
        errors += 1
        results.append({"id": i, "tier": tier, "error": str(e)[:100]})
        print(f"  [{i}] ERROR: {str(e)[:80]}")
    
    if (i + 1) % 30 == 0:
        print(f"  {i+1}/{len(prompts)} — {errors} errors")
    time.sleep(0.05)

# Count tiers
from collections import Counter
counts = Counter(r["tier"] for r in results)

print(f"\nDone. {len(results)} classified, {errors} errors")
print(f"Counts:")
for tier in ['agent-simple-core', 'agent-medium-core', 'agent-complex-core', 'agent-reasoning-core', 'agent-advanced-core']:
    c = counts.get(tier, 0)
    print(f"  {tier}: {c}")
error_count = sum(1 for r in results if r["tier"] == "ERROR")
if error_count:
    print(f"  ERROR: {error_count}")

# Build dataset
dataset_prompts = []
for p, r in zip(prompts, results):
    dataset_prompts.append({
        "prompt": p['prompt'],
        "tier": r['tier'],
        "classifier": "uuid",
        "session_id": p.get('session_id', ''),
    })

dataset = {
    "prompts": dataset_prompts,
    "counts": dict(counts),
    "total": len(dataset_prompts),
    "gaps": [t for t in ['agent-simple-core','agent-medium-core','agent-complex-core','agent-reasoning-core','agent-advanced-core'] 
              if counts.get(t, 0) < 20]
}

out_path = data_dir / "classified_dataset.json"
with open(out_path, 'w') as f:
    json.dump(dataset, f, indent=2, ensure_ascii=False)

print(f"\nSaved to {out_path}")
print(f"Gaps: {dataset['gaps']}")