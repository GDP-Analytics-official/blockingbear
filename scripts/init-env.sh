#!/bin/sh
# Create backend/.env and generate local deployment secrets without printing them.
# Idempotent: preserve every existing entry, including source-development values,
# and fill only missing mandatory deployment secrets.

set -eu

fail() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

[ -f docker-compose.yaml ] && [ -f backend/.env.example ] || \
    fail "run this script from the repository root"

ENV_FILE=backend/.env
if [ ! -f "$ENV_FILE" ]; then
    cp backend/.env.example "$ENV_FILE"
    printf 'Created %s from backend/.env.example.\n' "$ENV_FILE"
fi

has_value() {
    grep -qE "^[[:space:]]*$1[[:space:]]*=[[:space:]]*[^[:space:]]" "$ENV_FILE"
}

random_hex() {
    bytes=$1
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex "$bytes"
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c "import secrets; print(secrets.token_hex($bytes))"
    elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        docker run --rm --network=none python:3.11-slim \
            python -c "import secrets; print(secrets.token_hex($bytes))"
    else
        fail "need openssl, python3, or a reachable Docker daemon to generate secrets"
    fi
}

set_value() {
    key=$1
    value=$2
    tmp=$(mktemp "backend/.env.tmp.XXXXXX") || fail "cannot create a temporary env file"
chmod 0600 "$tmp"
    written=no
    {
        while IFS= read -r line || [ -n "$line" ]; do
            case "$line" in
                "$key="*)
                    if [ "$written" = no ]; then
                        # printf is a shell built-in: the value never appears in
                        # an external process argument or in terminal output.
                        printf '%s=%s\n' "$key" "$value"
                        written=yes
                    fi
                    ;;
                *) printf '%s\n' "$line" ;;
            esac
        done < "$ENV_FILE"
        if [ "$written" = no ]; then
            printf '%s=%s\n' "$key" "$value"
        fi
    } > "$tmp" || {
        rm -f "$tmp"
        fail "cannot update $key"
    }
    mv "$tmp" "$ENV_FILE"
}

generated=""
if ! has_value BLOCKINGBEAR_PG_PASSWORD; then
    set_value BLOCKINGBEAR_PG_PASSWORD "$(random_hex 24)"
    generated="BLOCKINGBEAR_PG_PASSWORD"
fi
if ! has_value BLOCKINGBEAR_CAMOFOX_ACCESS_KEY; then
    set_value BLOCKINGBEAR_CAMOFOX_ACCESS_KEY "$(random_hex 48)"
    generated="${generated}${generated:+, }BLOCKINGBEAR_CAMOFOX_ACCESS_KEY"
fi
chmod 0600 "$ENV_FILE"

if [ -n "$generated" ]; then
    printf 'Generated locally (values not printed): %s.\n' "$generated"
else
    printf 'Local deployment secrets already exist; no values were changed.\n'
fi
printf 'OpenRouter is configured in the application setup wizard after the first start,\n'
printf 'never in .env or in an agent conversation.\n'
