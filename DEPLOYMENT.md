# 🚀 Deploying the Backend to Render

Follow this **in order**. The backend must be live **first**, because the
Admin and Frontend need its URL.

```
1. Backend → Render        (this guide)
2. Admin   → Vercel         → needs the Render URL
3. Frontend→ Vercel         → needs the Render URL
4. Back to Render: set CORS_ORIGINS to the two Vercel URLs → redeploy
```

---

## ⛔ Before you deploy — 2 things that WILL break it if skipped

### 1. Rotate the leaked MongoDB password
The old public repos exposed the Atlas password. **Create a new password** for
the Atlas DB user (Atlas → Database Access → Edit → Edit Password) and use the
new connection string below. Treat the old one as compromised.

### 2. Allow Render to reach MongoDB Atlas
Render's `starter` plan has **no fixed outbound IP**, so IP-whitelisting a
single address won't work. In Atlas:

> **Atlas → Network Access → Add IP Address → `0.0.0.0/0` (Allow from anywhere)**

Without this, the API starts but every DB call times out. (Security is still
enforced by the DB username/password.)

---

## Step 1 — Create the service

The repo already contains [`render.yaml`](render.yaml) (a Blueprint).

**Option A — Blueprint (recommended):**
Render Dashboard → **New → Blueprint** → connect `ulmind-com/Srikrishna-Seva-Niketan-Backend`
→ Render reads `render.yaml` and creates the `srikrishna-seva-niketan-backend` web service.

**Option B — Manual Web Service:**
New → Web Service → pick the repo →
- **Runtime:** Python 3 · **Build:** `pip install -r requirements.txt`
- **Start:** `gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker --workers 2 --bind 0.0.0.0:$PORT --timeout 120`
- **Health check path:** `/health`

> Keep `--workers 2` on the 512 MB starter plan. More workers → out-of-memory restarts.

## Step 2 — Set environment variables (Render → service → Environment)

Auto-set by the blueprint: `PYTHON_VERSION`, `APP_ENV`, `DEBUG`, `SECRET_KEY`
(generated), `MONGODB_DB_NAME`, `SMTP_PORT`. **You must add these:**

| Variable | Example / notes |
|----------|-----------------|
| `MONGODB_URL` | `mongodb+srv://<user>:<NEW_password>@<cluster>.mongodb.net/?retryWrites=true&w=majority` |
| `CORS_ORIGINS` | `https://<admin>.vercel.app,https://<frontend>.vercel.app` — **exact origins, comma-separated, no trailing slash** |
| `FRONTEND_URL` | `https://<frontend>.vercel.app` |
| `FIRST_ADMIN_EMAIL` | the super-admin email created on first boot |
| `FIRST_ADMIN_PASSWORD` | a strong password (used once to seed the super-admin) |
| `CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` | from your Cloudinary dashboard (needed for uploads) |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM_EMAIL` / `ADMIN_NOTIFICATION_EMAIL` | for transactional email (optional but recommended) |

> You can set `CORS_ORIGINS` / `FRONTEND_URL` to placeholders now and update
> them in **Step 4** once the Vercel URLs exist.

## Step 3 — Deploy & verify

Render builds and deploys automatically. When live, check:

- `https://<service>.onrender.com/health` → `{"status":"ok", ...}`
- `https://<service>.onrender.com/docs` → Swagger UI loads
- Logs show: `Beanie initialised`, `Seeded role ... manager`, `Seeded bootstrap admin`

On first boot the app seeds roles (incl. `manager`), the super-admin, and a
default **Head Office** branch. Log in to the Admin with `FIRST_ADMIN_*`.

## Step 4 — Lock down CORS (after the Vercel URLs exist)

Come back and set `CORS_ORIGINS` to the **exact** Admin + Frontend Vercel URLs,
then **Manual Deploy → Clear build cache & deploy** (or just redeploy).
Only the Admin calls the API directly from the browser, so its origin **must**
be present or every admin request fails with a CORS error.

---

## Troubleshooting (common "it broke" causes)

| Symptom | Fix |
|---------|-----|
| API up but every request 500 / DB timeout | Atlas Network Access missing `0.0.0.0/0`, or wrong `MONGODB_URL` |
| Admin shows CORS error in console | `CORS_ORIGINS` missing the admin's exact origin → add it, redeploy |
| Service keeps restarting / OOM | reduce `--workers` (2 on starter), or upgrade the plan |
| `401` right after deploy | you haven't logged in; use `FIRST_ADMIN_EMAIL`/`PASSWORD` |
| Auth works locally, not in prod | `SECRET_KEY` changed between deploys invalidates old tokens — just log in again |

See also: [README](README.md) · [SECURITY](SECURITY.md)
