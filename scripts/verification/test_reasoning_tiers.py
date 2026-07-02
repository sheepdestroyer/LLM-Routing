#!/usr/bin/env python3
"""
Verification script to test the 5-tier intent classification and routing pipeline
of the LLM-Routing gateway.

This script sends varying prompt complexities to trigger classification across
all 5 triage levels and validates the gateway's routing responses.
"""

import httpx
import json
import time

gateway_url = "http://127.0.0.1:5000/v1/chat/completions"
headers = {
    "Authorization": "Bearer gateway-pass",
    "Content-Type": "application/json"
}

prompts = [
    {
        "name": "Simple (1/5)",
        "prompt": "Write a one-line hello world in Python."
    },
    {
        "name": "Medium (2/5)",
        "prompt": "Refactor this Python function to add a docstring and use a type hint: def sum(a, b): return a + b"
    },
    {
        "name": "Complex (3/5)",
        "prompt": "Write a multi-file Python script implementing a complete ETL data pipeline that parses a complex nested JSON file of users and logs, validates the data with Pydantic, writes it to a PostgreSQL database using asyncpg, handles connection pool retries with backoff, and includes unit tests with pytest mocks."
    },
    {
        "name": "Reasoning (4/5)",
        "prompt": "Compare the design trade-offs of using Valkey/Redis Sentinel versus Valkey/Redis Cluster for high availability in a high-throughput, low-latency microservice architecture. Analyze failure detection mechanisms, failover times, read/write scaling, client complexity, and network partition (split-brain) scenarios."
    },
    {
        "name": "Advanced (5/5)",
        "prompt": "Design a highly secure, zero-trust microservice topology for a federated agent-routing system deployed across multiple Kubernetes clusters in different cloud regions. Incorporate mutual TLS via Istio service mesh, automated certificate rotation with cert-manager, OIDC/OAuth2 authentication via an external identity provider, centralized audit logging/distributed tracing using Langfuse and Clickhouse, regional failover routing based on latency, and DDoS protection."
    }
]

def run_tests():
    print("🚀 Starting 5-tier reasoning test queries...")
    print("=" * 60)
    for p in prompts:
        print(f"\n📂 Sending {p['name']} Prompt:")
        print(f"   Prompt: {p['prompt'][:100]}...")
        
        payload = {
            "model": "llm-routing-auto-free",
            "messages": [
                {"role": "user", "content": p["prompt"]}
            ],
            "temperature": 0.0,
            "max_tokens": 150
        }
        
        start_time = time.time()
        try:
            r = httpx.post(gateway_url, json=payload, headers=headers, timeout=120.0)
            elapsed = time.time() - start_time
            print(f"   Status: {r.status_code}")
            
            if r.status_code == 200:
                data = r.json()
                model_used = data.get("model", "unknown")
                try:
                    msg = data["choices"][0]["message"]
                    content = msg.get("content")
                    if content is not None:
                        content_clean = content.strip().replace("\n", " ")
                    else:
                        content_clean = "<empty content / tool call>"
                    print(f"   Routed Model: {model_used}")
                    print(f"   Response Preview: {content_clean[:120]}...")
                except Exception as parse_inner:
                    print(f"   Failed to parse response choices: {parse_inner}")
                    print(f"   Raw Data: {data}")
                print(f"   Latency: {elapsed:.2f}s")
            else:
                print(f"   Error: {r.text}")
        except Exception as e:
            print(f"   Request Exception: {e}")
            
if __name__ == "__main__":
    run_tests()
