#!/bin/sh
# Prove that the installation WORKS, which is not the same as containers being
# up. Exit zero only when there are no failures.
#
#   sh scripts/verify.sh
#
# Why this exists: `docker compose ps` can report healthy while the model is
# unusable. Each worker loads and executes a real inference in a background
# warm-up; `model_loaded` becomes true only after every first forward succeeds,
# including Triton compilation on CUDA. The sandbox follows the same contract:
# `available` is insufficient until Python runs and an artifact is retrieved.
#
# Overrides for deployments on different ports or addresses:
#   BLOCKINGBEAR_VERIFY_API       default http://127.0.0.1:8000/api/health
#   BLOCKINGBEAR_VERIFY_WEB       default http://127.0.0.1/; empty = skip
#   BLOCKINGBEAR_VERIFY_TIMEOUT   total wait in seconds, default 180
#
# Operator-facing output is English.

set -u

TIMEOUT=${BLOCKINGBEAR_VERIFY_TIMEOUT:-180}
API=${BLOCKINGBEAR_VERIFY_API:-http://127.0.0.1:8000/api/health}
WEB=${BLOCKINGBEAR_VERIFY_WEB-http://127.0.0.1/}

FAILED=0
WARNED=0

# Use colors only on a terminal; redirected output remains clean.
if [ -t 1 ]; then G='\033[32m'; R='\033[31m'; Y='\033[33m'; Z='\033[0m'
else G=''; R=''; Y=''; Z=''; fi

pass() { printf "  ${G}PASS${Z}  %s\n" "$1"; }
fail() { printf "  ${R}FAIL${Z}  %s\n" "$1"; FAILED=$((FAILED+1)); }
warn() { printf "  ${Y}WARN${Z}  %s\n" "$1"; WARNED=$((WARNED+1)); }
note() { printf '        %s\n' "$1"; }

# Use host curl when available, otherwise use the backend container so a
# Docker-only host can still run this verifier.
fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 10 "$1" 2>/dev/null
    else
        docker compose --env-file backend/.env exec -T backend \
            python -c "import sys,urllib.request;sys.stdout.write(urllib.request.urlopen('$1',timeout=10).read().decode())" 2>/dev/null
    fi
}

# Extract a top-level scalar. This is intentionally not a general JSON parser;
# the pass/fail fields are simple booleans.
field() {
    printf '%s' "$1" | tr ',' '\n' | grep -m1 "\"$2\"[[:space:]]*:" \
        | sed 's/^[^:]*:[[:space:]]*//;s/[[:space:]]*[]}]*$//;s/^"//;s/"$//'
}

# Use a wall-clock deadline because each loop also spends curl timeout time.
now()      { date +%s 2>/dev/null || echo 0; }
START=$(now)
expired()  { [ "$(( $(now) - START ))" -ge "$TIMEOUT" ]; }

printf '\nBlockingBear verify\n\n'

# --- 0. Verify repository identity -------------------------------------------
# Without this check, any service on 127.0.0.1:8000 could be certified as this
# installation even when the current clone never started.
if [ ! -f docker-compose.yaml ] || [ ! -d backend ]; then
    fail "not in the repository root: cannot tell which installation this is"
    note "cd into the clone and run: sh scripts/verify.sh"
    printf '\nNot working: 1 failure.\n\n'
    exit 1
fi
pass "running from the repository root"

# On Windows the daemon lives in Docker Desktop's utility VM. Confirm that the
# user-started Desktop Engine is running and this WSL2 distro is integrated.
VERIFY_OS=$(uname -s 2>/dev/null || echo unknown)
if [ "$VERIFY_OS" = Linux ] && grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
    docker_os=$(docker info --format '{{.OperatingSystem}}|{{.OSType}}' 2>/dev/null || echo "")
    case "$docker_os" in
        *"Docker Desktop"*"|linux") pass "Docker Desktop WSL2 Linux engine is reachable" ;;
        '') fail "Docker Desktop is not running or is not reachable from WSL2"
            note "open Docker Desktop manually from the Windows Start menu"
            note "wait until it reports 'Engine running', then re-run this verifier" ;;
        *) fail "this WSL2 distro is not connected to the Docker Desktop Linux engine"
           note "enable it in Docker Desktop > Resources > WSL Integration" ;;
    esac

fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    running=$(docker compose --env-file backend/.env ps --services --status running 2>/dev/null | tr '\n' ' ')
    case "$running" in
        *backend*) pass "this project's backend container is running" ;;
        '')        fail "no container of this project is running"
                   note "docker compose --env-file backend/.env ps -a" ;;
        *)         fail "the backend container of this project is not running (up: ${running% })"
                   note "docker compose --env-file backend/.env ps -a" ;;
    esac
fi

# --- 1. API response ----------------------------------------------------------
body=$(fetch "$API")
if [ -z "$body" ]; then
    printf '        waiting for the API (Alembic migrations run at startup)...\n'
    while [ -z "$body" ] && ! expired; do
        sleep 5
        body=$(fetch "$API")
    done
fi

if [ -z "$body" ]; then
    fail "the backend API never answered on $API within ${TIMEOUT}s"
    note "docker compose --env-file backend/.env ps -a"
    note "docker compose --env-file backend/.env logs --tail=100 backend"
    printf '\nNot working: %d failure(s).\n\n' "$FAILED"
    exit 1
fi
pass "backend API answers ($(( $(now) - START ))s)"

# --- 2. Model: the check that matters ----------------------------------------
loaded=$(field "$body" model_loaded)
if [ "$loaded" != "true" ]; then
    printf '        waiting for every PII worker to complete a real inference...\n'
    while [ "$loaded" != "true" ] && ! expired; do
        sleep 5
        body=$(fetch "$API")
        loaded=$(field "$body" model_loaded)
    done
fi

if [ "$loaded" = "true" ]; then
    pass "PII inference smoke test passed on every worker — anonymization works"
else
    fail "PII inference NOT ready after ${TIMEOUT}s: anonymization is dead"
    note "model_dir, as the backend sees it: $(field "$body" model_dir)"
    note "check first for a skipped or misplaced download on the HOST:"
    note "  ls backend/models/rizzo-pii-0.3B-v1.5.0/config.json"
    note "then: docker compose --env-file backend/.env logs --tail=50 backend"
    note "on NVIDIA, also check torch.compile/Triton and the C compiler in the log"
    note "NOTE: every container reports 'healthy' in this state. It is not working."
fi

# --- 3. Operational components -----------------------------------------------
if [ "$(field "$body" libreoffice)" = "true" ]; then
    pass "LibreOffice available — Word/Excel/PowerPoint previews work"
else
    warn "LibreOffice not available: PDFs work, OOXML previews do not"
fi

w=$(field "$body" workers)
case "$w" in
    ''|0)  warn "document workers: ${w:-unknown} — no document can be processed" ;;
    *)     pass "document workers: $w (each holds its own copy of the model)" ;;
esac

# Inspect only the sandbox object to avoid matching similarly named fields in
# other components. `available` proves only runtime/socket access;
# `smoke_tested` requires a real kernel execution and artifact retrieval.
sb=$(printf '%s' "$body" | sed -n 's/.*"sandbox"[^{]*{\([^}]*\)}.*/\1/p')
case "$sb" in
    *'"available":true'*'"smoke_tested":true'*|*'"available": true'*'"smoke_tested": true'*) ;;
    *)
        printf '        waiting for a real sandbox kernel smoke test...\n'
        while ! expired; do
            sleep 2
            body=$(fetch "$API")
            sb=$(printf '%s' "$body" | sed -n 's/.*"sandbox"[^{]*{\([^}]*\)}.*/\1/p')
            case "$sb" in
                *'"available":true'*'"smoke_tested":true'*|*'"available": true'*'"smoke_tested": true'*) break ;;
            esac
        done
        ;;
esac
case "$sb" in
    *'"available":true'*'"smoke_tested":true'*|*'"available": true'*'"smoke_tested": true'*)
        pass "code interpreter executed Python and retrieved an output artifact" ;;
    *'"available":true'*|*'"available": true'*)
        fail "sandbox runtime detected, but its real execution smoke test never passed"
        note "check the sandbox image, Docker exec access and tmpfs workspace permissions" ;;
    *)
        fail "required code interpreter sandbox is not available"
        note "re-run preflight: it verifies uid 1000 Engine access and saves the container-visible socket GID"
        note "then run its complete sandbox build/start commands so Compose recreates the backend"
        verify_os=$(uname -s 2>/dev/null || echo unknown)
        case "$verify_os" in
            Darwin) note "macOS: check Docker Desktop socket sharing; with ECI, allow the documented Python base and derived backend" ;;
            MINGW*|MSYS*|CYGWIN*) note "Windows: run this verifier inside the Docker Desktop-integrated WSL2 distro" ;;
            Linux)
                if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
                    note "Windows/WSL2: check Docker Desktop integration, socket access and any ECI socket policy"
                else
                    note "Linux: check the Docker socket path/GID and the sandbox image build"
                fi ;;
            *) note "check the Docker socket path/GID and the sandbox image build" ;;
        esac ;;
esac

# --- 4. SPA behind nginx ------------------------------------------------------
# The frontend builder exits 0 after publishing the SPA, and nginx starts only
# afterwards. A 404 means the SPA did not reach the volume. Override WEB when an
# external reverse proxy publishes another address.
if [ -n "$WEB" ] && command -v curl >/dev/null 2>&1; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$WEB")
    case "$code" in
        200) pass "the web tier serves the SPA at $WEB" ;;
        000) fail "nothing answered at $WEB"
             note "docker compose --env-file backend/.env ps -a"
             note "set BLOCKINGBEAR_VERIFY_WEB only when a reverse proxy uses another URL" ;;
        *)   fail "$WEB answered $code instead of 200: the SPA is not being served"
             note "docker compose --env-file backend/.env logs frontend" ;;
    esac
fi

# --- Verdict ------------------------------------------------------------------
printf '\n'
if [ "$FAILED" -gt 0 ]; then
    printf 'NOT working: %d failure(s), %d warning(s). Fix the FAIL lines above.\n\n' "$FAILED" "$WARNED"
    exit 1
fi

printf 'Working. %d warning(s). The local deployment, PII engine and sandbox are ready.\n\n' "$WARNED"
printf 'Required human steps:\n'
printf '  1. Open http://localhost (or this machine address).\n'
printf '  2. Complete the setup wizard shown on the sign-in page: interface language,\n'
printf '     OpenRouter Management API Key, password of the "admin" account, categories to\n'
printf '     anonymize, optional terms to always anonymize, chat rules, default model. Then sign in.\n'
printf '     Create the Management API Key in your own browser at:\n'
printf '       https://openrouter.ai/settings/management-keys\n'
printf '     It is not a normal inference key; the wizard rejects inference keys.\n'
printf '     Until the wizard is completed anyone who reaches the sign-in page can\n'
printf '     complete it and become the administrator: do it now.\n\n'
printf '  Never paste the key into an agent conversation, issue, .env or command.\n'
printf '  Details: docs/OPENROUTER.md\n\n'
printf 'INSTALLATION AGENT: stop here. The wizard is a human step in the browser.\n'
printf 'Do not ask the user for the key and do not try to complete the wizard.\n\n'
exit 0
