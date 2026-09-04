# BlockingBear BACKEND image: FastAPI API, PII engine, and document queue.
#
# Build (the context is this directory, not the repository root):
#     docker build -t blockingbear-backend:0.1.0 backend
#
# Three non-obvious choices are explained where they apply:
#   1. the 1.2 GB PII model is MOUNTED, not copied into the image;
#   2. sandbox files are streamed through Docker exec, so no host/container path
#      parity is required;
#   3. the code interpreter requires the host Docker socket, which grants this
#      process root-equivalent Engine control on Linux.
# Complete standalone diagnostic commands are at the end of this file.


# --- Docker CLI ---------------------------------------------------------------
# The backend controls containers through the `docker` executable: sandbox.py
# for code execution and browser.py for web search. It needs the CLI, not a
# daemon in this image. Copy the pinned static binary from the official image
# instead of adding Docker's apt repository.
FROM docker:27-cli AS dockercli


# Python 3.11 is the version used to develop the project (docs/DEPLOYMENT.md).
# Do not raise it without checking the available torch and onnxruntime wheels.
# onnxruntime is pinned to 1.20.1 in requirements.txt for compatibility.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# --- System packages ----------------------------------------------------------
# libreoffice-* converts legacy formats and generates OOXML previews. Without
# it, PDF still works but Office previews do not, and /api/health reports
# libreoffice=false. Debian installs soffice in PATH at /usr/bin/soffice.
# fonts-* keeps document conversion metrics stable.
# libgomp1 is loaded dynamically by onnxruntime and is absent from slim images.
# libgl1 and libglib2.0-0 are runtime dependencies of OpenCV/RapidOCR.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer libreoffice-calc libreoffice-impress \
        fonts-liberation fonts-dejavu \
        libgomp1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker


# --- Python dependencies ------------------------------------------------------
# Install PyTorch alone from the CPU index BEFORE requirements.txt. The default
# PyPI wheel pulls CUDA libraries that this image does not need. Installing it
# first satisfies the requirements constraint without replacement. Always add
# psycopg: its small size avoids a late missing-driver failure when DATABASE_URL
# selects PostgreSQL.
WORKDIR /app
# The PyTorch index is a build argument so one Dockerfile produces the default
# CPU image or the CUDA variant. docker-compose.nvidia.yaml changes the index
# and attaches the GPU together. Manual build example:
#   docker build --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu126 -t ... backend
# No nvidia/cuda base is needed because PyTorch CUDA wheels include their runtime
# packages. Compatible indices have one source of truth in the NVIDIA override.
# The CUDA image grows to roughly 6-7 GB and is useless without a GPU device.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

COPY requirements.txt requirements-postgres.txt ./
# The version constraint on this first install is essential. Without it, an
# incompatible index could install its newest older torch, after which PyPI
# would replace it while leaving unused NVIDIA packages behind. Read the version
# floor directly from requirements.txt to keep one source of truth. The pattern
# deliberately excludes torchvision and torchaudio.
RUN pip install --index-url ${TORCH_INDEX} "$(grep -E '^torch([<>=!~]|$)' requirements.txt)" \
    && pip install -r requirements.txt \
    && pip install -r requirements-postgres.txt \
    && rm requirements.txt requirements-postgres.txt

# RapidOCR bundles only the release defaults (PP-OCRv6 in 3.9.2), while
# image_ocr.py selects PP-OCRv5 mobile based on project document quality tests.
# Lazy initialization would try to write these models into site-packages after
# the process has dropped to uid 1000 and fail with EACCES. Initialize as root at
# build time so both ONNX files are validated and available without runtime egress.
RUN python -c "from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR; RapidOCR(params={'Det.engine_type': EngineType.ONNXRUNTIME, 'Rec.engine_type': EngineType.ONNXRUNTIME, 'Det.ocr_version': OCRVersion.PPOCRV5, 'Rec.ocr_version': OCRVersion.PPOCRV5, 'Det.model_type': ModelType.MOBILE, 'Rec.model_type': ModelType.MOBILE, 'Global.use_cls': False})"

# The NVIDIA variant reaches torch.compile/Triton on its first forward. Triton
# builds a native launcher at runtime and needs cc/gcc, a linker, and libc
# headers. The NVIDIA override sets this argument to 1; CPU remains smaller.
# Keeping this step after Python dependencies preserves their shared cache.
ARG INSTALL_TRITON_BUILD_TOOLS=0
RUN if [ "$INSTALL_TRITON_BUILD_TOOLS" = "1" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends build-essential \
        && command -v cc >/dev/null \
        && command -v gcc >/dev/null \
        && rm -rf /var/lib/apt/lists/*; \
    fi


# --- Application --------------------------------------------------------------
# backend/ becomes /app, so app/config.py resolves relative model paths there and
# db_migrate.py finds Alembic resources at their expected absolute locations.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
# Maintenance utility: delete conversations while retaining projects. Run with
# `docker exec -it <container> python wipe_chats.py`.
COPY wipe_chats.py ./

# MOUNT the 1.2 GB PII model instead of baking it into the image. It has an
# independent update cycle, is Git-ignored, and a clean clone does not contain
# it. Versioned directories make model changes a mount/configuration switch
# instead of an image rebuild. This value is relative to /app.
RUN mkdir -p /app/models
ENV PII_MODEL_DIR=models/rizzo-pii-0.3B-v1.5.0

# Environment variables take precedence because app/config.py loads .env with
# os.environ.setdefault. Change container values through the runtime environment,
# not by editing or mounting a development .env file.
#
# BLOCKINGBEAR_DATA_DIR is the application's internal persistent-data root. The
# supported Compose deployment binds the host data directory here. Sandbox
# containers do not bind nested host staging paths: each uses private tmpfs and
# the backend transfers inputs and artifacts through `docker exec` streams, so
# no host/container path parity is required.
ENV BLOCKINGBEAR_DATA_DIR=/srv/blockingbear/data
# Required by config.py. Production defaults use one document worker and disable
# CORS because the proxy serves a same-origin frontend. BLOCKINGBEAR_CPU_THREADS is
# deliberately absent so the process derives it from available container CPUs.
ENV BLOCKINGBEAR_N_WORKERS=1 \
    BLOCKINGBEAR_CORS_ORIGINS=""


# --- User ---------------------------------------------------------------------
# Run as non-root uid 1000. The process writes to a host bind mount, so incorrect
# ownership causes the first upload to fail with EACCES. Use --user when a
# standalone host requires another uid. LibreOffice also requires writable HOME.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin blockingbear \
    && mkdir -p /srv/blockingbear/data \
    && chown -R blockingbear:blockingbear /srv/blockingbear
ENV HOME=/home/blockingbear
USER 1000:1000

# The code interpreter also needs membership in the host Docker socket group.
# Its GID differs by host and is supplied at runtime with --group-add.


# --- Startup ------------------------------------------------------------------
EXPOSE 8000

# /api/health is intentionally unauthenticated. The generous start period covers
# Alembic migrations and queued model warm-up with a real inference.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5)"

# cwd is /app, so `app.main:app` imports without --app-dir. Bind 0.0.0.0 because
# container loopback is not reachable from other containers.
#
# Do not add uvicorn --workers. Application parallelism is internal and governed
# by BLOCKINGBEAR_N_WORKERS. A second process would duplicate the model and queue,
# while in-flight SSE buffers are process-local and reconnection could reach the
# wrong process. Scale document workers, not uvicorn processes. Proxy headers are
# unnecessary because application code does not trust or consume the client IP.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


# --- Standalone diagnostic launch --------------------------------------------
#
# The supported path is Compose from the repository root. It starts the database,
# backend, frontend, browser, and required mounts:
#
#     docker compose --env-file backend/.env up -d --build
#
# The commands below are only for isolated image diagnostics.
#
# Network shared with the frontend (nginx resolves the `backend` host):
#     docker network create blockingbear
#
# API and anonymization only, without code interpreter or web search:
#     docker run -d --name backend --network blockingbear \
#       -v /srv/blockingbear/data:/srv/blockingbear/data \
#       -v /srv/blockingbear/models/rizzo-pii-0.3B-v1.5.0:/app/models/rizzo-pii-0.3B-v1.5.0:ro \
#       -e DATABASE_URL=postgresql+psycopg://blockingbear:...@postgres:5432/blockingbear \
#       -p 127.0.0.1:8000:8000 \
#       blockingbear-backend:0.1.0
#
# With the code interpreter (build blockingbear-sandbox:1 on the HOST Engine):
#     ... -v /var/run/docker.sock:/var/run/docker.sock \
#         --group-add "$(getent group docker | cut -d: -f3)" ...
# Sandbox inputs and artifacts are streamed; the data directory does not need
# the same host and container path.
#
# KNOWN LIMITATIONS of an isolated image run:
#   - mounting /var/run/docker.sock grants complete host Engine control and is
#     root-equivalent on Linux;
#   - default container networking cannot reach a host-loopback Camofox. Compose
#     solves this with the `camofox` service name and BLOCKINGBEAR_CAMOFOX_URL;
#   - with PostgreSQL, backing up /srv/blockingbear/data alone is insufficient;
#     users, chats, and decoding maps live in the database.
