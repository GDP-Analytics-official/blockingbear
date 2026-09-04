# Development and tests

The production installation is container-only. The commands here are for contributors
working on the source and are not an alternative deployment procedure.

## Repository layout

```text
backend/                 FastAPI application and Python 3.11 image
  app/engine/            anonymization, OCR and document processing
  app/openrouter/        chat, tools, browser and sandbox manager
  app/routes/            API routes
  sandbox/               code-interpreter image and kernel
  alembic/               database migrations
  tests/                 executable test suites and synthetic assets
frontend/                React 19 and Vite 8
nginx/                   container web tier
docker-compose.yaml      CPU stack
docker-compose.nvidia.yaml   NVIDIA delta
docker-compose.sandbox.yaml  required sandbox delta
scripts/                 init, preflight, toolkit installer and verifier
```

## Local source configuration

`backend/.env` intentionally serves both source development and Docker Compose. The
backend loads it directly when started from source. Compose reads the same file for
interpolation but passes only variables explicitly declared in `docker-compose.yaml`, so
local `DATABASE_URL`, `PII_MODEL_DIR`, worker and CORS settings do not replace container
configuration.

Start from `backend/.env.example` or run `sh scripts/init-env.sh`. Keep the active
development values for `BLOCKINGBEAR_N_WORKERS`, `BLOCKINGBEAR_CORS_ORIGINS` and
`PII_MODEL_DIR`; the remaining development settings are optional. The file is ignored by
Git and may contain database and OpenRouter credentials, so never print or commit it.

## Test suites

Suites are executable Python scripts rather than pytest tests. Each prints its own pass
and fail counts and exits non-zero on failure.

```bash
python backend/tests/pii_readiness_test.py
python backend/tests/smoke_test.py
python backend/tests/deanonymize_suite.py
python backend/tests/column_smoke_test.py
python backend/tests/chat_context_test.py
python backend/tests/user_purge_test.py
python backend/tests/pdf_ghost_test.py
python backend/tests/sandbox_kernel_test.py
python backend/tests/sandbox_manager_test.py
python backend/tests/chat_loop_test.py
python backend/tests/chat_routes_test.py
```

Run model-heavy suites one at a time. Each may load its own PII model and LibreOffice;
running the entire directory concurrently causes resource failures that do not reproduce
in isolation.

Chat suites use `chat_mock_openrouter.py`, so they need no OpenRouter key and spend no
tokens. Test data stays under `backend/tests/data/test_*`; sandbox containers use the
`blockingbear-sbxt-` prefix. Suites ignore the live `DATABASE_URL` and default to isolated
SQLite files.

## Synthetic test documents

Assets under `backend/tests/assets/` are synthetic and versioned. Regenerate them only
when changing the fixtures:

```bash
python -m pip install -r backend/requirements-dev.txt
python backend/tests/make_assets.py
```

`restore_marks_stress_test.py` also needs `requirements-dev.txt` because it reopens
generated documents with third-party libraries.

## PostgreSQL test runs

Use only a disposable database and recreate it between suites:

```bash
docker exec blockingbear-postgres psql -U blockingbear -c "DROP DATABASE IF EXISTS blockingbear_test"
docker exec blockingbear-postgres psql -U blockingbear -c "CREATE DATABASE blockingbear_test"
export BLOCKINGBEAR_TEST_DATABASE_URL="postgresql+psycopg://blockingbear:<password>@127.0.0.1:5432/blockingbear_test"
python backend/tests/chat_search_test.py
```

Never point a test suite at the live database.
