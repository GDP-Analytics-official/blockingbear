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
python backend/tests/pdf_image_regions_test.py
python backend/tests/pdf_image_regions_flow_test.py
python backend/tests/sandbox_kernel_test.py
python backend/tests/sandbox_manager_test.py
python backend/tests/chat_loop_test.py
python backend/tests/chat_routes_test.py
python backend/tests/web_search_test.py
python backend/tests/browser_extraction_test.py
```

Run model-heavy suites one at a time. Each may load its own PII model and LibreOffice;
running the entire directory concurrently causes resource failures that do not reproduce
in isolation.

Chat suites use `chat_mock_openrouter.py`, so they need no OpenRouter key and spend no
tokens. Test data stays under `backend/tests/data/test_*`; sandbox containers use the
`blockingbear-sbxt-` prefix. Suites ignore the live `DATABASE_URL` and default to isolated
SQLite files.

Web extraction keeps semantic containers and uses a single non-empty rendered
`main`/main landmark when unambiguous, otherwise the body. The static fallback
keeps the whole document because it cannot determine CSS visibility. Both paths
preserve links and table cell separators; this deliberately accepts some extra
navigation text. Page limits remain in place, and the tool reports truncation
at either the browser or the tool boundary.

`browser_extraction_test.py --camofox` additionally checks the actual browser
extractor using synthetic HTML in disposable tabs of the configured Camofox
service. It uses a separate diagnostic user and does not visit the fixture URLs.

## Synthetic test documents

Assets under `backend/tests/assets/` are synthetic and versioned. Regenerate them only
when changing the fixtures:

```bash
python -m pip install -r backend/requirements-dev.txt
python backend/tests/make_assets.py
```

`restore_marks_stress_test.py` also needs `requirements-dev.txt` because it reopens
generated documents with third-party libraries.

## PDF image regions

Image OCR groups adjacent, edge-aligned raster tiles into coherent regions before
recognition. A temporary PDF render includes only the group's image resources;
native text and vector artwork remain outside the OCR input. Standalone images,
including header logos, keep their existing image OCR path. A logo does not cause
the whole page to be rasterized.

The OCR cache records region coordinates and the original image transforms.
Redaction maps detected boxes back to the original raster pixels, preserving
native text, page rotation and layout. Old xref-based caches remain readable;
use **Reprocess OCR** to replace an earlier fragmented scan's cache with the new
region-based recognition. Rebuilding an old cache alone does not discover new text.

The region suites generate synthetic PDFs in memory and test grids, thin strips,
out-of-order resources, crop boxes, quarter-turn rotations, transparency, repeated
images, hidden OCR text, destructive redaction, selection, restoration, real OCR,
and the project/chat processing flows. Run them in an isolated backend container
with disposable data and SQLite, without production data or credentials mounted.
Keep real-document diagnostics and all their outputs under the Git-ignored `/test/`.

## PDF signature detection

With image OCR enabled, PDF processing runs the existing signature detector on
extracted images and once on every complete page. The page pass does not run
text OCR. Either pass can detect a signature; overlapping detections are merged
using their union so accepted strokes are not lost. Vector-only pages already
rendered for OCR reuse that detection.

The combined signature boxes are stored in the OCR cache in unrotated PDF
coordinates. Redaction removes the underlying pixels, text and intersecting
vector strokes, then draws one replacement label. Page size, rotation and native
text outside the detected area are preserved; the output page is not flattened.
Existing caches remain readable. Use **Reprocess OCR** on an existing document
to run the additional detector; rebuilding a preview reuses its saved results.
The model, confidence threshold and OCR option are unchanged.

Run the synthetic geometry and project/chat integration checks in an isolated
backend container with disposable data and SQLite:

```bash
python tests/pdf_signatures_test.py
python tests/pdf_signatures_flow_test.py
```

The tests cover either-pass acceptance, duplicates, multiple signatures, image
reuse and tiles, page rotation/cropping, mixed native text/vector signatures,
actual pixel removal, old caches, restoration and OCR-off behavior. The flow
suite uses the local PII model with controlled signature detections. Real-file
recognition checks and their outputs must remain outside tracked source files.

## PostgreSQL test runs

Use only a disposable database and recreate it between suites:

```bash
docker exec blockingbear-postgres psql -U blockingbear -c "DROP DATABASE IF EXISTS blockingbear_test"
docker exec blockingbear-postgres psql -U blockingbear -c "CREATE DATABASE blockingbear_test"
export BLOCKINGBEAR_TEST_DATABASE_URL="postgresql+psycopg://blockingbear:<password>@127.0.0.1:5432/blockingbear_test"
python backend/tests/chat_search_test.py
```

Never point a test suite at the live database.
