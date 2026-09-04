#!/bin/sh
# Check whether this machine can run BlockingBear in containers, and print the
# exact command to use. Exit 0 when everything is ready, 1 when something is missing.
#
#   sh scripts/preflight.sh
#
# Why this exists: all stack prerequisites used to be documented somewhere, but
# none were MACHINE-CHECKABLE — and the worst failures do not appear at startup.
# An unwritable data directory fails at first login; a missing model fails on the
# first document; an unreachable Docker daemon fails halfway through installation.
# An installer (human or agent) needs a command that answers yes/no BEFOREHAND,
# not prose that must be interpreted.
#
# This project supports container deployment only: a failure here must be fixed,
# not bypassed by starting the backend or frontend directly on the host.
#
# On Windows, run this from a WSL2 distro integrated with Docker Desktop. Docker
# Desktop owns the daemon and VM: do not install docker-ce inside the distro and
# do not keep Ubuntu artificially alive. macOS also uses Docker Desktop; the
# effective socket is detected below.
#
# Messages and comments are in English because installers read them and the
# README is in English. NEVER print the value of a secret.

set -u

FAIL=0
WARN=0
GPU=none
DOCKER_OK=no
DOCKER_OS=""
IS_DOCKER_DESKTOP=no
OS=$(uname -s 2>/dev/null || echo unknown)
IS_WSL=no
if [ "$OS" = Linux ] && grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
    IS_WSL=yes
fi

if [ -t 1 ]; then G='\033[32m'; R='\033[31m'; Y='\033[33m'; Z='\033[0m'
else G=''; R=''; Y=''; Z=''; fi

pass() { printf "  ${G}PASS${Z}  %s\n" "$1"; }
fail() { printf "  ${R}FAIL${Z}  %s\n" "$1"; FAIL=$((FAIL+1)); }
warn() { printf "  ${Y}WARN${Z}  %s\n" "$1"; WARN=$((WARN+1)); }
note() { printf '        %s\n' "$1"; }

# Keep this identical to the final base in backend/Dockerfile. Docker Desktop
# ECI can then allow this registry image with allowDerivedImages=true, covering
# both the probe and the locally built backend image.
PROBE_IMAGE=python:3.11-slim-bookworm
PROBE_IMAGE_READY=unknown
ensure_probe_image() {
    case "$PROBE_IMAGE_READY" in
        yes) return 0 ;;
        no)  return 1 ;;
    esac
    if docker image inspect "$PROBE_IMAGE" >/dev/null 2>&1; then
        PROBE_IMAGE_READY=yes
        return 0
    fi
    note "downloading $PROBE_IMAGE for container permission probes"
    if docker pull "$PROBE_IMAGE"; then
        PROBE_IMAGE_READY=yes
        return 0
    fi
    PROBE_IMAGE_READY=no
    return 1
}

printf '\nBlockingBear preflight (container deployment)\n\n'

# --- 1. Verify the working directory ------------------------------------------
# All Compose paths are relative to its directory, so running elsewhere can mount
# the wrong directories without producing an error.
if [ -f docker-compose.yaml ] && [ -d backend ]; then
    pass "running from the repository root"
else
    fail "not in the repository root: docker-compose.yaml and backend/ must be here"
    note "cd into the clone and run: sh scripts/preflight.sh"
    printf '\nBlocked: 1 check failed.\n\n'
    exit 1
fi

# Docker Engine bind mounts inside WSL must originate on the Linux filesystem.
# On /mnt/c (DrvFS/9p), modes reported by stat are synthetic, chmod does not have
# full Unix semantics, and the build context is much slower. A 777 here could
# therefore report PASS while leaving the uid 1000 backend unable to write.
if [ "$IS_WSL" = yes ]; then
    ROOT_DIR=$(pwd -P 2>/dev/null || pwd)
    ROOT_FS=$(stat -f -c '%T' . 2>/dev/null || echo unknown)
    case "$ROOT_DIR/" in
        /mnt/[A-Za-z]/*)
            fail "the repository is on a Windows-mounted drive ($ROOT_DIR)"
            note "clone or move it into the WSL Linux filesystem, for example:"
            note "  cd \"\$HOME\" && git clone <repository-url> blockingbear"
            note "DrvFS permissions can look writable while uid 1000 bind mounts are not"
            ;;
        *)
            case "$ROOT_FS" in
                9p|drvfs|DrvFS)
                    fail "the repository filesystem is $ROOT_FS (Windows/DrvFS), not the WSL Linux filesystem"
                    note "clone or move it below \$HOME inside WSL, then re-run preflight"
                    ;;
                *) pass "repository is on the WSL Linux filesystem ($ROOT_DIR)" ;;
            esac
            ;;
    esac
fi

# --- Effective paths, not their defaults --------------------------------------
# backend/.env can relocate the data and model directories. Checking ./data when
# Compose mounts another path would produce a PASS for an irrelevant directory.
# Read PATHS only, never secrets.
envval() {
    [ -f backend/.env ] || return 0
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" backend/.env \
        | tail -1 | sed 's/^"//;s/"$//;s/^'\''//;s/'\''$//'
}

# Persist host-specific, non-secret sandbox values so every later Compose
# command works with --env-file backend/.env, even from a new shell.
persist_sandbox_env() {
    [ -f backend/.env ] || return 1
    tmp=$(mktemp "backend/.env.tmp.XXXXXX") || return 1
    awk -v socket="$DOCKER_SOCKET" -v gid="$SOCKET_GID" '
        BEGIN { wrote_socket = 0; wrote_gid = 0 }
        /^[[:space:]]*BLOCKINGBEAR_DOCKER_SOCKET[[:space:]]*=/ {
            if (!wrote_socket) { print "BLOCKINGBEAR_DOCKER_SOCKET=" socket; wrote_socket = 1 }
            next
        }
        /^[[:space:]]*BLOCKINGBEAR_DOCKER_GID[[:space:]]*=/ {
            if (!wrote_gid) { print "BLOCKINGBEAR_DOCKER_GID=" gid; wrote_gid = 1 }
            next
        }
        { print }
        END {
            if (!wrote_socket) print "BLOCKINGBEAR_DOCKER_SOCKET=" socket
            if (!wrote_gid) print "BLOCKINGBEAR_DOCKER_GID=" gid
        }
    ' backend/.env > "$tmp" || {
        rm -f "$tmp"
        return 1
    }
    if chmod 0600 "$tmp" && mv "$tmp" backend/.env; then
        return 0
    fi
    rm -f "$tmp"
    return 1
}

DATA_DIR_HOST=$(envval BLOCKINGBEAR_DATA_DIR_HOST); : "${DATA_DIR_HOST:=./data}"
MODEL_DIR=$(envval BLOCKINGBEAR_MODEL_DIR_HOST);    : "${MODEL_DIR:=./backend/models/rizzo-pii-0.3B-v1.5.0}"

# --- 2. Docker, especially a daemon REACHABLE by this user --------------------
# "Docker installed" is insufficient: on a newly prepared host the user may not
# belong to the docker group, so every command fails with permission denied. This
# common failure has TWO forms that require different fixes.
if ! command -v docker >/dev/null 2>&1; then
    fail "the 'docker' command is not installed"
    if [ "$IS_WSL" = yes ]; then
        note "install Docker Desktop on Windows and enable this distro under Resources > WSL Integration"
        note "official download and instructions: https://docs.docker.com/desktop/setup/install/windows-install/"
        note "then start Docker Desktop manually and wait until it reports 'Engine running'"
    else
        case "$OS" in
            Darwin)
                note "install Docker Desktop for Mac"
                note "official download (choose Apple silicon or Intel): https://docs.docker.com/desktop/setup/install/mac-install/"
                note "then open Docker.app and wait until Docker Desktop reports 'Engine running'"
                ;;
            Linux)
                note "install Docker Engine for your Linux distribution"
                note "official instructions: https://docs.docker.com/engine/install/"
                note "then start the Docker service and re-run this preflight"
                ;;
            MINGW*|MSYS*|CYGWIN*)
                note "Windows deployment must run inside a Docker Desktop-integrated WSL2 distro"
                note "official download and instructions: https://docs.docker.com/desktop/setup/install/windows-install/"
                note "enable the distro under Resources > WSL Integration, then run this script there"
                ;;
            *)
                note "unsupported or unrecognized operating system: $OS"
                ;;
        esac
    fi
else
    pass "docker command found ($(docker --version 2>/dev/null | cut -d, -f1))"
    if docker info >/dev/null 2>&1; then
        pass "docker daemon is reachable by this user"
        DOCKER_OK=yes
        DOCKER_OS=$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || echo "")
        case "$DOCKER_OS" in *"Docker Desktop"*) IS_DOCKER_DESKTOP=yes ;; esac
    else
        err=$(docker info 2>&1)
        case "$err" in
            *"permission denied"*)
                if [ "$IS_WSL" = yes ]; then
                    fail "this WSL2 distro cannot access the Docker Desktop Engine"
                    note "restart Docker Desktop and re-enable this distro under Resources > WSL Integration"
                    note "reopen the WSL2 shell, then re-run this preflight"
                else
                    case "$OS" in
                        Darwin)
                            fail "this macOS user cannot access the Docker Desktop socket"
                            note "restart Docker Desktop and enable 'Allow the default Docker socket' in its settings"
                            note "then open a new Terminal session and re-run this preflight"
                            ;;
                        Linux)
                            # Native Docker Engine uses the docker group. A stale
                            # login and missing membership need different fixes.
                            me=$(id -un 2>/dev/null || echo "")
                            grp=$(getent group docker 2>/dev/null || echo "")
                            if [ -n "$me" ] && printf '%s' "$grp" | grep -q "[:,]$me\(,\|$\)"; then
                                fail "your user IS in the docker group, but this session predates it"
                                note "no privileges needed - start a shell that has the group:"
                                note "  newgrp docker        # then re-run this script"
                                note "or simply log out and back in"
                            else
                                fail "the Docker Engine is running but this Linux user cannot reach it"
                                note "add the user to the docker group, then start a NEW session:"
                                note "  sudo usermod -aG docker \"\$USER\""
                                note "  newgrp docker        # or log out and back in"
                                note "this needs a password: an unattended run must be handed a user"
                                note "that is already in the group"
                            fi
                            ;;
                        MINGW*|MSYS*|CYGWIN*)
                            fail "Windows deployment must run inside a Docker Desktop-integrated WSL2 distro"
                            note "enable the distro under Resources > WSL Integration, then run this script there"
                            ;;
                        *) fail "this user cannot access the Docker daemon" ;;
                    esac
                fi
                ;;
            *"Cannot connect"*|*"refused"*|*"not running"*|*"pipe"*)
                fail "the docker daemon is not running"
                if [ "$IS_WSL" = yes ]; then
                    note "open Docker Desktop manually from the Windows Start menu"
                    note "wait until Docker Desktop reports 'Engine running', then re-run this preflight"
                else
                    case "$OS" in
                        Darwin)
                            note "open Docker.app from Applications"
                            note "wait until Docker Desktop reports 'Engine running', then re-run this preflight"
                            ;;
                        Linux)
                            note "start the Docker Engine service, then re-run this preflight"
                            ;;
                        MINGW*|MSYS*|CYGWIN*)
                            note "open Docker Desktop, then run this preflight inside its integrated WSL2 distro"
                            ;;
                        *) note "start the Docker daemon, then re-run this preflight" ;;
                    esac
                fi
                ;;
            *)
                fail "docker info failed"
                printf '%s' "$err" | grep -iE 'error|denied|refused' | head -2 | sed 's/^/        /'
                if [ "$IS_WSL" = yes ]; then
                    note "if Docker Desktop is closed, start it manually, wait for 'Engine running', and retry"
                else
                    case "$OS" in
                        Darwin) note "if Docker Desktop is closed, open Docker.app, wait for 'Engine running', and retry" ;;
                        Linux)  note "inspect the native Docker Engine service and socket, then retry" ;;
                        MINGW*|MSYS*|CYGWIN*) note "run this preflight inside a Docker Desktop-integrated WSL2 distro" ;;
                    esac
                fi
                ;;
        esac
    fi
fi

# A build can succeed below the documented target and fail later as layers or
# model tooling grow. Report the daemon/VM allocation, not host physical RAM.
if [ "$DOCKER_OK" = yes ]; then
    docker_memory=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo "")
    case "$docker_memory" in
        ''|*[!0-9]*) ;;
        *)
            docker_memory_tenths=$((docker_memory * 10 / 1073741824))
            docker_memory_label=$(printf '%d.%d' \
                "$((docker_memory_tenths / 10))" "$((docker_memory_tenths % 10))")
            if [ "$docker_memory" -lt 8589934592 ] 2>/dev/null; then
                warn "Docker exposes ${docker_memory_label} GiB RAM; at least 8 GiB is recommended"
                note "the current build may pass, but a future build can be OOM-killed with exit 137"
                case "$OS:$IS_WSL" in
                    Darwin:*)
                        note "VM overhead can make an 8 GiB allocation appear smaller; allocate slightly more than 8 GiB"
                        ;;
                    Linux:yes)
                        note "raise the Docker Desktop/WSL2 memory allocation above 8 GiB before relying on this installation"
                        ;;
                    *)
                        [ "$IS_DOCKER_DESKTOP" = yes ] && \
                            note "raise the Docker Desktop VM memory allocation above 8 GiB"
                        ;;
                esac
            else
                pass "Docker exposes ${docker_memory_label} GiB RAM (>= 8 GiB recommended)"
            fi
            ;;
    esac
fi

# --- 3. Compose version --------------------------------------------------------
# The base file's `configs: content:` block was introduced in 2.23.1; older
# versions reject the file outright.
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    cv=$(docker compose version --short 2>/dev/null | tr -d 'v')
    maj=$(printf '%s' "$cv" | cut -d. -f1)
    min=$(printf '%s' "$cv" | cut -d. -f2)
    pat=$(printf '%s' "$cv" | cut -d. -f3 | cut -d- -f1)
    : "${pat:=0}"
    if [ "$maj" -gt 2 ] 2>/dev/null || \
       { [ "$maj" -eq 2 ] && [ "$min" -gt 23 ]; } 2>/dev/null || \
       { [ "$maj" -eq 2 ] && [ "$min" -eq 23 ] && [ "$pat" -ge 1 ]; } 2>/dev/null; then
        pass "docker compose $cv (>= 2.23.1 required)"
    else
        fail "docker compose $cv is too old: 2.23.1 or newer is required"
        note "the compose file uses an inline 'configs: content:' block"
    fi
else
    fail "'docker compose' (v2 plugin) not available"
    note "the old 'docker-compose' python script will not work: this project needs the plugin"
fi

# --- 4. Configuration file and the two mandatory Compose variables ------------
if [ -f backend/.env ]; then
    pass "backend/.env exists"
    for v in BLOCKINGBEAR_PG_PASSWORD BLOCKINGBEAR_CAMOFOX_ACCESS_KEY; do
        # Check presence and non-empty values only; never print the value.
        if grep -qE "^[[:space:]]*$v[[:space:]]*=[[:space:]]*[^[:space:]]" backend/.env; then
            pass "$v is set"
        else
            fail "$v is missing or empty in backend/.env"
            note "AUTHORIZED ACTION: generate missing local secrets without asking the user:"
            note "  sh scripts/init-env.sh"
        fi
    done
else
    fail "backend/.env is missing"
    note "AUTHORIZED ACTION: create it and generate both local secrets without asking:"
    note "  sh scripts/init-env.sh"
fi

# --- 5. Compose interpolation --------------------------------------------------
# This does not require the daemon, and errors identify the missing variable.
# Discard stdout deliberately because it contains interpolated secrets. The
# unpredictable mktemp file captures stderr only.
if command -v docker >/dev/null 2>&1 && [ -f backend/.env ]; then
    tmp=$(mktemp 2>/dev/null || echo "./.preflight.$$")
    if docker compose --env-file backend/.env config >/dev/null 2>"$tmp"; then
        pass "docker-compose.yaml interpolates cleanly"
    else
        fail "docker compose config failed:"
        sed 's/^/        /' "$tmp" | head -6
    fi
    rm -f "$tmp"
fi

# --- 6. Data directory: must exist and be writable by the container uid --------
# The backend runs as uid 1000 (backend/Dockerfile) and writes the JWT secret here
# at FIRST LOGIN. If unwritable, the stack starts healthy but login returns 500.
if [ ! -d "$DATA_DIR_HOST" ]; then
    fail "the data directory does not exist: $DATA_DIR_HOST"
    if [ "$IS_WSL" = yes ]; then
        if [ "$(id -u 2>/dev/null || echo unknown)" = 1000 ]; then
            note "mkdir -p \"$DATA_DIR_HOST\" && chmod 0700 \"$DATA_DIR_HOST\""
        else
            note "sudo install -d -o 1000 -g 1000 -m 0700 \"$DATA_DIR_HOST\""
            note "the backend runs as uid 1000; this WSL user has a different uid"
        fi
    else
        case "$OS" in
            Darwin)
                note "mkdir -p \"$DATA_DIR_HOST\""
                note "keep it on a local path shared with Docker Desktop"
                ;;
            MINGW*|MSYS*|CYGWIN*)
                note "run this script inside the integrated WSL2 distro, not from a Windows shell"
                ;;
            Linux)
                if [ "$(id -u 2>/dev/null || echo unknown)" = 1000 ]; then
                    note "mkdir -p \"$DATA_DIR_HOST\" && chmod 0700 \"$DATA_DIR_HOST\""
                else
                    note "sudo install -d -o 1000 -g 1000 -m 0700 \"$DATA_DIR_HOST\""
                    note "without administrator access: mkdir -p \"$DATA_DIR_HOST\" && chmod 0777 \"$DATA_DIR_HOST\"   # world-writable fallback"
                fi
                ;;
            *)
                note "create \"$DATA_DIR_HOST\" and make it writable by uid 1000"
                ;;
        esac
    fi
else
    # The authoritative test is a REAL write through the same bind mount and with
    # the backend uid. This covers DrvFS, ACLs, rootless Docker, SELinux, and
    # Docker Desktop, where stat mode bits may lie or tell only part of the story.
    # The probe file is removed inside the container.
    if [ "$DOCKER_OK" = yes ]; then
        DATA_DIR_ABS=$(cd "$DATA_DIR_HOST" 2>/dev/null && pwd -P)
        if ! ensure_probe_image; then
            fail "cannot download the permission-probe image ($PROBE_IMAGE)"
            note "check Docker registry access, then re-run this preflight"
        elif [ -n "$DATA_DIR_ABS" ] && docker run --rm --pull=never --user 1000:1000 \
                -v "$DATA_DIR_ABS:/probe" "$PROBE_IMAGE" sh -c \
                'p=/probe/.blockingbear-write-probe; : > "$p" && rm -f "$p"' \
                >/dev/null 2>&1; then
            pass "$DATA_DIR_HOST accepts a real uid 1000 container write"
        else
            fail "$DATA_DIR_HOST is not writable by uid 1000 through Docker"
            if [ "$IS_WSL" = yes ]; then
                note "keep the repository below \$HOME in the WSL2 Linux filesystem, not below /mnt/c"
                note "sudo chown 1000:1000 \"$DATA_DIR_HOST\" && chmod 0700 \"$DATA_DIR_HOST\""
                note "without administrator access: chmod 0777 \"$DATA_DIR_HOST\"   # world-writable fallback"
            else
                case "$OS" in
                    Darwin)
                        note "keep the repository on a local path shared with Docker Desktop"
                        note "check Docker Desktop file-sharing permissions, then retry"
                        ;;
                    Linux)
                        note "sudo chown 1000:1000 \"$DATA_DIR_HOST\" && chmod 0700 \"$DATA_DIR_HOST\""
                        note "a shared host may instead use chmod 0777, accepting world-write access"
                        ;;
                    MINGW*|MSYS*|CYGWIN*)
                        note "run this script inside the integrated WSL2 distro, not from a Windows shell"
                        ;;
                esac
            fi
            note "skipping this makes the FIRST LOGIN fail with a 500, not the startup"
        fi
    else
        case "$OS" in
            Darwin|MINGW*|MSYS*|CYGWIN*)
                warn "$DATA_DIR_HOST exists, but Docker is unavailable so uid 1000 write access was not tested"
                ;;
            *)
            owner=$(stat -c '%u' "$DATA_DIR_HOST" 2>/dev/null || echo "")
            group=$(stat -c '%g' "$DATA_DIR_HOST" 2>/dev/null || echo "")
            mode=$(stat -c '%a' "$DATA_DIR_HOST" 2>/dev/null || echo "")
            # Keep the last three digits so a four-digit mode (sticky/setgid)
            # does not shift the fields.
            m3=$(printf '%s' "$mode" | tail -c3)
            uo=$(printf '%s' "$m3" | cut -c1)
            gr=$(printf '%s' "$m3" | cut -c2)
            ot=$(printf '%s' "$m3" | cut -c3)
            writable=no
            # uid 1000 can write when it owns the directory and user-write is set;
            # when its primary group (1000) owns it and group-write is set; or
            # when other-write is set.
            [ "$owner" = "1000" ] && [ -n "$uo" ] && [ "$((uo & 2))" -ne 0 ] 2>/dev/null && writable=yes
            [ "$group" = "1000" ] && [ -n "$gr" ] && [ "$((gr & 2))" -ne 0 ] 2>/dev/null && writable=yes
            [ -n "$ot" ] && [ "$((ot & 2))" -ne 0 ] 2>/dev/null && writable=yes
            if [ "$writable" = yes ]; then
                pass "$DATA_DIR_HOST is writable by uid 1000 (owner $owner, group $group, mode $mode)"
            else
                fail "$DATA_DIR_HOST is NOT writable by uid 1000 (owner $owner, group $group, mode $mode)"
                note "chmod 0777 \"$DATA_DIR_HOST\"                     # no privileges, world-writable"
                note "sudo chown 1000:1000 \"$DATA_DIR_HOST\" && chmod 0700 \"$DATA_DIR_HOST\"   # preferred"
                note "that directory holds original document bytes and the JWT secret"
                note "skipping this makes the FIRST LOGIN fail with a 500, not the startup"
            fi
                ;;
        esac
    fi
fi

# --- 7. Model: the mounted directory must contain the checkpoint ---------------
# If absent, Docker creates an EMPTY bind-mount source and the stack starts
# healthy with a dead engine. This is the main silent failure, so an incomplete
# download is a FAIL rather than a warning: the stack would otherwise look green.
if [ -f "$MODEL_DIR/config.json" ]; then
    sz=$(du -sm "$MODEL_DIR" 2>/dev/null | cut -f1)
    if [ -n "$sz" ] && [ "$sz" -ge 900 ] 2>/dev/null; then
        pass "PII model present in $MODEL_DIR (${sz} MB)"
    else
        fail "PII model in $MODEL_DIR looks incomplete (${sz:-?} MB, expected ~1200)"
        note "an interrupted download leaves config.json without the weights, and the"
        note "stack would still come up healthy. Re-run the download."
    fi
else
    fail "no PII model in $MODEL_DIR (config.json missing)"
    note "download it (needs no Python on the host):"
    note "  mkdir -p backend/models"
    note "  docker run --rm --mount \"type=bind,src=\$PWD/backend/models,dst=/models\" python:3.11-slim sh -c \"pip install -q huggingface_hub && hf download rizzoaiacademy/rizzo-pii-0.3B --revision v1.5.0 --local-dir /models/rizzo-pii-0.3B-v1.5.0\""
    note "WITHOUT IT the stack still starts and reports healthy, and anonymization is dead"
fi

# --- 8. Ports ------------------------------------------------------------------
# nginx publishes 0.0.0.0:80 at a fixed address. It is not configurable, so a
# busy port is not a warning; it prevents startup.
CAN_CHECK_PORTS=yes
if command -v ss >/dev/null 2>&1; then
    port_busy() { ss -ltn 2>/dev/null | grep -qE "[:.]$1 "; }
elif command -v lsof >/dev/null 2>&1; then
    port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
elif command -v netstat >/dev/null 2>&1; then
    port_busy() { netstat -an 2>/dev/null | grep -qE "[:.]$1[[:space:]].*LISTEN"; }
else
    CAN_CHECK_PORTS=no
    port_busy() { return 1; }
fi
if [ "$CAN_CHECK_PORTS" = no ]; then
    warn "cannot check ports: no ss, lsof or netstat on this host"
    note "port 80 must be free — nginx publishes it and it is not configurable"
else
    for p in 80; do
        if port_busy "$p"; then
            own_port=no
            # Preflight must remain idempotent: a second `up -d` correctly
            # reconciles nginx already started from THIS clone. Compose labels
            # bind the container to both its service and working directory, so
            # another project using port 80 is not mistaken for ours.
            if [ "$DOCKER_OK" = yes ]; then
                ROOT_DIR=$(pwd -P 2>/dev/null || pwd)
                own_ids=$(docker ps \
                    --filter "label=com.docker.compose.project.working_dir=$ROOT_DIR" \
                    --filter "label=com.docker.compose.service=nginx" \
                    --format '{{.ID}}' 2>/dev/null)
                for own_id in $own_ids; do
                    if docker port "$own_id" 80/tcp 2>/dev/null | grep -q ':80$'; then
                        own_port=yes
                        break
                    fi
                done
            fi
            if [ "$own_port" = yes ]; then
                pass "port $p is already held by this repository's running nginx"
                note "the printed docker compose up command will reconcile it idempotently"
            else
                fail "port $p is already in use by something outside this repository"
                note "startup will fail with 'port is already allocated'; stop the other service"
            fi
        else
            pass "port $p is free"
        fi
    done
fi

# --- 9. Disk space -------------------------------------------------------------
# This measures the current directory's filesystem. On Docker Desktop, images
# live in the VM's virtual disk, whose separate limit is not visible here.
avail=$(df -Pm . 2>/dev/null | awk 'END{print $4}')
if [ -n "$avail" ]; then
    if [ "$avail" -ge 12000 ] 2>/dev/null; then
        pass "disk space here: ${avail} MB free"
    elif [ "$avail" -ge 6000 ] 2>/dev/null; then
        warn "disk space here: ${avail} MB free — enough for the CPU build, not for a CUDA one (~15 GB)"
    else
        fail "disk space here: only ${avail} MB free; the CPU build needs about 6 GB"
    fi
    case "$OS" in Darwin|MINGW*|MSYS*|CYGWIN*)
        note "on desktop platforms the images live in the container runtime VM's disk image"
        note "that separate limit is not visible here — check the VM storage settings" ;;
    esac
fi

# --- 10. Hardware: select the correct command ---------------------------------
# This is why the script is useful: it chooses between the universal CPU path
# and NVIDIA without asking the installer to interpret drivers and runtimes. If
# NVIDIA hardware is present, the GPU path is mandatory: no CPU fallback. AMD
# remains explicitly on the CPU path.
printf '\nHardware\n\n'

# The backend always uses /var/run/docker.sock INSIDE the container, but its host
# source can differ: Docker Desktop on macOS may use
# $HOME/.docker/run/docker.sock when the global symlink is disabled. The group
# consumed by Compose is the socket GID AS SEEN INSIDE the backend container;
# desktop VM mount layers can map it differently from the host-side inode.
DOCKER_SOCKET=""
SOCKET_GID=""
resolve_sandbox_socket() {
    case "${DOCKER_HOST:-}" in
        unix://*) DOCKER_SOCKET=${DOCKER_HOST#unix://} ;;
    esac
    if [ -z "$DOCKER_SOCKET" ] || [ ! -S "$DOCKER_SOCKET" ]; then
        if [ -S /var/run/docker.sock ]; then
            DOCKER_SOCKET=/var/run/docker.sock
        elif [ "$OS" = Darwin ] && [ -S "$HOME/.docker/run/docker.sock" ]; then
            DOCKER_SOCKET="$HOME/.docker/run/docker.sock"
        fi
    fi
    if [ -z "$DOCKER_SOCKET" ] || [ ! -S "$DOCKER_SOCKET" ]; then
        fail "the required Docker socket could not be found"
        if [ "$OS" = Darwin ]; then
            note "enable 'Allow the default Docker socket' in Docker Desktop, or set DOCKER_HOST"
        else
            note "the sandbox needs a local Unix Docker Engine socket"
        fi
        return
    fi

    # Use the same small image as the data-directory check. ensure_probe_image
    # announces and performs a pull when needed, so this step never appears hung.
    if ! ensure_probe_image; then
        fail "cannot download the Docker socket probe image ($PROBE_IMAGE)"
        note "check Docker registry access, then re-run this preflight"
        return
    fi

    socket_probe=$(docker run --rm --pull=never \
        -v "$DOCKER_SOCKET:/var/run/docker.sock" \
        "$PROBE_IMAGE" \
        python -c 'import os; print(os.stat("/var/run/docker.sock").st_gid)' \
        2>&1) || {
        fail "the Docker socket cannot be mounted into the sandbox backend"
        note "$(printf '%s' "$socket_probe" | tail -1)"
        if [ "$OS" = Darwin ]; then
            note "Docker Desktop ECI: allow python:3.11-slim-bookworm with allowDerivedImages=true"
            note "see https://docs.docker.com/enterprise/security/hardened-desktop/enhanced-container-isolation/config/"
        fi
        return
    }
    SOCKET_GID=$(printf '%s' "$socket_probe" | tail -1)
    case "$SOCKET_GID" in
        ''|*[!0-9]*)
            fail "the container-visible Docker socket GID is invalid: ${SOCKET_GID:-<empty>}"
            return
            ;;
    esac

    # Reproduce the backend identity and group_add, then perform a real Engine
    # ping over the mounted Unix socket. Merely checking mode bits would miss
    # desktop mount translation and security policies.
    socket_access=$(docker run --rm --pull=never \
        --user 1000:1000 --group-add "$SOCKET_GID" \
        -v "$DOCKER_SOCKET:/var/run/docker.sock" \
        "$PROBE_IMAGE" python -c 'import socket
s = socket.socket(socket.AF_UNIX)
s.settimeout(5)
s.connect("/var/run/docker.sock")
s.sendall(b"GET /_ping HTTP/1.0\r\nHost: docker\r\nConnection: close\r\n\r\n")
reply = s.makefile("rb").read()
raise SystemExit(0 if b"200 OK" in reply and reply.rstrip().endswith(b"OK") else 1)' \
        2>&1) || {
        fail "uid 1000 cannot use the Docker socket with its container-visible GID $SOCKET_GID"
        [ -n "$socket_access" ] && note "$(printf '%s' "$socket_access" | tail -1)"
        if [ "$OS" = Darwin ]; then
            note "Docker Desktop ECI: allow python:3.11-slim-bookworm with allowDerivedImages=true"
            note "this covers both this probe and the locally built backend image"
        else
            note "check the Docker socket permissions and daemon policy"
        fi
        return
    }

    pass "sandbox socket works inside a uid 1000 container at $DOCKER_SOCKET (GID $SOCKET_GID)"
    if persist_sandbox_env; then
        pass "verified sandbox socket settings saved in backend/.env for future Compose commands"
    else
        fail "cannot save the sandbox socket settings in backend/.env"
        note "check that backend/.env exists and is writable, then re-run this preflight"
    fi
}

case "$OS" in
    Darwin)
        resolve_sandbox_socket
        pass "macOS: containers are CPU-only here (Metal has no passthrough)"
        note "arm64 is fully supported: no --platform, no emulation"
        note "Docker Desktop sandbox is supported; ECI needs the documented derived-image socket exception"
        note "the Docker VM must expose at least 8 GiB; allocate slightly more to cover VM overhead"
        GPU=none
        ;;
    MINGW*|MSYS*|CYGWIN*)
        fail "Windows deployment scripts must run inside a Docker Desktop-integrated WSL2 distro"
        note "open the WSL2 distro enabled under Docker Desktop > Resources > WSL Integration"
        note "keep the repository below the distro's $HOME and re-run this script there"
        GPU=none
        ;;
    *)
        if [ "$IS_WSL" = yes ]; then
            if [ "$IS_DOCKER_DESKTOP" = yes ]; then
                pass "WSL2 is connected to the Docker Desktop Linux engine"
                resolve_sandbox_socket
                note "do not install docker-ce or NVIDIA Container Toolkit inside this distro"
            else
                fail "Windows requires the Docker Desktop WSL2 Linux engine"
                note "docker info reports: ${DOCKER_OS:-<unreachable>} (expected: Docker Desktop)"
                note "if it reports Ubuntu/Linux, a conflicting native Docker daemon is active"
                note "back up any native Docker volumes or databases before changing daemons"
                note "then remove the native Docker Engine/CLI packages from this WSL distro"
                note "start Docker Desktop manually and wait for 'Engine running'"
                note "enable this distro in Docker Desktop > Resources > WSL Integration"
                note "choose 'Apply & restart', reopen WSL, and re-run this preflight"
                note "if WSL Integration is absent, switch Docker Desktop to Linux containers"
            fi
        elif [ "$IS_DOCKER_DESKTOP" = yes ]; then
            fail "native Linux deployment requires Docker Engine, not Docker Desktop"
            note "install native Docker Engine for this Linux host: https://docs.docker.com/engine/install/"
        else
            resolve_sandbox_socket
        fi

        NVIDIA_SMI=$(command -v nvidia-smi 2>/dev/null || true)
        if [ -z "$NVIDIA_SMI" ] && [ -x /usr/lib/wsl/lib/nvidia-smi ]; then
            NVIDIA_SMI=/usr/lib/wsl/lib/nvidia-smi
        fi
        if [ -z "$NVIDIA_SMI" ]; then
            if [ -e /dev/kfd ] && [ -e /dev/dri ]; then
                pass "AMD GPU detected: ROCm is not supported for now; using CPU"
                note "AMD processors and machines are supported normally through the CPU path"
            else
                pass "no GPU detected: CPU is the right path (and the default)"
            fi
            GPU=none
        elif ! nvidia_out=$("$NVIDIA_SMI" -L 2>&1) || [ -z "$nvidia_out" ] || \
             ! printf '%s' "$nvidia_out" | grep -q '^GPU 0'; then
            fail "nvidia-smi is installed but reports no usable GPU"
            note "first line was: $(printf '%s' "${nvidia_out:-<empty>}" | head -1)"
            note "repair/install the host NVIDIA driver, then re-run this script"
            note "do not continue with the CPU profile when an NVIDIA GPU is present"
            GPU=none
        elif ! docker info >/dev/null 2>&1; then
            fail "NVIDIA GPU detected, but the toolkit cannot be checked: the daemon is unreachable"
            note "fix the daemon FAIL above, then re-run; CPU fallback is not allowed"
            note "$(printf '%s' "$nvidia_out" | head -1)"
            GPU=none
        else
            pass "NVIDIA GPU detected: $(printf '%s' "$nvidia_out" | head -1)"
            if [ "$IS_DOCKER_DESKTOP" = yes ]; then
                NVIDIA_TEST_IMAGE=${BLOCKINGBEAR_NVIDIA_TEST_IMAGE:-nvidia/cuda:12.6.3-base-ubuntu22.04}
                note "verifying GPU passthrough with $NVIDIA_TEST_IMAGE (the first run may pull it)"
                gpu_status=0
                gpu_probe=$(docker run --rm --gpus all "$NVIDIA_TEST_IMAGE" nvidia-smi -L 2>&1) || gpu_status=$?
                gpu_line=$(printf '%s\n' "$gpu_probe" | grep '^GPU 0' | head -1)
                if [ "$gpu_status" -eq 0 ] && [ -n "$gpu_line" ]; then
                    pass "Docker Desktop NVIDIA passthrough works: $gpu_line"
                    GPU=nvidia
                else
                    fail "Windows sees NVIDIA, but Docker Desktop GPU passthrough failed"
                    note "update the Windows NVIDIA driver, run 'wsl --update', and use Docker Desktop's WSL2 engine"
                    note "do not install NVIDIA Container Toolkit inside Ubuntu"
                    note "first probe line: $(printf '%s' "${gpu_probe:-<empty>}" | head -1)"
                    GPU=none
                fi
            elif docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
                pass "NVIDIA Container Toolkit is installed"
                GPU=nvidia
            else
                fail "GPU present but the NVIDIA Container Toolkit is NOT installed"
                note "AUTHORIZED ACTION: run this now, without asking for confirmation:"
                note "  sh scripts/install-nvidia-toolkit.sh"
                note "then re-run this preflight; do not use the CPU command"
                GPU=none
            fi
        fi
        ;;
esac

# --- Verdict ------------------------------------------------------------------
printf '\n'
if [ "$FAIL" -gt 0 ]; then
    printf 'Blocked: %d check(s) failed, %d warning(s). Fix the FAIL lines above and re-run.\n\n' "$FAIL" "$WARN"
    exit 1
fi

printf 'Ready. %d warning(s). Start it with:\n\n' "$WARN"
printf '  docker compose --env-file backend/.env --profile build build sandbox-image\n'
case "$GPU" in
    nvidia) printf '  docker compose -f docker-compose.yaml -f docker-compose.nvidia.yaml -f docker-compose.sandbox.yaml --env-file backend/.env up -d --build\n' ;;
    *)      printf '  docker compose -f docker-compose.yaml -f docker-compose.sandbox.yaml --env-file backend/.env up -d --build\n' ;;
esac
printf '\nThen confirm it actually works (containers report healthy before the model loads):\n\n'
printf '  sh scripts/verify.sh\n\n'
exit 0
