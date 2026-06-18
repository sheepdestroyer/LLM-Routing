"""Re-run gemma4 classifier (with grammar) on all dataset prompts via router."""
import json, urllib.request, time, sys
from pathlib import Path
from collections import Counter

TIERS = ['agent-simple-core','agent-medium-core','agent-complex-core','agent-reasoning-core','agent-advanced-core']

ROUTER_URL = "http://127.0.0.1:8080/v1/chat/completions"
print(f"Using router on {ROUTER_URL}")

PROMPT_TEMPLATE = """Analyze the request complexity. Respond with exactly one of:
- simple boilerplate: agent-simple-core
- moderate complexity: agent-medium-core
- deep algorithms: agent-complex-core
- heavy multi-step reasoning: agent-reasoning-core
- system-level / novel design: agent-advanced-core

Request: """

def classify(prompt):
    if len(prompt) > 600:
        prompt = prompt[:600]
    payload = {
        'model': 'gemma4-26a4b-routing',
        'messages': [{'role': 'user', 'content': PROMPT_TEMPLATE + prompt}],
        'max_tokens': 15, 'temperature': 0,
        'grammar': 'root ::= "agent-simple-core" | "agent-medium-core" | "agent-complex-core" | "agent-reasoning-core" | "agent-advanced-core"'
    }
    req = urllib.request.Request(ROUTER_URL, data=json.dumps(payload).encode(), headers={'Content-Type':'application/json','Authorization':'Bearer local-token'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data['choices'][0]['message'].get('content','').strip()

# Load existing dataset (kanban/llm evals)
data_dir = Path(__file__).resolve().parent.parent / 'data'
with open(data_dir / 'classified_dataset.json') as f:
    dataset = json.load(f)

# Load raw prompts for full text
with open(data_dir / 'raw_prompts_hermes.json') as f:
    all_prompts = json.load(f)

# Build prompt lookup
prompt_map = {}
for p in all_prompts:
    prompt_map[p['prompt']] = p

print(f"Classifying {len(dataset['prompts'])} prompts with gemma4-26a4b (grammar-enforced)...")

results = []
for i, item in enumerate(dataset['prompts']):
    prompt = item['prompt']
    
    # Original LLM/kanban eval
    llm_tier = item.get('tier', '?')
    
    # Classifier eval
    try:
        clf_tier = classify(prompt)
    except Exception as e:
        clf_tier = f"ERROR: {str(e)[:50]}"
    
    results.append({
        'prompt': prompt,
        'llm_tier': llm_tier,
        'clf_tier': clf_tier,
        'session_id': item.get('session_id', ''),
    })
    
    if (i + 1) % 30 == 0:
        agree = sum(1 for r in results if r['llm_tier'] == r['clf_tier'])
        print(f"  {i+1}/{len(dataset['prompts'])} — {agree}/{i+1} agree ({agree/(i+1)*100:.0f}%)")
        sys.stdout.flush()

# Stats
clf_counts = Counter(r['clf_tier'] for r in results)
llm_counts = Counter(r['llm_tier'] for r in results)
agree = sum(1 for r in results if r['llm_tier'] == r['clf_tier'])

print(f"\n{'='*60}")
print(f"Agreement: {agree}/{len(results)} ({agree/len(results)*100:.1f}%)")
print(f"\nTier distribution:")
print(f"{'Tier':30s} {'LLM':>6s} {'CLF':>6s} {'Δ':>6s}")
for t in TIERS:
    lc = llm_counts.get(t, 0)
    cc = clf_counts.get(t, 0)
    print(f"  {t:30s} {lc:>6d} {cc:>6d} {cc-lc:>+6d}")

# Save combined dataset
combined = {
    'total': len(results),
    'agreement': round(agree / len(results) * 100, 1),
    'llm_counts': dict(llm_counts),
    'clf_counts': dict(clf_counts),
    'prompts': results,
}

with open(data_dir / 'classified_dataset.json', 'w') as f:
    json.dump(combined, f, indent=2, ensure_ascii=False)

print(f"\nSaved to classified_dataset.json (now with llm_tier + clf_tier)")
