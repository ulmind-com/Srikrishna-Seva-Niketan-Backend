<div align="center">

# 🏥 Srikrishna Seva Niketan — Backend API

**The REST API powering the Srikrishna Seva Niketan platform** — a multi-location
nursing & home-care staffing system (public website + admin panel).

[![FastAPI](https://img.shields.io/badge/FastAPI-0.11x-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MongoDB](https://img.shields.io/badge/MongoDB-Beanie%20ODM-47A248?logo=mongodb&logoColor=white)](https://beanie-odm.dev/)
[![License](https://img.shields.io/badge/license-Proprietary-red)](LICENSE)

</div>

---

## 📖 Overview

This service is the single source of truth for the whole platform. It serves:

- **Public website** ([Frontend](https://github.com/ulmind-com/Srikrishna-Seva-Niketan-Frontend)) — services, blogs, equipment, bookings, enquiries.
- **Admin panel** ([Admin](https://github.com/ulmind-com/Srikrishna-Seva-Niketan-Admin)) — full back-office + the multi-location **Staff Management** system.

It is built with **FastAPI + Beanie (async MongoDB ODM)** following a clean,
layered architecture (routers → services → repositories → models) with
role-based access control and per-branch data scoping.

### ✨ Key capabilities

| Area | What it does |
|------|--------------|
| 🔐 **Auth & RBAC** | JWT access/refresh tokens, roles & granular `resource:action` permissions, bootstrap super-admin |
| 🏢 **Multi-location** | Branches/locations; managers scoped to their branch(es), super-admin sees all |
| 👩‍⚕️ **Staff Management** | Employees (nurse/aaya/attendant), customers, daily-shift assignments, notifications |
| 💰 **Finance** | Customer payments, firm expenses, margin-aware reporting |
| 📊 **Reports** | Profit & loss, staff distribution, customer payments, daily vacancy summary |
| 🌐 **Website content** | Services, categories, blogs, videos, testimonials, FAQs, equipment, careers |
| 📥 **Requests** | Bookings, rentals, job applications, contact & infection-control enquiries |
| 🖼️ **Media** | Cloudinary-backed image/file uploads |
| 🔔 **Notifications** | In-app admin notifications + email (SMTP) |
| 🧾 **Auditing** | Activity logs for every mutating action |

---

## 🧱 Tech stack

- **Framework:** FastAPI (ASGI, served by Uvicorn/Gunicorn)
- **Database:** MongoDB via [Beanie](https://beanie-odm.dev/) (async ODM over Motor)
- **Auth:** JWT (`python-jose`) + `passlib[bcrypt]`
- **Validation:** Pydantic v2 + `pydantic-settings`
- **Media:** Cloudinary
- **Rate limiting:** SlowAPI
- **Runtime:** Python **3.13**
- **Package manager:** [`uv`](https://docs.astral.sh/uv/) (or plain `pip`)

---

## 🚀 Quick start

> **Prerequisites:** Python 3.13, a MongoDB instance (local `mongod` or Atlas),
> and (optionally) a Cloudinary account for media uploads.

```bash
# 1. Clone
git clone https://github.com/ulmind-com/Srikrishna-Seva-Niketan-Backend.git
cd Srikrishna-Seva-Niketan-Backend

# 2. Create the environment file (NEVER commit .env)
cp .env.example .env
#    → open .env and fill in real values (see the table below)

# 3. Install dependencies
#    Option A — uv (recommended, fast):
uv sync
#    Option B — pip + venv:
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 4. Run (hot-reload dev server on http://localhost:8000)
python run.py
```

On first start the app **auto-seeds** the permission catalogue, the default
roles (`super_admin`, `admin`, `editor`, `support`, `manager`), the bootstrap
super-admin (from `FIRST_ADMIN_*`), and a default **Head Office** branch.

- 📚 **Interactive API docs (Swagger):** http://localhost:8000/docs
- 📘 **ReDoc:** http://localhost:8000/redoc

### Run a throwaway local DB (no Atlas needed)

```bash
# Point the API at a local MongoDB with a disposable database name
MONGODB_URL="mongodb://127.0.0.1:27017" MONGODB_DB_NAME="ssn_dev" python run.py
```

---

## 🔑 Environment variables

Copy `.env.example` → `.env`. **Never commit `.env`.** Key variables:

| Variable | Required | Description |
|----------|:--------:|-------------|
| `SECRET_KEY` | ✅ | JWT signing key — generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `MONGODB_URL` | ✅ | Mongo connection string (local or Atlas) |
| `MONGODB_DB_NAME` | ✅ | Database name |
| `CLOUDINARY_*` | ⚠️ | Needed for media uploads |
| `SMTP_*` | ⚠️ | Needed for transactional email |
| `CORS_ORIGINS` | ✅ | Comma-separated allowed origins (frontend + admin URLs) |
| `FIRST_ADMIN_EMAIL` / `FIRST_ADMIN_PASSWORD` | ✅ | Bootstrap super-admin created on first run |
| `FRONTEND_URL` | ✅ | Used in password-reset links |

See [`.env.example`](.env.example) for the full annotated list.

---

## 🗂️ Project structure

```
app/
├── main.py                 # FastAPI app factory, middleware, lifespan
├── config/                 # Pydantic settings (reads .env)
├── core/                   # Cross-cutting: security, permissions, responses,
│                           #   exceptions, pagination, rate limiter, logging
├── database/               # Mongo connection + idempotent seeding
├── dependencies/           # Auth deps (ActorContext), multi-location scoping
├── models/                 # Beanie documents (one collection each)
├── schemas/                # Pydantic request/response models
├── repositories/           # Generic async data-access layer
├── services/               # Business logic (CRUD, reports, notifications, …)
├── middleware/             # Request context + security headers
├── utils/                  # Helpers (slugify, references, files, sanitize)
└── api/v1/                 # Versioned routers, one package per resource
run.py                      # Dev entrypoint (uvicorn with reload)
Dockerfile / docker-compose.yml / render.yaml   # Deployment
```

### Request lifecycle

```
HTTP → router (api/v1/<resource>) → require_permission (RBAC + scope)
     → service (business logic) → repository → Beanie model → MongoDB
     → schema (response) → standard JSON envelope
```

Every list/detail query for operational data is **branch-scoped** through
`ActorContext.scope_filter()`, so a branch manager only ever sees their own
location's records while a super-admin sees everything.

---

## 🔒 Authentication & RBAC

- **Login:** `POST /api/v1/auth/login` → `{ access_token, refresh_token, user }`.
  The `user` payload includes the **effective permission set** (role + direct
  grants) so clients can gate their UI.
- **Authorize requests:** send `Authorization: Bearer <access_token>`.
- **Permissions** follow a `resource:action` convention (e.g. `employees:create`).
  Routes are guarded by `require_permission("employees", "create")`.
- **Roles** are seeded from `app/core/permissions.py::DEFAULT_ROLES` and kept in
  sync on every startup.

---

## 🐳 Deployment

- **Render (recommended):** step-by-step guide in **[DEPLOYMENT.md](DEPLOYMENT.md)**
  — covers the `render.yaml` blueprint, every env var, the MongoDB Atlas
  network-access gotcha, and CORS wiring. Deploy the backend **first**.
- **Docker:** `docker compose up --build` (see `docker-compose.yml`).
- Production is served by Gunicorn + Uvicorn workers (see `Dockerfile`).

---

## 🧪 Development & quality

```bash
python -m py_compile app/**/*.py     # quick syntax check
pytest                               # test suite (see pytest.ini)
```

CI runs on every push/PR — see [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

---

## 🤝 Contributing & security

- Development workflow, branching and commit conventions: [CONTRIBUTING.md](CONTRIBUTING.md)
- Reporting vulnerabilities & secret-handling policy: [SECURITY.md](SECURITY.md)

> ⚠️ **Never commit secrets.** Real credentials live only in your local `.env`
> (git-ignored) and in the deployment platform's secret store.

---

<div align="center">
<sub>© Srikrishna Seva Niketan · built & maintained by <b>ulmind</b>. All rights reserved.</sub>
</div>
