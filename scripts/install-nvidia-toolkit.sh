#!/bin/sh
# Install and configure NVIDIA Container Toolkit ONLY for Docker Engine on
# native Linux. Idempotent: an agent can rerun it without deciding whether the
# repository or package already exists.
#
# Command source: official NVIDIA Container Toolkit documentation:
# https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html
#
# On Windows/WSL2, Docker Desktop exposes the GPU through the Windows driver; do
# not install this toolkit inside the distro.

set -eu

fail() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }
note() { printf '  %s\n' "$1"; }

[ "$(uname -s 2>/dev/null || echo unknown)" = Linux ] || \
    fail "this installer supports native Linux only"
command -v docker >/dev/null 2>&1 || fail "Docker Engine is not installed"
docker info >/dev/null 2>&1 || fail "Docker Engine is not reachable by this user"

docker_os=$(docker info --format '{{.OperatingSystem}}' 2>/dev/null || echo "")
case "$docker_os" in
    *"Docker Desktop"*)
        fail "do not install NVIDIA Container Toolkit for Docker Desktop; run preflight.sh to verify direct GPU passthrough"
        ;;
esac
if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
    fail "the supported Windows path uses Docker Desktop WSL integration, not a native Docker Engine inside Ubuntu"
fi

SMI=$(command -v nvidia-smi 2>/dev/null || true)
if [ -z "$SMI" ] && [ -x /usr/lib/wsl/lib/nvidia-smi ]; then
    SMI=/usr/lib/wsl/lib/nvidia-smi
fi
[ -n "$SMI" ] || fail "nvidia-smi is missing: install/repair the NVIDIA host driver first"

nvidia_out=$($SMI -L 2>&1 || true)
printf '%s' "$nvidia_out" | grep -q '^GPU 0' || {
    printf '%s\n' "$nvidia_out" >&2
    fail "the NVIDIA host driver does not expose a usable GPU"
}
note "Detected: $(printf '%s' "$nvidia_out" | head -1)"

if [ "$(id -u)" -eq 0 ]; then
    as_root() { "$@"; }
elif command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    as_root() { sudo -n "$@"; }
else
    fail "non-interactive root access is required (run as root or provide passwordless sudo)"
fi

TMP_DIR=$(mktemp -d)
cleanup() { [ ! -d "$TMP_DIR" ] || rm -r "$TMP_DIR"; }
trap cleanup EXIT HUP INT TERM

if command -v apt-get >/dev/null 2>&1; then
    note "Installing NVIDIA Container Toolkit from the NVIDIA apt repository"
    as_root apt-get update
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg2
    curl --fail --silent --show-error --location --retry 3 \
        https://nvidia.github.io/libnvidia-container/gpgkey \
        | gpg --dearmor --yes --output "$TMP_DIR/nvidia-container-toolkit-keyring.gpg"
    curl --fail --silent --show-error --location --retry 3 \
        https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        > "$TMP_DIR/nvidia-container-toolkit.list"
    as_root install -m 0644 "$TMP_DIR/nvidia-container-toolkit-keyring.gpg" \
        /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    as_root install -m 0644 "$TMP_DIR/nvidia-container-toolkit.list" \
        /etc/apt/sources.list.d/nvidia-container-toolkit.list
    as_root apt-get update
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y nvidia-container-toolkit
elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
    PM=dnf
    command -v dnf >/dev/null 2>&1 || PM=yum
    note "Installing NVIDIA Container Toolkit from the NVIDIA rpm repository"
    as_root "$PM" install -y ca-certificates curl
    curl --fail --silent --show-error --location --retry 3 \
        https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
        > "$TMP_DIR/nvidia-container-toolkit.repo"
    as_root install -m 0644 "$TMP_DIR/nvidia-container-toolkit.repo" \
        /etc/yum.repos.d/nvidia-container-toolkit.repo
    as_root "$PM" install -y nvidia-container-toolkit
elif command -v zypper >/dev/null 2>&1; then
    note "Installing NVIDIA Container Toolkit from the NVIDIA zypper repository"
    as_root zypper --non-interactive install -y ca-certificates curl
    if ! zypper lr -u 2>/dev/null | grep -q 'nvidia.github.io/libnvidia-container'; then
        as_root zypper --non-interactive addrepo \
            https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
            nvidia-container-toolkit
    fi
    as_root zypper --non-interactive --gpg-auto-import-keys refresh
    as_root zypper --non-interactive install -y nvidia-container-toolkit
else
    fail "unsupported Linux package manager (expected apt, dnf/yum, or zypper)"
fi

command -v nvidia-ctk >/dev/null 2>&1 || fail "nvidia-ctk was not installed"
note "Configuring the NVIDIA runtime in /etc/docker/daemon.json"
as_root nvidia-ctk runtime configure --runtime=docker

note "Restarting Docker Engine"
if command -v systemctl >/dev/null 2>&1 && as_root systemctl restart docker; then
    :
elif command -v service >/dev/null 2>&1 && as_root service docker restart; then
    :
else
    fail "toolkit installed, but Docker could not be restarted"
fi

i=0
while ! docker info >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -lt 30 ] || fail "Docker did not become reachable after restart"
    sleep 1
done

docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia || \
    fail "Docker restarted but the NVIDIA runtime is not registered"

# The project defaults to cu126 wheels. This small image verifies real GPU
# passthrough rather than only checking runtime registration metadata.
VERIFY_IMAGE=${BLOCKINGBEAR_NVIDIA_VERIFY_IMAGE:-nvidia/cuda:12.6.3-base-ubuntu22.04}
note "Verifying GPU passthrough with $VERIFY_IMAGE"
docker run --rm --gpus all "$VERIFY_IMAGE" nvidia-smi -L

printf '\nNVIDIA Container Toolkit is installed, configured and verified.\n'
printf 'Re-run: sh scripts/preflight.sh\n\n'
