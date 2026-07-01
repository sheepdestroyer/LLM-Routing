"""Benchmark gemma4-26a4b-routing classifier against labeled dataset."""
import os
import concurrent.futures
import threading
import json, urllib.request, time, sys
from collections import defaultdict, Counter
from pathlib import Path

# Load dataset
dataset_path = Path(__file__).resolve().parent.parent / "data" / "classified_dataset.json"
with open(dataset_path) as f:
    dataset = json.load(f)

# Classifier prompt (same as router/config.yaml)
PROMPT_TEMPLATE = """Classify the coding task complexity. Output ONLY the tier name.

agent-simple-core: trivial one-liners, syntax fixes, single-line edits
agent-medium-core: single-function changes, light refactoring, simple tests
agent-complex-core: multi-file changes, algorithmic work, data pipelines
agent-reasoning-core: deep analysis, architecture decisions, debugging complex systems
agent-advanced-core: system-level architecture, cross-cutting concerns, novel design

Task: """

TIERS = [
    "agent-simple-core", "agent-medium-core", "agent-complex-core",
    "agent-reasoning-core", "agent-advanced-core"
]

def classify(prompt):
    """Call gemma4-26a4b-routing via llama-server."""
    payload = {
        "model": "gemma4-26a4b-routing",
        "messages": [{"role": "user", "content": PROMPT_TEMPLATE + prompt}],
        "temperature": 0.0,
        "max_tokens": 15,
    }
    req = urllib.request.Request(
        "http://127.0.0.1:8080/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.environ.get('ROUTER_API_KEY', 'local-token')}"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    choices = data.get("choices", [])
    if not choices:
        return "ERROR"
    return choices[0].get("message", {}).get("content", "").strip()

total = len(dataset.get("prompts", []))
print(f"Benchmark: gemma4-26a4b-routing vs {total} labeled prompts\n")

# Run classification
results = []
correct = 0
per_tier = {t: {"correct": 0, "total": 0} for t in TIERS}
confusion = defaultdict(Counter)  # confusion[expected][predicted]


def process_item(item):
    prompt = item["prompt"]
    # Support both old schema ("tier") and new schema ("llm_tier" / "clf_tier")
    expected = item.get("tier") or item.get("llm_tier") or item.get("clf_tier", "")

    try:
        predicted = classify(prompt)
    except Exception as e:
        predicted = f"ERROR: {str(e)[:50]}"

    return expected, predicted

results_list = [None] * total

with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
    # Submit all tasks immediately, but executor map will process them and yield results in order
    # To maintain rate limit properly without the quadratic delay or blocking progress, we can use a threading.Lock
    rate_limit_lock = threading.Lock()
    
    def process_item_with_rate_limit(index_and_item):
        i, item = index_and_item
        # Enforce rate limit globally across all threads
        with rate_limit_lock:
            time.sleep(0.05)

        expected, predicted = process_item(item)
        return i, item, expected, predicted

    # We map over items, but map blocks if we process in order. We can use as_completed and store by index to preserve order.
    futures = [executor.submit(process_item_with_rate_limit, (i, item)) for i, item in enumerate(dataset.get("prompts", []))]
    
    completed_count = 0
    for future in concurrent.futures.as_completed(futures):
        i, item, expected, predicted = future.result()

        results_list[i] = {
            "prompt": item["prompt"][:100],
            "expected": expected,
            "predicted": predicted,
        }

        # Only score against known tiers — skip ERROR/unknown labels gracefully
        if expected not in per_tier:
            confusion[expected][predicted] += 1
        else:
            per_tier[expected]["total"] += 1
            if predicted == expected:
                correct += 1
                per_tier[expected]["correct"] += 1
            confusion[expected][predicted] += 1

        completed_count += 1

        # Progress
        if completed_count % 20 == 0:
            scored_so_far = sum(t["total"] for t in per_tier.values())
            acc = (correct / scored_so_far * 100) if scored_so_far > 0 else 0.0
            print(f"  {completed_count}/{total} — accuracy {acc:.1f}%")

# Filter out Nones if any
results = [r for r in results_list if r is not None]

# Report
scored_total = sum(t["total"] for t in per_tier.values())
overall = (correct / scored_total * 100) if scored_total > 0 else 0.0

print(f"\n{'='*60}")
print(f"Overall accuracy: {correct}/{scored_total} ({overall:.1f}%)")
print(f"{'='*60}")

print(f"\nPer-tier accuracy:")
for tier in TIERS:
    t = per_tier[tier]
    pct = t["correct"] / t["total"] * 100 if t["total"] > 0 else 0
    bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
    print(f"  {tier:30s} {t['correct']:3d}/{t['total']:3d}  {bar} {pct:.1f}%")

print(f"\nConfusion matrix (expected → predicted):")
header = " " * 30 + "".join(f"{t:25s}" for t in TIERS)
print(header)
for exp_tier in TIERS:
    row = f"{exp_tier:30s}"
    for pred_tier in TIERS:
        count = confusion[exp_tier].get(pred_tier, 0)
        count_str = f"{count:3d}"
        if exp_tier == pred_tier:
            cell = f"  \033[32m{count_str}\033[0m"
            row += f"{cell:34s}"
        elif count > 0:
            cell = f"  \033[31m{count_str}\033[0m"
            row += f"{cell:34s}"
        else:
            cell = f"  {count_str}"
            row += f"{cell:25s}"
    print(row)

# Save detailed results
out_path = Path(__file__).resolve().parent.parent / "data" / "benchmark_results.json"
with open(out_path, 'w') as f:
    json.dump({
        "classifier": "gemma4-26a4b-routing",
        "dataset_total": total,
        "overall_accuracy": round(overall, 1),
        "per_tier": {t: {
            "correct": per_tier[t]["correct"],
            "total": per_tier[t]["total"],
            "accuracy": round(per_tier[t]["correct"] / per_tier[t]["total"] * 100, 1) if per_tier[t]["total"] > 0 else 0
        } for t in TIERS},
        "confusion": {t: dict(confusion[t]) for t in TIERS},
        "details": results,
    }, f, indent=2, ensure_ascii=False)

print(f"\nDetailed results saved to {out_path}")
