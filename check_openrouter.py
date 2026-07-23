import asyncio
import httpx
import json

async def main():
    async with httpx.AsyncClient() as client:
        r = await client.get("https://openrouter.ai/api/v1/models")
        data = r.json().get("data", [])
        for m in data:
            if ":free" in m.get("id", ""):
                print(json.dumps(m, indent=2))
                break

if __name__ == "__main__":
    asyncio.run(main())
