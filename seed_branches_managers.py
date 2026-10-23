"""Seed branches and their branch managers.

Creates one :class:`Location` per entry in ``BRANCHES`` and one manager
:class:`User` scoped to it, so each manager sees only their own branch in the
admin panel while super-admins keep seeing everything.

The script talks to the **HTTP API**, not the database, so it runs through the
normal permission and validation layers and works against a deployed backend
without a local Mongo.

Usage
-----
    # against the deployed backend (default)
    python3 seed_branches_managers.py

    # against a local backend
    API_URL=http://localhost:8000/api/v1 python3 seed_branches_managers.py

You are prompted for a **super-admin** email and password (needed for
``users:create``). Nothing is hardcoded.

Re-running is safe: an existing branch code or manager email is skipped rather
than duplicated.

Generated manager passwords are written to ``manager-credentials.txt`` in this
folder — that file is git-ignored. Hand each manager their password, then
delete the file.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import string
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

API_URL = os.environ.get(
    "API_URL", "https://srikrishna-seva-niketan-backend-y3fe.onrender.com/api/v1"
)

CREDENTIALS_FILE = "manager-credentials.txt"

# ---------------------------------------------------------------------------
# EDIT ME — one entry per branch.
#
#   name    : branch name shown in the admin panel and on staff ID cards
#   code    : short unique code (uppercase, no spaces); leave "" to auto-generate
#   city    : used in listings and on the staff signup dropdown
#   manager : the branch manager account to create for it.
#             Set "manager": None to create the branch without a manager.
# ---------------------------------------------------------------------------
BRANCHES: list[dict[str, Any]] = [
    {
        "name": "Kolkata — Salt Lake",
        "code": "SSN-KOL",
        "city": "Kolkata",
        "address": "",
        "pincode": "",
        "phone": "",
        "manager": {"name": "Kolkata Branch Manager", "email": "kolkata.manager@example.com"},
    },
    {
        "name": "Howrah",
        "code": "SSN-HWH",
        "city": "Howrah",
        "address": "",
        "pincode": "",
        "phone": "",
        "manager": {"name": "Howrah Branch Manager", "email": "howrah.manager@example.com"},
    },
    {
        "name": "Durgapur",
        "code": "SSN-DGP",
        "city": "Durgapur",
        "address": "",
        "pincode": "",
        "phone": "",
        "manager": {"name": "Durgapur Branch Manager", "email": "durgapur.manager@example.com"},
    },
    {
        "name": "Siliguri",
        "code": "SSN-SLG",
        "city": "Siliguri",
        "address": "",
        "pincode": "",
        "phone": "",
        "manager": {"name": "Siliguri Branch Manager", "email": "siliguri.manager@example.com"},
    },
]

# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

_TOKEN: Optional[str] = None


def call(method: str, path: str, payload: Optional[dict] = None) -> Any:
    """Issue an API request and return the unwrapped ``data`` field."""
    url = f"{API_URL}{path}"
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Content-Type", "application/json")
    if _TOKEN:
        request.add_header("Authorization", f"Bearer {_TOKEN}")

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            parsed = json.loads(response.read().decode())
            return parsed.get("data", parsed)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        raise RuntimeError(f"{method} {path} failed ({exc.code}): {detail}") from exc


def strong_password() -> str:
    """Generate a password satisfying the backend's complexity rule."""
    alphabet = string.ascii_letters + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(12)) + "@1Aa"
        if (
            re.search(r"[A-Z]", candidate)
            and re.search(r"[a-z]", candidate)
            and re.search(r"\d", candidate)
            and re.search(r"[\W_]", candidate)
        ):
            return candidate


def login() -> None:
    """Authenticate as a super admin; ``users:create`` requires it."""
    global _TOKEN
    print(f"Backend: {API_URL}\n")
    email = input("Super-admin email: ").strip()
    password = getpass.getpass("Super-admin password: ")

    data = call("POST", "/auth/login", {"email": email, "password": password})
    _TOKEN = data["access_token"]

    if not data.get("user", {}).get("is_superuser"):
        print(
            "\n! That account is not a super admin — creating users will likely "
            "fail with a permission error.",
            file=sys.stderr,
        )
    print("Signed in.\n")


def existing_branches() -> dict[str, dict]:
    """Map of branch code -> branch, used to stay idempotent."""
    data = call("GET", "/locations?page_size=200")
    items = data.get("items", data) if isinstance(data, dict) else data
    return {b["code"]: b for b in items}


def existing_user_emails() -> set[str]:
    data = call("GET", "/users?page_size=200")
    items = data.get("items", data) if isinstance(data, dict) else data
    return {u["email"].lower() for u in items}


def main() -> None:
    login()

    branches_by_code = existing_branches()
    taken_emails = existing_user_emails()
    created_credentials: list[tuple[str, str, str, str]] = []

    for index, entry in enumerate(BRANCHES):
        name = entry["name"]
        code = (entry.get("code") or "").strip().upper()

        # ---- Branch ----
        branch = branches_by_code.get(code) if code else None
        if branch:
            print(f"= Branch already exists: {name} ({code})")
        else:
            payload = {
                "name": name,
                "city": entry.get("city") or None,
                "address": entry.get("address") or None,
                "pincode": entry.get("pincode") or None,
                "phone": entry.get("phone") or None,
                "order": index,
                "is_active": True,
            }
            if code:
                payload["code"] = code
            branch = call("POST", "/locations", payload)
            branches_by_code[branch["code"]] = branch
            print(f"+ Branch created: {name} ({branch['code']})")

        # ---- Manager ----
        manager = entry.get("manager")
        if not manager:
            continue

        email = manager["email"].strip().lower()
        if email in taken_emails:
            print(f"  = Manager already exists: {email}")
            continue

        password = strong_password()
        call(
            "POST",
            "/users",
            {
                "name": manager["name"],
                "email": email,
                "password": password,
                "role": "manager",
                "location_ids": [branch["id"]],
                "is_active": True,
                "is_superuser": False,
            },
        )
        taken_emails.add(email)
        created_credentials.append((name, manager["name"], email, password))
        print(f"  + Manager created: {email} -> {branch['code']}")

    # ---- Credentials file ----
    if created_credentials:
        with open(CREDENTIALS_FILE, "w", encoding="utf-8") as handle:
            handle.write("Branch manager logins — hand these over, then delete this file.\n")
            handle.write(f"Admin panel: (your admin URL)/login\n\n")
            for branch_name, person, email, password in created_credentials:
                handle.write(f"{branch_name}\n")
                handle.write(f"  name     : {person}\n")
                handle.write(f"  email    : {email}\n")
                handle.write(f"  password : {password}\n\n")
        print(f"\nWrote {len(created_credentials)} login(s) to {CREDENTIALS_FILE}")
        print("Hand each manager their password, then delete that file.")
    else:
        print("\nNo new managers were created.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
    except RuntimeError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
