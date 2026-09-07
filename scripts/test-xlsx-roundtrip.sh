#!/bin/sh
# Offline integration using the existing backend and sandbox images.
set -eu
test -f docker-compose.yaml
WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT HUP INT TERM
TEST_USER="$(id -u):$(id -g)"

phase() {
    image=$1
    mode=$2
    docker run --rm --network none --read-only --user "$TEST_USER" \
        --tmpfs /tmp:rw,size=512m \
        --env DATABASE_URL=sqlite:///:memory: \
        --env BLOCKINGBEAR_DATA_DIR=/tmp/test-data \
        --env PYTHONDONTWRITEBYTECODE=1 \
        --mount "type=bind,src=$PWD/backend/app,dst=/app/app,readonly" \
        --mount "type=bind,src=$PWD/backend/tests/xlsx_editor_roundtrip_test.py,dst=/test.py,readonly" \
        --mount "type=bind,src=$WORK_DIR,dst=/work" \
        --entrypoint python "$image" /test.py "$mode"
}

phase blockingbear-sandbox:1 create
phase blockingbear-backend:0.1.0 redact
phase blockingbear-sandbox:1 edit
phase blockingbear-backend:0.1.0 restore
phase blockingbear-sandbox:1 check
