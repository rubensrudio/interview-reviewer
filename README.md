# Interview Reviewer

## Overview

Interview Reviewer lets a candidate upload a resume, run a mock technical interview and receive
a report with an assessment of each answer and traceable technical sources.

The repository has two workspaces:

- `backend/` — FastAPI API and background job worker (Python 3.12, managed with `uv`).
- `frontend/` — Angular web application.

The language model runs locally behind an OpenAI-compatible API (Ollama in development). The
full flow (resume upload to report) does not depend on any external service other than Google
OIDC sign-in and the e-mail (SMTP) service.

## Requirements

- Docker with Docker Compose v2
- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js and npm (version pinned in `frontend/package.json`)

## Local setup

1. Start the development services (Postgres 16 and Mailpit):

   ```bash
   docker compose up -d db mailpit
   ```

   - Postgres: `localhost:5432` (user `ir`, password `ir`, database `interview_reviewer`)
   - Mailpit SMTP: `localhost:1025`; web UI: <http://localhost:8025>

2. Optional: start the local LLM (Ollama) and pull the development model:

   ```bash
   docker compose --profile llm up -d llm
   docker compose exec llm ollama pull qwen2.5:7b-instruct
   ```

   The OpenAI-compatible API is served at `http://localhost:11434/v1`. Tests never call a real
   LLM.

3. Install dependencies:

   ```bash
   (cd backend && uv sync --frozen)
   (cd frontend && npm ci)
   ```

4. Run the application:

   ```bash
   docker compose up -d db mailpit && (cd backend && uv run alembic upgrade head && uv run uvicorn app.main:create_app --factory --port 8000 & uv run python -m app.jobs.worker &) && (cd frontend && npx ng serve --proxy-config proxy.conf.json)
   ```

   Open <http://localhost:4200>. Requests to `/api` are proxied to the backend on port 8000.

## Commands

Backend (run inside `backend/`):

| Target | Command |
|---|---|
| Install dependencies | `uv sync --frozen` |
| Lint | `uv run ruff check . --output-format=concise` |
| Typecheck | `uv run mypy app` |
| Tests | `uv run pytest -q` |
| Build | `uv build` |

Backend integration tests require the Postgres service (`docker compose up -d db`).

Frontend (run inside `frontend/`):

| Target | Command |
|---|---|
| Install dependencies | `npm ci` |
| Lint | `npx ng lint` |
| Typecheck | `npx tsc --noEmit -p tsconfig.app.json` |
| Unit tests | `npx ng test --watch=false` |
| End-to-end tests | `npx playwright test` |
| Build | `npx ng build` |

## Backend

The backend is the Python package `interview-reviewer-backend` (Python 3.12, built with
hatchling, dependencies locked in `backend/uv.lock`). The FastAPI application is created by the
factory `app.main:create_app`.

Run the API alone (inside `backend/`):

```bash
uv sync --frozen
uv run uvicorn app.main:create_app --factory --port 8000
```

Check that it is up:

```bash
curl http://localhost:8000/api/health
# {"status":"ok"}
```

Tooling configured in `backend/pyproject.toml`:

- `ruff` for lint (line length 100, target Python 3.12).
- `mypy` in strict mode for the `app` package.
- `pytest` writes a JUnit report to `backend/reports/junit.xml` on every run.

## Configuration

The backend reads its settings from environment variables prefixed with `IR_`. Defaults target
the local services from `docker-compose.yml`.

| Variable | Default |
|---|---|
| `IR_APP_ENV` | `development` |
| `IR_DATABASE_URL` | `postgresql+psycopg://ir:ir@localhost:5432/interview_reviewer` |
| `IR_FRONTEND_BASE_URL` | `http://localhost:4200` |
| `IR_SMTP_HOST` / `IR_SMTP_PORT` | `localhost` / `1025` |
| `IR_LLM_BASE_URL` | `http://localhost:11434/v1` |
| `IR_LLM_MODEL` | `qwen2.5:7b-instruct` (development only) |
| `IR_LLM_ALLOWED_HOSTS` | `["localhost", "127.0.0.1", "llm"]` |
| `IR_MAX_PDF_BYTES` | `5242880` (5 MB) |
| `IR_MAX_RESUMES_PER_USER` | `10` |
| `IR_MAX_ANSWER_CHARS` | `5000` |
| `IR_SESSION_EXPIRY_DAYS` | `30` |
| `IR_MAX_REQUIRED_SKILLS` | `20` |
| `IR_OCR_ENABLED` | `false` |

The full list of variables and their defaults is in `backend/.env.example`.

Secrets have no default and must be provided through the environment, never committed:
`IR_THROTTLE_SECRET`, `IR_OIDC_STATE_SECRET`, `IR_GOOGLE_CLIENT_ID` and
`IR_GOOGLE_CLIENT_SECRET` (the last two only for Google sign-in).

QA credentials are read from `QA_USER_EMAIL` and `QA_USER_PASSWORD`.

The LLM client only connects to hosts listed in `IR_LLM_ALLOWED_HOSTS`. When a candidate
deletes their account, all of their data is removed immediately; copies in backups expire
within 30 days.

## Database migrations

The schema is managed with Alembic (`backend/alembic/`, configured by `backend/alembic.ini`).
Migrations run against `IR_DATABASE_URL`; the required secrets (`IR_THROTTLE_SECRET`,
`IR_OIDC_STATE_SECRET`) must also be set because the settings are loaded as a whole.

Run inside `backend/`:

| Target | Command |
|---|---|
| Apply all migrations | `uv run alembic upgrade head` |
| Revert the last migration | `uv run alembic downgrade -1` |
| Show the current revision | `uv run alembic current` |
| Create a migration | `uv run alembic revision --rev-id 0001 -m "jobs"` (file `0001_jobs.py`) |

Every ORM model module must be imported in `backend/app/models/__init__.py` so that
`Base.metadata` (used by `alembic/env.py`) knows its tables.

Integration tests (`backend/tests/integration`) never use the development database: each
pytest session creates its own database `ir_test_<uuid>` on the configured server, runs
`alembic upgrade head`, gives each test a `db` session whose transaction is rolled back, and
drops the database at the end. Parallel test runs therefore do not interfere with each other.

## Google sign-in

Google sign-in uses the OpenID Connect authorization code flow with PKCE (`S256`), `state`
and `nonce`. The whole exchange happens in the backend (`backend/app/auth/google_oidc.py`);
the client secret never reaches the browser and is never logged.

To enable it:

1. In the Google Cloud console, create an OAuth 2.0 client of type "Web application".
2. Add the callback URL as an authorized redirect URI. Locally it is
   `http://localhost:4200/api/auth/google/callback` (`/api` reaches the backend through a
   same-origin proxy); in other environments it is `<frontend origin>/api/auth/google/callback`.
3. Set `IR_GOOGLE_CLIENT_ID`, `IR_GOOGLE_CLIENT_SECRET` and, if the URL differs from the
   default, `IR_GOOGLE_REDIRECT_URI` (it must match the registered URI exactly).
4. Set `IR_OIDC_STATE_SECRET` to a long random value. It signs the short-lived
   (10 minutes), HttpOnly `ir_google_oidc` cookie that holds `state`, `nonce` and the PKCE
   verifier between the redirect to Google and the callback.

The backend needs outbound HTTPS access to `accounts.google.com`, `oauth2.googleapis.com`
and `www.googleapis.com`. A cancelled sign-in, an invalid `state`, an `id_token` that fails
validation (signature, `iss`, `aud`, `exp`, `nonce`) or an e-mail that Google has not
verified all end with the `GOOGLE_AUTH_FAILED` error and no account is created or linked.
