#!/usr/bin/env python3
import asyncio
import httpx
import json
import time

async def main():
    url = "http://localhost:5000/v1/chat/completions"
    payload = {
        "model": "agent-complex-core",
        "messages": [
            {"role": "user", "content": "Write a 50-word story about a spaceship"}
        ],
        "stream": True
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer gateway-pass"
    }

    print("=" * 60)
    print("Testing Stream Latency (TTFT Verification)")
    print("=" * 60)
    print("Sending streaming request to triage router on port 5000...")

    start_time = time.time()
    first_token_time = None
    chunks_received = 0
    full_response = []

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    print(f"❌ Error: Triage router returned status code {response.status_code}")
                    text = await response.aread()
                    print(text.decode('utf-8', errors='replace'))
                    return

                async for chunk in response.aiter_bytes():
                    # Parse the SSE data format
                    # data: {"choices": [{"delta": {"content": "..."}}]}
                    lines = chunk.decode('utf-8', errors='replace').split('\n')
                    for line in lines:
                        if not line.strip():
                            continue
                        if line.startswith("data: [DONE]"):
                            continue
                        if line.startswith("data:"):
                            try:
                                data_str = line[5:].strip()
                                data = json.loads(data_str)
                                choices = data.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    content = delta.get("content", "")
                                    if content:
                                        chunks_received += 1
                                        if first_token_time is None:
                                            first_token_time = time.time()
                                            ttft = (first_token_time - start_time) * 1000
                                            print(f"🚀 Time-To-First-Token (TTFT): {ttft:.0f} ms")

                                        full_response.append(content)
                                        # Print character to show live streaming
                                        print(content, end="", flush=True)
                            except Exception as e:
                                # Ignore parse errors for partial chunks
                                pass

        end_time = time.time()
        elapsed = end_time - start_time
        print("\n\nStream Finished!")
        print(f"Total time: {elapsed:.2f} s")
        print(f"Total chunks received: {chunks_received}")
        print(f"Story length: {len(''.join(full_response))} characters")

        # Verify TTFT is within acceptable limits for a streamed response (typically <3-4s, unlike the 8s+ legacy TTFT)
        if first_token_time is not None:
            ttft_ms = (first_token_time - start_time) * 1000
            if ttft_ms < 6000:
                print("✅ TTFT is under 6 seconds! Streaming is working progressively.")
            else:
                print("⚠️  TTFT is high, check daemon buffering.")
        else:
            print("❌ No tokens received in stream.")

    except Exception as e:
        print(f"❌ Exception occurred: {e}")

if __name__ == "__main__":
    asyncio.run(main())
