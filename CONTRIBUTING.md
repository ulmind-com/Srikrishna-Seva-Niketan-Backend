# Contributing — Srikrishna Seva Niketan Backend

Thanks for contributing! This guide keeps the codebase consistent and the
developer experience smooth.

## Getting set up

1. `cp .env.example .env` and fill in local values (see the README env table).
2. Install deps: `uv sync` **or** `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`.
3. Run a local MongoDB (`mongod`) or point `MONGODB_URL` at a dev Atlas cluster.
4. Start the API: `python run.py` → http://localhost:8000/docs

## Project conventions

- **Layering:** keep routers thin. Business logic belongs in `app/services/`,
  data access in `app/repositories/`, shapes in `app/schemas/`, documents in
  `app/models/`. Don't query Mongo directly from a router.
- **New resource?** Add `models/<x>.py` → register in `models/__init__.py` →
  `schemas/<x>.py` → `api/v1/<x>/router.py` → include it in `api/v1/__init__.py`.
  Add its permissions to `core/permissions.py`.
- **Auth:** guard every mutating route with `require_permission("<resource>", "<action>")`.
- **Multi-location:** operational records carry a `location_id`. Use the helpers
  in `app/dependencies/scoping.py` so branch managers stay scoped.
- **Style:** type hints everywhere, module/function docstrings, follow the
  surrounding code's idioms.

## Branching & commits

- Branch from `main`: `feat/<short-name>`, `fix/<short-name>`, `chore/<short-name>`.
- Use [Conventional Commits](https://www.conventionalcommits.org/):
  `feat: add payslip export`, `fix: scope reports by branch`, `docs: …`, `chore: …`.
- Keep commits focused and the message in the imperative mood.

## Before you open a PR

- [ ] `python -m compileall app` passes.
- [ ] `pytest` passes (if you touched tested code).
- [ ] No secrets, `.env`, or credentials added to tracked files.
- [ ] README / `.env.example` updated if you added config or endpoints.

Open the PR against `main` and fill in the PR template. CI must be green.

## ⚠️ Secrets

Real credentials live **only** in your local `.env` (git-ignored) and in the
deployment platform's secret store — never in code, fixtures, or commits.
See [SECURITY.md](SECURITY.md).
