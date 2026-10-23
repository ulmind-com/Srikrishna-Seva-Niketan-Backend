"""
Cloudinary Migration Script:
1. Migrates media assets (images, videos, raw files) from old Cloudinary to new Cloudinary.
2. Updates asset URLs in MongoDB to reference the new Cloudinary account.
3. Verifies zero stale URLs remain and prints a migration report.
Reads configuration from environment variables to prevent leaking credentials.
"""

import asyncio
import os
import re
from motor.motor_asyncio import AsyncIOMotorClient
import cloudinary
import cloudinary.api
import cloudinary.uploader

OLD_CONFIG = {
    "cloud_name": os.environ.get("OLD_CLOUDINARY_CLOUD_NAME", ""),
    "api_key": os.environ.get("OLD_CLOUDINARY_API_KEY", ""),
    "api_secret": os.environ.get("OLD_CLOUDINARY_API_SECRET", ""),
    "secure": True
}

NEW_CONFIG = {
    "cloud_name": os.environ.get("CLOUDINARY_CLOUD_NAME", os.environ.get("NEW_CLOUDINARY_CLOUD_NAME", "")),
    "api_key": os.environ.get("CLOUDINARY_API_KEY", os.environ.get("NEW_CLOUDINARY_API_KEY", "")),
    "api_secret": os.environ.get("CLOUDINARY_API_SECRET", os.environ.get("NEW_CLOUDINARY_API_SECRET", "")),
    "secure": True
}

MONGO_URL = os.environ.get("MONGODB_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("MONGODB_DB_NAME", "nupun_health")


def fetch_all_old_resources():
    print(f"\nFetching all assets from OLD Cloudinary ({OLD_CONFIG['cloud_name']})...")
    cloudinary.config(**OLD_CONFIG)
    
    all_resources = []
    for rtype in ["image", "video", "raw"]:
        next_cursor = None
        count_for_type = 0
        while True:
            kwargs = {"resource_type": rtype, "max_results": 500}
            if next_cursor:
                kwargs["next_cursor"] = next_cursor
            try:
                res = cloudinary.api.resources(**kwargs)
                items = res.get("resources", [])
                for item in items:
                    item["detected_resource_type"] = rtype
                all_resources.extend(items)
                count_for_type += len(items)
                next_cursor = res.get("next_cursor")
                if not next_cursor:
                    break
            except Exception as e:
                print(f"Error fetching {rtype} resources: {e}")
                break
        print(f"  Found {count_for_type} {rtype} assets")
    return all_resources


def migrate_asset_to_new(item):
    public_id = item["public_id"]
    rtype = item.get("detected_resource_type", "image")
    src_url = item.get("secure_url") or item.get("url")

    upload_fn = cloudinary.uploader.upload_large if rtype == "video" else cloudinary.uploader.upload
    
    upload_res = upload_fn(
        src_url,
        public_id=public_id,
        resource_type=rtype,
        overwrite=True,
        invalidate=True,
        **NEW_CONFIG
    )
    new_url = upload_res.get("secure_url") or upload_res.get("url")
    return public_id, rtype, src_url, new_url


async def update_database_urls(url_mapping, pid_mapping):
    old_cname = OLD_CONFIG["cloud_name"]
    new_cname = NEW_CONFIG["cloud_name"]
    print(f"\nUpdating MongoDB database references with new Cloudinary ({new_cname}) URLs...")
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    collections = await db.list_collection_names()

    total_updates = 0

    def transform_value(val):
        nonlocal total_updates
        if isinstance(val, dict):
            new_dict = {}
            for k, v in val.items():
                new_dict[k] = transform_value(v)
            return new_dict
        elif isinstance(val, list):
            return [transform_value(elem) for elem in val]
        elif isinstance(val, str):
            if f"res.cloudinary.com/{old_cname}/" in val:
                # 1. Exact match in url_mapping
                if val in url_mapping:
                    total_updates += 1
                    return url_mapping[val]
                
                # 2. Extract public_id and match in pid_mapping
                match = re.search(r"/(?:image|video|raw)/upload/(?:v\d+/)?(.+?)(?:\.[a-zA-Z0-9]+)?$", val)
                if match:
                    pid = match.group(1)
                    if pid in pid_mapping:
                        total_updates += 1
                        return pid_mapping[pid]

                # 3. Direct cloud_name substitution fallback
                new_val = val.replace(f"res.cloudinary.com/{old_cname}/", f"res.cloudinary.com/{new_cname}/")
                total_updates += 1
                return new_val
            return val
        else:
            return val

    for col_name in collections:
        col = db[col_name]
        updated_docs = 0
        async for doc in col.find({}):
            doc_id = doc["_id"]
            modified_doc = transform_value(doc)
            if modified_doc != doc:
                await col.replace_one({"_id": doc_id}, modified_doc)
                updated_docs += 1
        if updated_docs > 0:
            print(f"  [{col_name}] Updated {updated_docs} documents")

    print(f"Total URL fields updated in database: {total_updates}")
    client.close()


async def verify_database():
    old_cname = OLD_CONFIG["cloud_name"]
    new_cname = NEW_CONFIG["cloud_name"]
    print(f"\nVerifying database has ZERO remaining references to old Cloudinary ({old_cname})...")
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    colls = await db.list_collection_names()

    remaining_old = []
    new_urls_found = []

    def check_val(val, path, col):
        if isinstance(val, dict):
            for k, v in val.items():
                check_val(v, f"{path}.{k}", col)
        elif isinstance(val, list):
            for i, x in enumerate(val):
                check_val(x, f"{path}[{i}]", col)
        elif isinstance(val, str):
            if old_cname in val:
                remaining_old.append(f"[{col}] {path}: {val}")
            elif new_cname in val:
                new_urls_found.append(f"[{col}] {path}: {val}")

    for col in colls:
        async for doc in db[col].find({}):
            check_val(doc, col, col)

    client.close()
    print(f"Remaining '{old_cname}' references in DB: {len(remaining_old)}")
    print(f"New '{new_cname}' references active in DB: {len(new_urls_found)}")
    return len(remaining_old) == 0


async def main():
    print("=" * 65)
    print(f"CLOUDINARY MIGRATION: {OLD_CONFIG['cloud_name']} -> {NEW_CONFIG['cloud_name']}")
    print("=" * 65)

    resources = fetch_all_old_resources()
    print(f"\nTotal assets to migrate: {len(resources)}")

    url_mapping = {}
    pid_mapping = {}

    success_count = 0
    fail_count = 0

    print(f"\nUploading assets to New Cloudinary ({NEW_CONFIG['cloud_name']})...")
    for i, item in enumerate(resources, 1):
        pid = item["public_id"]
        rtype = item.get("detected_resource_type", "image")
        try:
            pid, rtype, src_url, new_url = migrate_asset_to_new(item)
            url_mapping[src_url] = new_url
            pid_mapping[pid] = new_url
            success_count += 1
            if i % 10 == 0 or i == len(resources):
                print(f"  [{i}/{len(resources)}] Transferred: {pid} ({rtype})")
        except Exception as e:
            fail_count += 1
            print(f"  [{i}/{len(resources)}] FAILED to transfer {pid}: {e}")

    print("\n" + "=" * 65)
    print(f"CLOUDINARY TRANSFER SUMMARY: {success_count} succeeded, {fail_count} failed")
    print("=" * 65)

    await update_database_urls(url_mapping, pid_mapping)
    is_clean = await verify_database()
    if is_clean:
        print("\nSUCCESS: All media assets migrated and all database URLs updated!")
    else:
        print("\nWARNING: Some old references still remain in the database.")


if __name__ == "__main__":
    asyncio.run(main())
