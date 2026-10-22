import os
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient

async def main():
    client = AsyncIOMotorClient(os.environ.get("MONGODB_URL", "mongodb://localhost:27017"))
    dbs = await client.list_database_names()
    print("Databases:", dbs)
    db = client["nupun_health"]
    colls = await db.list_collection_names()
    print("Collections in nupun_health:", colls)
    
if __name__ == "__main__":
    asyncio.run(main())
