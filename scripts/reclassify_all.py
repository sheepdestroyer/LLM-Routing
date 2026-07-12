"""Re-run gemma4 classifier (with grammar) on all dataset prompts via router."""
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from collections import Counter

# Shared chat response parser (used by verification scripts too)
try:
    from scripts.chat_helpers import parse_chat_response
except ImportError:
    from chat_helpers import parse_chat_response

TIERS = ['agent-simple-core','agent-medium-core','agent-complex-core','agent-reasoning-core','agent-advanced-core']

LLAMA_SERVER_URL = "http://127.0.0.1:8080/v1/chat/completions"
print(f"Using llama-server on {LLAMA_SERVER_URL}")

PROMPT_TEMPLATE = """Analyze the request complexity. Respond with exactly one of:
- simple boilerplate: agent-simple-core
- moderate complexity: agent-medium-core
- deep algorithms: agent-complex-core
- heavy multi-step reasoning: agent-reasoning-core
- system-level / novel design: agent-advanced-core

Request: """

def classify(prompt):
    """Query the llama-server to classify the prompt complexity with grammar enforcement."""
    payload = {
        'model': 'qwen-4b-routing',
        'messages': [{'role': 'user', 'content': PROMPT_TEMPLATE + prompt}],
        'max_tokens': 15, 'temperature': 0,
        'grammar': 'root ::= "agent-simple-core" | "agent-medium-core" | "agent-complex-core" | "agent-reasoning-core" | "agent-advanced-core"'
    }
    req = urllib.request.Request(LLAMA_SERVER_URL, data=json.dumps(payload).encode(), headers={'Content-Type':'application/json','Authorization': f'Bearer {os.environ.get("ROUTER_API_KEY", "local-token")}'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    content, _ = parse_chat_response(data)
    return content if content else "ERROR: empty response"

# Load existing dataset (kanban/llm evals)
data_dir = Path(__file__).resolve().parent.parent / 'data'
with open(data_dir / 'classified_dataset.json') as f:
    dataset = json.load(f)


print(f"Classifying {len(dataset['prompts'])} prompts with qwen-4b-routing (grammar-enforced)...")

results = []
for i, item in enumerate(dataset['prompts']):
    prompt = item['prompt']
    
    # Original LLM/kanban eval
    llm_tier = item.get('llm_tier') or item.get('tier', '?')
    
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

total_results = len(results)
print(f"\n{'='*60}")
if total_results > 0:
    print(f"Agreement: {agree}/{total_results} ({agree/total_results*100:.1f}%)")
else:
    print("Agreement: 0/0 (0.0%)")
print("\nTier distribution:")
print(f"{'Tier':30s} {'LLM':>6s} {'CLF':>6s} {'Δ':>6s}")
for t in TIERS:
    lc = llm_counts.get(t, 0)
    cc = clf_counts.get(t, 0)
    print(f"  {t:30s} {lc:>6d} {cc:>6d} {cc-lc:>+6d}")

# Save combined dataset atomically
combined = {
    'total': total_results,
    'agreement': round(agree / total_results * 100, 1) if total_results > 0 else 0.0,
    'llm_counts': dict(llm_counts),
    'clf_counts': dict(clf_counts),
    'prompts': results,
}

dest_path = data_dir / 'classified_dataset.json'
tmp_name = None
try:
    with tempfile.NamedTemporaryFile('w', dir=str(data_dir), delete=False, encoding='utf-8') as tmp_f:
        tmp_name = tmp_f.name
        json.dump(combined, tmp_f, indent=2, ensure_ascii=False)
    os.replace(tmp_name, str(dest_path))
    tmp_name = None
finally:
    if tmp_name and os.path.exists(tmp_name):
        try:
            os.unlink(tmp_name)
        except Exception:
            pass

print("\nSaved to classified_dataset.json (now with llm_tier + clf_tier)")
