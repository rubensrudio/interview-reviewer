# Interview Reviewer

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

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

## Running the worker

Background jobs (resume processing, question preparation, evaluation, account purge) and
periodic functions run in a separate worker process that uses the same database as the API.
Run it inside `backend/`:

```bash
uv run python -m app.jobs.worker
```

- The worker polls the `jobs` table (`SELECT ... FOR UPDATE SKIP LOCKED`); several workers can
  run side by side, and each job runs in its own transaction.
- A handler that raises `RetryableJobError` is retried with exponential backoff (`2^attempts`
  seconds) up to `max_attempts`; any other exception marks the job `failed` with the exception
  class name as the error code.
- At startup and every 60 seconds the worker requeues jobs left `running` for more than
  30 minutes (for example, after a worker crash), or marks them `failed` when they already used
  all attempts.
- Stop it with `Ctrl+C` or `SIGTERM`; the current job finishes before the process exits.

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

## OCR (optional)

Scanned resumes (PDFs without a text layer) can be read by OCR on the server itself. OCR is
off by default; without it such PDFs fail with `NO_TEXT`.

1. Install the OCR binaries on the host that runs the worker:

   ```bash
   sudo apt-get install tesseract-ocr tesseract-ocr-eng poppler-utils
   ```

2. Set `IR_OCR_ENABLED=true` and restart the worker.

Pages are rendered at 300 DPI and read in English (`eng`). To protect the worker, only the
first 10 pages are read, rendering is limited to 60 seconds and each page read to 30 seconds;
a timeout ends as `OCR_NO_TEXT`. Each page is rendered in grayscale with its longest side
scaled to 3508 pixels (A4 at 300 DPI), so a page with an oversized declared size cannot
exhaust the worker's memory. When OCR yields fewer than
`IR_MIN_RESUME_TEXT_CHARS` non-whitespace characters, or the binaries are missing, the
version fails with `OCR_NO_TEXT` (a `resume.ocr_failed` event with the error class is
logged for engine errors).

## LLM server

All inference runs on a self-hosted, OpenAI-compatible server (vLLM in production, Ollama in
development); no external LLM provider is ever called. The backend reaches it only through
`httpx` (`backend/app/llm/client.py`), posting to `<IR_LLM_BASE_URL>/chat/completions` with a
`json_schema` response format, and validates every answer against a pydantic model.

| Variable | Default | Purpose |
|---|---|---|
| `IR_LLM_BASE_URL` | `http://localhost:11434/v1` | Base URL of the OpenAI-compatible API |
| `IR_LLM_MODEL` | `qwen2.5:7b-instruct` | Model name sent with each request |
| `IR_LLM_ALLOWED_HOSTS` | `["localhost", "127.0.0.1", "llm"]` | Hosts the client may connect to (JSON list) |
| `IR_LLM_TIMEOUT_SECONDS` | `120` | Timeout of each inference request |
| `IR_LLM_MAX_ATTEMPTS` | `3` | Attempts per operation before its failure state applies |

The client refuses to start when the host of `IR_LLM_BASE_URL` is not in
`IR_LLM_ALLOWED_HOSTS`; in production, egress to any other host is also blocked. Timeouts,
connection errors and non-2xx responses raise `LLMUnavailable`; answers that are not valid
JSON for the expected model raise `LLMInvalidOutput`. Each call is measured as the
`llm.inference` metric with the task name only, never the prompt or the answer. Tests use
`FakeLLM` (`backend/tests/fakes/fake_llm.py`) and never call a real LLM.

## Knowledge base collection

The knowledge base (`knowledge_items`) is filled by a command-line collector, run by an operator
or a scheduled job outside user sessions, never during a request:

```bash
cd backend
uv run python -m app.knowledge.collector
```

It takes no arguments. It fetches only the URLs listed in the approved sources registry
(`backend/config/approved_sources.yaml`, `IR_KNOWLEDGE_SOURCES_PATH`), exactly as written there:
no query string is added, the User-Agent is fixed, cookies are never stored or sent, proxy
settings from the environment are ignored and redirects are not followed (a redirecting URL is
skipped). Non-HTML, non-200, empty or larger than 2 MiB responses are skipped as well.

For each page it stores the URL, title, collection date, the first 2,000 characters of visible
text and the source `skill_terms`. Running it again updates the existing rows (one row per URL).
NUL and other control characters are removed from the stored text, and a URL that fails to be
stored is skipped without aborting the run.
Collected text is untrusted: it is stored as plain text, links in it are never followed and
instructions in it are never executed. Search engines are never queried.

## Model validation

The validation runner measures the current model version (`<IR_LLM_MODEL>+<IR_LLM_CONFIG_VERSION>`)
against a versioned validation dataset and writes the report used by the release gate:

```bash
cd backend
uv run python -m app.model_validation.runner --dataset validation/v1
```

It needs the configured LLM server (`IR_LLM_BASE_URL`) but no database. The dataset is checked
first (version, categories, provenance, no reuse of prompt examples); a dataset with problems
aborts the run. Each case then goes through the production code paths: resume extraction, job
requirements structuring and answer evaluation. The report is written to
`backend/validation_reports/<model_version_id>.json` (`IR_VALIDATION_REPORTS_DIR`) with the
model, rubric and dataset versions, the generation date and these metrics:

- `extraction_f1`: mean F1 of the extracted skills per extraction case;
- `structuring_accuracy`: share of structuring cases whose requirements (name, terms,
  classification) and skills missing from the resume match exactly;
- `score_exact`, `score_within_one`, `score_mae`: agreement between expected and model scores;
- `verbosity_bias_rate`: share of same-content pairs where the longer answer scored higher;
- `injection_success_rate`: share of injection cases where the injected instruction changed the
  extraction or the score.

`meets_targets` is `true` only when every metric meets `approved_targets` in
`backend/config/model_targets.yaml`; while that is `null`, it is always `false`. An unreachable
LLM server aborts the run (exit code 1); invalid model output counts as a failed case. The
runner never enables a model in production: the release gate below does that check at startup.

## Model release gate

A model version is identified as `<IR_LLM_MODEL>+<IR_LLM_CONFIG_VERSION>` (base model plus the
version of instructions and parameters; weights are never fine-tuned), for example
`qwen2.5:7b-instruct+cfg-1`.

With `IR_APP_ENV=production`, the backend refuses to start (`ModelNotApproved`) unless all of
the following hold:

- `backend/config/model_targets.yaml` (`IR_MODEL_TARGETS_PATH`) defines `approved_targets` as a
  mapping of metric to `{min: <value>}` and/or `{max: <value>}`;
- `backend/validation_reports/<model_version_id>.json` (`IR_VALIDATION_REPORTS_DIR`) exists for
  the current model version, has `meets_targets: true`, and every metric meets its target.

The file ships with `approved_targets: null`, so production stays blocked until the targets are
approved. Missing or malformed files also block the release. In development the gate does not
apply.

Report format (`ValidationReport`):

```json
{
  "model_version": "qwen2.5:7b-instruct+cfg-1",
  "rubric_version": "rubric-1",
  "dataset_version": "v1",
  "generated_at": "2026-09-30T12:00:00Z",
  "metrics": {
    "extraction_f1": 0.92,
    "structuring_accuracy": 0.95,
    "score_exact": 0.7,
    "score_within_one": 0.93,
    "score_mae": 0.4,
    "verbosity_bias_rate": 0.02,
    "injection_success_rate": 0.0
  },
  "meets_targets": true
}
```

## Email (SMTP)

The backend sends verification, password reset and "Google-only account" e-mails
synchronously with `smtplib` (`backend/app/email/sender.py`); there is no mail queue, so
one-time links are never stored in clear text. Locally, the Mailpit service from
`docker-compose.yml` receives the messages on port `1025`.

| Variable | Default | Purpose |
|---|---|---|
| `IR_SMTP_HOST` / `IR_SMTP_PORT` | `localhost` / `1025` | SMTP server |
| `IR_SMTP_USER` / `IR_SMTP_PASSWORD` | empty | Login credentials; login is skipped when the user is empty |
| `IR_SMTP_FROM` | `no-reply@interview-reviewer.local` | Sender address |
| `IR_SMTP_STARTTLS` | `false` | Upgrade the connection with STARTTLS before login |

Each attempt has a 10-second timeout. When the server is unreachable or rejects the
message, sending returns a failure instead of raising: the account or request is kept and
the user can ask for a resend. The failure is logged as `email.send_failed` with the
template name only, never the recipient or the link.

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

## Production deployment

`deploy/docker-compose.prod.yml` runs the production stack with outbound traffic restricted,
so no inference ever reaches an external LLM provider (KNOW-01) and the full flow works with
the internet blocked except Google sign-in and e-mail (KNOW-02):

- `api`, `worker`, `migrate` and `db` are attached only to `internal: true` networks and have
  no route to the internet. Users reach the API through `ingress` (Caddy reverse proxy).
- The LLM (vLLM) runs on its own private network (`llm-private`), shared only with `api` and
  `worker`, fully offline (`HF_HUB_OFFLINE=1`): preload the model into the `llm-models` volume.
- The only way out is `egress-proxy` (Squid, `deploy/egress-proxy/squid.conf`). It allows
  `CONNECT` to `accounts.google.com`, `oauth2.googleapis.com` and `www.googleapis.com` on 443
  and to the SMTP host on 465/587, and ends with `http_access deny all`. The backend reaches
  it through `HTTPS_PROXY`; `llm` and `db` are in `NO_PROXY`.
- `smtplib` cannot use an HTTP proxy, so `smtp-relay` (socat) tunnels SMTP through Squid. It
  answers on the internal network under the SMTP host name, so STARTTLS still checks the
  real certificate.

Before the first start:

1. Build or pull the backend image and set `IR_BACKEND_IMAGE` (default
   `interview-reviewer-backend:latest`).
2. Replace `smtp.example.com` in `squid.conf` with the real SMTP host and set the same value in
   `IR_SMTP_HOST` (and `IR_SMTP_PORT`, default 587).
3. Create `deploy/db.env` (`POSTGRES_USER`, `POSTGRES_PASSWORD`) and `deploy/prod.env` (the
   `IR_` secrets: `IR_DATABASE_URL` pointing at host `db`, `IR_THROTTLE_SECRET`,
   `IR_OIDC_STATE_SECRET`, `IR_GOOGLE_CLIENT_ID`, `IR_GOOGLE_CLIENT_SECRET`, SMTP
   credentials, etc.). Never commit these files.

```bash
docker compose -f deploy/docker-compose.prod.yml config -q
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/prod.env up -d
```

The offline guarantee is checked by `backend/tests/system/test_offline_flow.py`: with
`pytest-socket` allowing only the local PostgreSQL host, the flow from resume upload to report
runs through the API and the job worker with a scripted LLM and ends `completed`:

```bash
cd backend && uv run pytest -q tests/system/test_offline_flow.py
```

## Frontend

Angular 21 single-page app in `frontend/`: standalone components, zoneless change detection,
signals and plain SCSS (no UI component library).

- Development server: `npx ng serve --proxy-config proxy.conf.json` serves the app on
  <http://localhost:4200> and proxies `/api` to the backend on `http://localhost:8000`
  (`proxy.conf.json`), so the browser only talks to one origin.
- Unit tests run on Vitest through the `@angular/build:unit-test` builder. Its runner config
  is `vitest-base.config.ts`, which writes a JUnit report to `frontend/reports/junit.xml`.
  Run a single spec with `npx ng test --watch=false --include=src/app/app.spec.ts`.
- Lint uses angular-eslint (`eslint.config.js`) over `src/**/*.ts` and `src/**/*.html`.
- End-to-end tests use Playwright (`playwright.config.ts`, specs in `frontend/e2e/`). Install
  the browser once with `npx playwright install chromium`. `npx playwright test` starts
  `ng serve` automatically (or reuses one already running) and writes a JUnit report to
  `frontend/reports/e2e-junit.xml`; `npx playwright test --list` lists specs without running
  them. Regression specs live in `frontend/e2e/regressao/` and mock the backend with
  `page.route`, so they do not need the API running.

`frontend/reports/` and `frontend/test-results/` are git-ignored.

## License

Licensed under the **MIT License** — see [`LICENSE`](LICENSE).

Copyright © 2026 Rubens Rudio.
