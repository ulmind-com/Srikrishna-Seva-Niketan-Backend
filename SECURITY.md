# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Email the
maintainers at **security@ulmind.in** (or contact the ulmind team privately)
with details and reproduction steps. We'll acknowledge and work on a fix
promptly.

## Secret handling (read this)

This repository is configured so that **no secret is ever committed**:

- Real credentials live only in a local, git-ignored `.env` and in the
  deployment platform's secret store (Render / Docker env).
- Only `.env.example` (placeholders) is tracked.
- `.gitignore` blocks `.env`, `.env.*` (except `.env.example`), key files,
  and service-account JSON.

### If you ever leak a secret

1. **Rotate it immediately** at the provider (MongoDB Atlas user password,
   Cloudinary API secret, SMTP app password, `SECRET_KEY`, etc.).
2. Remove it from the working tree and from git history (`git filter-repo` or
   BFG) before pushing.
3. Assume anything pushed to a public repo is already compromised.

### Never hardcode connection strings

Utility/migration scripts read `MONGODB_URL` from the environment:

```python
import os
MONGO = os.environ.get("MONGODB_URL", "mongodb://localhost:27017")
```

Do not paste a real Atlas URI (with username/password) into any `.py` file.
