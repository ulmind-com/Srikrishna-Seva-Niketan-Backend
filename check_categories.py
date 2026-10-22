import os
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient

async def main():
    client = AsyncIOMotorClient(os.environ.get("MONGODB_URL", "mongodb://localhost:27017"))
    db = client["nupun_health"]
    cats = await db["categories"].find().to_list(100)
    print(f"Found {len(cats)} categories")
    for c in cats:
        print(c.get("name"), c.get("slug"))

if __name__ == "__main__":
    asyncio.run(main())
