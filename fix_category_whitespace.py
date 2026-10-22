import os
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient

MONGO = os.environ.get("MONGODB_URL", "mongodb://localhost:27017")


async def main():
    db = AsyncIOMotorClient(MONGO)["nupun_health"]

    # 1) Trim leading/trailing whitespace on category names.
    cat_fixed = 0
    async for c in db["categories"].find({}):
        name = c.get("name")
        if isinstance(name, str) and name != name.strip():
            await db["categories"].update_one(
                {"_id": c["_id"]}, {"$set": {"name": name.strip()}}
            )
            cat_fixed += 1
            print(f"category: |{name}| -> |{name.strip()}|")

    # 2) Trim category_name on services so they match the cleaned category.
    svc_fixed = 0
    async for s in db["services"].find({}):
        cn = s.get("category_name")
        if isinstance(cn, str) and cn != cn.strip():
            await db["services"].update_one(
                {"_id": s["_id"]}, {"$set": {"category_name": cn.strip()}}
            )
            svc_fixed += 1
            print(f"service: |{cn}| -> |{cn.strip()}|  ({s.get('title')})")

    print(f"\nDone. categories fixed: {cat_fixed}, services fixed: {svc_fixed}")


if __name__ == "__main__":
    asyncio.run(main())
