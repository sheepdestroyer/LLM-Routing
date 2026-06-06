#!/usr/bin/env python3
import os
import sys
import time
import json
import yaml
import urllib.request
import urllib.error

# Load config to get system prompt
CONFIG_PATH = os.getenv("CONFIG_PATH", "/home/gpav/Vrac/LAB/AI/LLM-Routing/router/config.yaml")
system_prompt = ""
if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, "r") as f:
            config = yaml.safe_load(f)
            system_prompt = config.get("classification_rules", {}).get("system_prompt", "")
    except Exception as e:
        print(f"Warning: Could not read config from {CONFIG_PATH}: {e}")

if not system_prompt:
    system_prompt = (
        "Analyze the user request complexity. Respond with exactly one of these identifiers:\n"
        "- If the request requires deep algorithmic logic, complex code refactoring, system architecture decisions, or complex multi-file tracing: return \"agent-complex-core\".\n"
        "- If the request is a simple syntax fix, file lookup, directory check, git message write, or repetitive boilerplate: return \"agent-simple-core\".\n"
        "Do not add markdown formatting or explanation. Only output the target model name string."
    )

# Labeled dataset of 25 diverse queries
test_cases = [
    # Simple prompts (agent-simple-core)
    ("Write a hello world in Python", "agent-simple-core"),
    ("Check if this directory exists", "agent-simple-core"),
    ("Write a git commit message for these changes", "agent-simple-core"),
    ("Print the current date and time in bash", "agent-simple-core"),
    ("How do I list files in a folder?", "agent-simple-core"),
    ("Create a new empty file named test.txt", "agent-simple-core"),
    ("What command is used to copy a file?", "agent-simple-core"),
    ("Rename document.docx to backup.docx", "agent-simple-core"),
    ("Show the git status", "agent-simple-core"),
    ("Write a simple regex to match email addresses", "agent-simple-core"),
    ("Define a helper function to calculate the square of a number", "agent-simple-core"),
    ("Delete all .tmp files in the current directory", "agent-simple-core"),
    ("Check if a package is installed using apt", "agent-simple-core"),

    # Complex prompts (agent-complex-core)
    ("Design a distributed pub/sub system with Valkey and describe failover states", "agent-complex-core"),
    ("Refactor this 500-line class to follow Clean Code principles and add unit tests", "agent-complex-core"),
    ("Implement a custom memory-efficient Trie data structure in C++ and analyze its space complexity", "agent-complex-core"),
    ("Troubleshoot a race condition in a multi-threaded Go web server handling WebSockets", "agent-complex-core"),
    ("Design a database schema for a multi-tenant e-commerce platform with row-level security", "agent-complex-core"),
    ("Write a Kubernetes deployment configuration for a microservices app with strict affinity rules", "agent-complex-core"),
    ("Optimize a slow PostgreSQL query with multiple joins, aggregations, and subqueries", "agent-complex-core"),
    ("Create a compiler frontend (lexer and parser) for a custom query language using ANTLR", "agent-complex-core"),
    ("Implement a secure OAuth2 login flow with refresh token rotation and PKCE in React", "agent-complex-core"),
    ("Refactor our monolithic legacy billing service into event-driven microservices", "agent-complex-core"),
    ("Analyze heap dump profiles to identify a memory leak in a Node.js production service", "agent-complex-core"),
    ("Write a Python script to perform semantic search on a dataset using vector embeddings and cosine similarity", "agent-complex-core")
]

# We support querying either llama-server directly or the router gateway
# Default is llama-server directly
LLAMA_SERVER_URL = "http://127.0.0.1:8080/v1/chat/completions"

def query_model(prompt: str) -> tuple[str, float]:
    payload = {
        "model": "qwen-0.8b-routing",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.0,
        "max_tokens": 15,
        "grammar": 'root ::= "agent-simple-core" | "agent-complex-core"'
    }
    
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        LLAMA_SERVER_URL,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer local-token"}
    )
    
    start_time = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            res_body = response.read().decode("utf-8")
            result = json.loads(res_body)
            content = result["choices"][0]["message"].get("content", "").strip()
            latency = (time.time() - start_time) * 1000.0
            return content, latency
    except Exception as e:
        latency = (time.time() - start_time) * 1000.0
        print(f"Error querying model for prompt '{prompt[:30]}...': {e}")
        return "ERROR", latency

def calculate_metrics(results):
    total = len(results)
    correct = sum(1 for r in results if r["expected"] == r["actual"])
    accuracy = (correct / total) * 100.0 if total > 0 else 0.0
    
    # Initialize metrics structure
    classes = ["agent-simple-core", "agent-complex-core"]
    metrics = {}
    
    for c in classes:
        tp = sum(1 for r in results if r["expected"] == c and r["actual"] == c)
        fp = sum(1 for r in results if r["expected"] != c and r["actual"] == c)
        fn = sum(1 for r in results if r["expected"] == c and r["actual"] != c)
        
        precision = (tp / (tp + fp)) if (tp + fp) > 0 else 0.0
        recall = (tp / (tp + fn)) if (tp + fn) > 0 else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
        
        metrics[c] = {
            "precision": precision * 100.0,
            "recall": recall * 100.0,
            "f1": f1 * 100.0,
            "tp": tp,
            "fp": fp,
            "fn": fn
        }
    
    return accuracy, metrics

def main():
    print(f"Starting Classifier Accuracy Evaluation Suite...")
    print(f"Querying endpoint: {LLAMA_SERVER_URL}")
    print(f"Loaded {len(test_cases)} test cases.")
    print("-" * 80)
    
    results = []
    latencies = []
    misclassifications = []
    
    for i, (prompt, expected) in enumerate(test_cases, 1):
        print(f"[{i}/{len(test_cases)}] Testing: '{prompt[:50]}...'")
        actual, latency = query_model(prompt)
        latencies.append(latency)
        
        success = (actual == expected)
        status = "✓ PASS" if success else "✗ FAIL"
        print(f"      Expected: {expected} | Actual: {actual} | Latency: {latency:.2f}ms | {status}")
        
        results.append({
            "prompt": prompt,
            "expected": expected,
            "actual": actual,
            "latency": latency
        })
        
        if not success:
            misclassifications.append({
                "prompt": prompt,
                "expected": expected,
                "actual": actual
            })
            
    print("=" * 80)
    accuracy, metrics = calculate_metrics(results)
    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
    
    print(f"OVERALL METRICS:")
    print(f"Classification Accuracy: {accuracy:.2f}%")
    print(f"Average Latency: {avg_latency:.2f} ms")
    print("-" * 80)
    
    for c, m in metrics.items():
        print(f"Class: {c}")
        print(f"  Precision: {m['precision']:.2f}%")
        print(f"  Recall:    {m['recall']:.2f}%")
        print(f"  F1-Score:  {m['f1']:.2f}%")
        print(f"  (TP={m['tp']}, FP={m['fp']}, FN={m['fn']})")
        print("-" * 80)
        
    if misclassifications:
        print(f"MISCLASSIFICATION LOGS ({len(misclassifications)} total):")
        for mc in misclassifications:
            print(f"Prompt:   '{mc['prompt']}'")
            print(f"Expected: {mc['expected']}")
            print(f"Actual:   {mc['actual']}")
            print("-" * 40)
    else:
        print("🎉 No misclassifications! Perfect accuracy!")
        
if __name__ == "__main__":
    main()
