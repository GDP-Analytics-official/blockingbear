# Container deployment

BlockingBear supports one deployment model: Docker Compose. Do not install the backend,
frontend or application dependencies directly on the host. Linux uses Docker Engine;
Windows and macOS use Docker Desktop.

The scripts in this repository are the executable contract:

```bash
sh scripts/init-env.sh
sh scripts/preflight.sh
# run the build/start commands printed by preflight
sh scripts/verify.sh
```

`preflight.sh` detects the operating system, Docker socket and NVIDIA availability. Do
not select CPU or GPU manually and do not edit a Compose file to enable a feature.

## Requirements

| Requirement | Verification |
| --- | --- |
| Docker daemon reachable by the current user | `docker info` exits 0 |
| Docker Compose 2.23.1 or newer | `docker compose version --short` |
| About 8 GB RAM available to Docker | Docker/Docker Desktop settings |
| About 10 GB free disk for a CPU build | `preflight.sh` |
| More free disk for NVIDIA builds | `preflight.sh` |

The repository must be the current directory when running every command.

## Complete installation

### 1. Prepare local configuration

Run this without inventing or asking for local secret values:

```bash
sh scripts/init-env.sh
```

It creates `backend/.env` from the example when missing, generates the PostgreSQL
password and Camofox access key locally, sets mode 0600 and preserves every existing
entry. It never prints the secrets. The same file also documents backend source-
development settings; Compose ignores those settings because the service passes only
its explicitly declared environment variables.

`backend/.env` is intentionally ignored by Git. VS Code can hide it when **Explorer:
Exclude Git Ignore** is enabled. Confirm its existence without printing its contents:

```bash
test -f backend/.env && echo "backend/.env exists"
```

### 2. Run preflight

```bash
sh scripts/preflight.sh
```

Preflight exits non-zero and prints an exact correction when a prerequisite is missing.
Apply the correction and run it again. A successful run prints the complete commands for
this host, including the required sandbox and the NVIDIA override when applicable.

When the Linux or WSL user has uid 1000, prepare the data directory with:

```bash
mkdir -p data && chmod 0700 data
```

If the user has a different uid, create it for the uid used by the backend container:

```bash
sudo install -d -o 1000 -g 1000 -m 0700 data
```

If administrator access is unavailable, `mkdir -p data && chmod 0777 data` works as a
world-writable fallback. Preflight labels that tradeoff explicitly.

Preflight verifies access with a real bind-mount write as uid 1000 rather than trusting
host mode bits.

### 3. Download the PII checkpoint

The checkpoint is about 1.2 GB and is intentionally excluded from Git. Download it with
a disposable container so Python is not required on the host:

```bash
mkdir -p backend/models
docker run --rm --mount "type=bind,src=$PWD/backend/models,dst=/models" python:3.11-slim sh -c "pip install -q huggingface_hub && hf download rizzoaiacademy/rizzo-pii-0.3B --revision v1.5.0 --local-dir /models/rizzo-pii-0.3B-v1.5.0"
```

Re-run `preflight.sh`; it rejects a missing or incomplete checkpoint. The supported
mount source is exactly `backend/models/rizzo-pii-0.3B-v1.5.0`.

The command above follows the `v1.5.0` revision name. Operators who require a fully
reproducible supply chain should resolve that revision to a reviewed immutable commit
and use the commit hash with the same local directory name.

### 4. Build and start

Run the commands printed by preflight. The CPU form is equivalent to:

```bash
docker compose --env-file backend/.env --profile build build sandbox-image
docker compose -f docker-compose.yaml -f docker-compose.sandbox.yaml --env-file backend/.env up -d --build
```

Preflight detects the Docker socket path, mounts it into a small probe container and reads
the GID as it appears there. This matters on desktop VM runtimes, where host and container
ownership can differ. It then proves that uid 1000 with that supplemental group can ping
the Docker Engine and saves both non-secret values in `backend/.env`. Consequently, every
later Compose command works from a new shell when it includes `--env-file backend/.env`;
no session-only exports are required.

On a supported NVIDIA host, preflight prints this start command instead:

```bash
docker compose -f docker-compose.yaml -f docker-compose.nvidia.yaml -f docker-compose.sandbox.yaml --env-file backend/.env up -d --build
```

`--build` is required whenever changing between CPU and NVIDIA because both variants use
the same backend image tag and the PyTorch index is a build argument.

### 5. Verify the product, not just the containers

```bash
sh scripts/verify.sh
```

The verifier waits for a real PII inference on every worker, LibreOffice, a sandbox
Python execution with artifact retrieval, and the web application. A healthy container
is not enough: the API becomes healthy before the model finishes loading.

Open `http://<machine-address>` and complete the setup wizard shown on the sign-in page
(see [OpenRouter after installation](#openrouter-after-installation)).

## Windows and WSL2

Install [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/)
and use its WSL2 Linux engine. In Docker Desktop:

1. Enable **Use the WSL 2 based engine** under Settings → General.
2. Enable the exact distro under Settings → Resources → WSL Integration.
3. Select **Apply & restart** and wait for **Engine running**.

These are deliberate user-controlled GUI steps. Do not edit
`%APPDATA%\Docker\settings-store.json`, the Windows registry or other Docker Desktop
internal state to enable integration programmatically.

Keep the clone in the distro Linux filesystem, for example `~/blockingbear`, never under
`/mnt/c`. Enter the WSL shell first and run all repository scripts there. Do not embed
commands containing `$PWD` inside a double-quoted PowerShell
`wsl.exe ... bash -lc "..."` command: PowerShell expands `$PWD` to a Windows path before
Bash starts, which can redirect the model download outside the clone. The authoritative
check is:

```bash
docker info --format '{{.OperatingSystem}}|{{.OSType}}'
# expected: Docker Desktop|linux
```

If it prints Ubuntu, a second Docker Engine is installed inside the distro. Back up any
volumes belonging to that daemon, remove its Engine and CLI packages manually, then
enable Docker Desktop integration. Do not install `docker-ce` or NVIDIA Container
Toolkit inside the integrated distro.

Docker Desktop is started manually. After a Windows restart, open it and wait for
**Engine running**; the Compose services return through `restart: unless-stopped`.

For NVIDIA, Docker Desktop uses the Windows driver. Preflight proves passthrough with a
real `docker run --gpus all ... nvidia-smi`. If it fails, update the Windows NVIDIA
driver, run `wsl --update`, and update Docker Desktop. CPU fallback is not selected when
an NVIDIA GPU is present.

## Native Linux and NVIDIA

When `nvidia-smi -L` sees a GPU, NVIDIA Container Toolkit is required. The repository
provides an idempotent installer for apt, dnf/yum and zypper hosts:

```bash
sh scripts/install-nvidia-toolkit.sh
sh scripts/preflight.sh
```

It configures Docker with `nvidia-ctk`, restarts the daemon and verifies a CUDA
container. If root privileges are unavailable, report that blocker; do not silently use
CPU. Budget about 1.2–1.5 GB VRAM per document worker and substantially more build disk
than the CPU image.

## macOS

Install [Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/)
and choose the download matching Apple silicon or Intel. Open Docker.app, finish its
interactive setup and wait until it reports **Engine running**. Give its VM at least 8 GB
RAM; VM overhead can make an exact 8 GiB allocation appear slightly smaller to Docker, so
allocate a little more than the target if preflight warns.

Docker Desktop is the only macOS runtime supported by this repository. A Docker CLI by
itself provides no local Linux daemon, and alternative VM-backed runtimes have not been
validated with the required sandbox socket integration.

Intel and Apple Silicon use the CPU Compose path; the images support `linux/amd64` and
`linux/arm64`. Metal cannot be passed through Docker Desktop's Linux VM, so macOS has no
supported GPU path.

Docker Desktop may expose `/var/run/docker.sock` or `$HOME/.docker/run/docker.sock`;
preflight detects both and validates the ownership and Engine access from inside a uid
1000 container before accepting the installation. Enhanced Container Isolation blocks
Docker socket mounts by default. Because the backend is built locally, configure the ECI
socket image list with `python:3.11-slim-bookworm` and `allowDerivedImages: true`; this
allows both the probe and the backend derived from the same base. Do not use a wildcard
image exception. See Docker's [socket-exception configuration](https://docs.docker.com/enterprise/security/hardened-desktop/enhanced-container-isolation/config/).

## OpenRouter after installation

Passing `verify.sh` proves the local deployment, PII engine and sandbox. The installation
is completed in the browser: until the setup wizard has been completed, the sign-in page
at `http://localhost` (or the machine address) shows it.

1. Choose the interface language.
2. In the user's own browser, open <https://openrouter.ai/settings/management-keys>,
   choose **+ New key**, give it a recognizable name such as `blockingbear`
   and copy the value. Paste it into the wizard. BlockingBear verifies with OpenRouter
   that it is a Management API Key and rejects normal inference keys.
3. Choose the password of the `admin` account (at least 8 characters). BlockingBear
   creates the account and, through the Management API Key, its personal inference key.
   If OpenRouter refuses, the account is not created and the wizard shows the error.
4. Choose the categories of personal data to anonymize for every user (a reliable
   default set is preselected).
5. Optionally, enter the first terms to always anonymize for every user.
6. Choose whether chat anonymization is required or optional and whether models without
   Zero Data Retention providers may be used.
7. Optionally, choose the default chat model for every user (a fixed model with its
   initial options, or a criterion such as the latest model by a given company).
8. Optionally, choose which models non-administrator users can see and pick: the whole
   catalogue or a selected list. The default model is always included.

Everything chosen in steps 4–8 can be changed later from **Settings**. The wizard does
not sign in: once completed, the sign-in page appears and the administrator logs in with
the chosen password.

Complete the wizard immediately: until it is completed, anyone who reaches the sign-in
page can complete it and become the administrator.

An installation agent must not ask the user for the key and must not complete the wizard
on the user's behalf. Never place the key in `backend/.env`, a terminal, an issue, the
repository or an agent conversation. BlockingBear stores it in
`data/openrouter_management.key`, returns only a masked value to the browser and uses it
to create a normal inference key named `blockingbear-<username>` for every user. Storage,
verification and rotation are described in [OpenRouter configuration](OPENROUTER.md).

Web search is enabled by default and can be disabled from the admin settings.

## Next references

- [OpenRouter configuration](OPENROUTER.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Operations, updates and backups](OPERATIONS.md)
- [Sandbox architecture and security](SANDBOX.md)
