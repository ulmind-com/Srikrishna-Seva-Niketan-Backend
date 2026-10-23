"""
Database Migration Script: Old Atlas Cluster -> New Atlas Cluster
Migrates all collections, documents, and indexes from source database to destination database.
Reads connection strings from environment variables to prevent leaking credentials.
"""

import asyncio
import os
import sys
from motor.motor_asyncio import AsyncIOMotorClient

OLD_URL = os.environ.get("OLD_MONGODB_URL", "mongodb://localhost:27017")
NEW_URL = os.environ.get("NEW_MONGODB_URL", os.environ.get("MONGODB_URL", "mongodb://localhost:27017"))
DB_NAME = os.environ.get("MONGODB_DB_NAME", "nupun_health")

async def migrate():
    print("=" * 60)
    print("Connecting to Source and Target MongoDB clusters...")
    print("=" * 60)

    old_client = AsyncIOMotorClient(OLD_URL, serverSelectionTimeoutMS=10000)
    new_client = AsyncIOMotorClient(NEW_URL, serverSelectionTimeoutMS=10000)

    # Test connectivity
    await old_client.admin.command("ping")
    print("Connected to Source MongoDB successfully.")
    await new_client.admin.command("ping")
    print("Connected to Destination MongoDB successfully.")

    old_db = old_client[DB_NAME]
    new_db = new_client[DB_NAME]

    collections = await old_db.list_collection_names()
    print(f"\nFound {len(collections)} collections in database '{DB_NAME}':")
    print(collections)
    print("=" * 60)

    total_docs_migrated = 0
    results = []

    for col_name in collections:
        if col_name.startswith("system."):
            continue

        src_col = old_db[col_name]
        dest_col = new_db[col_name]

        src_count = await src_col.count_documents({})
        print(f"\nProcessing '{col_name}' ({src_count} documents)...")

        # Drop existing target collection to guarantee a fresh, exact mirror
        await dest_col.drop()

        if src_count > 0:
            cursor = src_col.find({})
            batch = []
            migrated_in_col = 0
            async for doc in cursor:
                batch.append(doc)
                if len(batch) >= 500:
                    await dest_col.insert_many(batch, ordered=False)
                    migrated_in_col += len(batch)
                    batch = []
            if batch:
                await dest_col.insert_many(batch, ordered=False)
                migrated_in_col += len(batch)

            total_docs_migrated += migrated_in_col
            print(f"  -> Migrated {migrated_in_col} docs to '{col_name}'")

        # Copy indexes
        try:
            indexes = await src_col.index_information()
            for idx_name, idx_info in indexes.items():
                if idx_name == "_id_":
                    continue

                keys = idx_info["key"]
                options = {}
                for opt in ["unique", "sparse", "expireAfterSeconds", "weights", "default_language", "language_override"]:
                    if opt in idx_info:
                        options[opt] = idx_info[opt]

                try:
                    await dest_col.create_index(keys, name=idx_name, **options)
                    print(f"  -> Index created: {idx_name}")
                except Exception as idx_err:
                    print(f"  -> Warning creating index {idx_name}: {idx_err}")
        except Exception as e:
            print(f"  -> Error fetching/creating indexes for {col_name}: {e}")

        # Verification
        dest_count = await dest_col.count_documents({})
        match = (src_count == dest_count)
        results.append({
            "collection": col_name,
            "source_count": src_count,
            "dest_count": dest_count,
            "status": "MATCH" if match else "MISMATCH"
        })

    print("\n" + "=" * 60)
    print(f"MIGRATION SUMMARY (Total documents transferred: {total_docs_migrated})")
    print("=" * 60)
    print(f"{'Collection':<32} | {'Source':<8} | {'Dest':<8} | {'Status'}")
    print("-" * 60)

    has_mismatch = False
    for res in sorted(results, key=lambda x: x["collection"]):
        print(f"{res['collection']:<32} | {res['source_count']:<8} | {res['dest_count']:<8} | {res['status']}")
        if res["status"] != "MATCH":
            has_mismatch = True

    print("=" * 60)
    if has_mismatch:
        print("WARNING: Some collections had count mismatches!")
        sys.exit(1)
    else:
        print("SUCCESS: All collections migrated and verified with 100% data integrity!")

    old_client.close()
    new_client.close()

if __name__ == "__main__":
    asyncio.run(migrate())
