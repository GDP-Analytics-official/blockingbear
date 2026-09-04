# Code-interpreter sandbox image (see docs/SANDBOX.md).
# Build:  docker build -t blockingbear-sandbox:1 backend/sandbox
# The container runs with --read-only and --network=none. Runtime-writable data
# belongs in /tmp (tmpfs) or the private workspace.

FROM python:3.12-slim

# Headless LibreOffice for high-quality and legacy-format conversion. Liberation
# and DejaVu fonts keep output metrics consistent.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer libreoffice-calc libreoffice-impress \
        fonts-liberation fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Non-root user (runtime uses --user 1000:1000) and workspace mount points.
RUN useradd --uid 1000 --create-home sandbox \
    && mkdir -p /workspace/inputs /workspace/outputs \
    && chown -R sandbox:sandbox /workspace

# PATH-first wrapper forces LibreOffice's user profile into writable
# /tmp/lo-profile, allowing ordinary `soffice --headless ...` commands.
RUN printf '#!/bin/sh\nexec /usr/bin/soffice -env:UserInstallation=file:///tmp/lo-profile "$@"\n' \
        > /usr/local/bin/soffice \
    && chmod 755 /usr/local/bin/soffice

# PRE-GENERATE the LibreOffice profile at build time. kernel.py copies it from
# /opt/lo-profile to /tmp on startup, avoiding a 15-25 second cold bootstrap in
# every fresh sandbox.
RUN echo warmup > /tmp/warm.txt \
    && /usr/bin/soffice --headless -env:UserInstallation=file:///opt/lo-profile \
        --convert-to pdf --outdir /tmp /tmp/warm.txt \
    && test -f /tmp/warm.pdf \
    && rm /tmp/warm.txt /tmp/warm.pdf \
    && chmod -R a+rX /opt/lo-profile

# Pre-generate the matplotlib font cache in /opt and copy it to /tmp at startup.
ENV MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/mpl
RUN MPLCONFIGDIR=/opt/mpl-cache python -c "import matplotlib.pyplot" \
    && chmod -R a+rX /opt/mpl-cache

ENV PYTHONUNBUFFERED=1 \
    HOME=/tmp/home

COPY kernel.py /kernel.py

USER 1000:1000
WORKDIR /workspace
ENTRYPOINT ["python", "/kernel.py"]
