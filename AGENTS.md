# AGENTS.md — unattended BlockingBear deployment

This is the deterministic contract for an installation agent. Read
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) before acting and use
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for failures. Public documentation and
operator-facing output must remain in English.

## Non-negotiable rules

- **Containers only.** Docker Compose is the sole supported deployment. Never create a
  host virtualenv, install the frontend on the host, run Uvicorn/Vite as the deployed
  application, or offer bare metal as a fallback.
- **Do not edit files to enable a supported variant.** CPU, NVIDIA and sandbox choices
  are Compose files and command flags. If a procedure requires uncommenting YAML, stop.
- **Preflight decides the hardware path.** Do not choose CPU/NVIDIA yourself. When an
  NVIDIA GPU is detected, the NVIDIA path is mandatory; never silently fall back to CPU.
- **The sandbox is required and pre-authorized.** Always build it and include
  `docker-compose.sandbox.yaml`. Do not ask again and do not offer a no-sandbox option.
  Disclose that the backend controls the Docker Engine socket; on native Linux that is
  effectively root-equivalent.
- **Generate local secrets autonomously.** Run `sh scripts/init-env.sh` without asking the
  user for PostgreSQL or Camofox secret values. Preserve every existing entry: the same
  file intentionally contains backend source-development settings that Compose ignores.
  Never print secrets. Report whether `backend/.env` was created or preserved. If an IDE
  hides it because it is Git-ignored, verify it with `test -f backend/.env`; never display
  its contents.
- **Never create an OpenRouter credential.** It belongs to the user's external account
  and is configured after the application works.
- **A healthy container is not proof.** Completion requires `sh scripts/verify.sh` to
  exit zero after real PII inference and sandbox execution.

## Keep the user informed

Before model download, package/toolkit installation, image build, container startup and
verification, state what is starting and what outcome is expected. While a long command
continues, send a short factual update at least every 60 seconds. Report retries,
privilege blockers and required human actions immediately. Do not invent percentages.
Finish with a success/failure summary and the application URL or next command.

## Required sequence

Run every command from the repository root:

```bash
sh scripts/init-env.sh
sh scripts/preflight.sh
# apply each correction and re-run preflight until it exits zero
# run the complete build/start commands printed by preflight
sh scripts/verify.sh
```

If the PII model is missing, download it immediately without asking for confirmation:

```bash
mkdir -p backend/models
docker run --rm --mount "type=bind,src=$PWD/backend/models,dst=/models" python:3.11-slim sh -c "pip install -q huggingface_hub && hf download rizzoaiacademy/rizzo-pii-0.3B --revision v1.5.0 --local-dir /models/rizzo-pii-0.3B-v1.5.0"
```

Create/fix `./data` using the exact correction printed by preflight. It uses mode 0700
when the host/WSL user is uid 1000, or asks for uid 1000 ownership otherwise. Mode 0777
is a no-privilege, world-writable fallback only.

Always pass `--env-file backend/.env` to Compose. Keep `--build` when changing between
CPU and NVIDIA. Preflight persists the detected Docker socket path and the GID observed
inside a probe container in `backend/.env`, after proving uid 1000 can ping the Engine
with that supplemental group. Later Compose commands must not depend on exports from an
earlier shell. Build the sandbox image before the stack:

```bash
docker compose --env-file backend/.env --profile build build sandbox-image
```

Then run exactly the start command printed by preflight, including
`-f docker-compose.sandbox.yaml` and, when selected,
`-f docker-compose.nvidia.yaml`.

## Platform decisions

### Windows/WSL2

- Windows support means Docker Desktop with its WSL2 Linux engine. Run repository scripts
  inside the integrated distro, with the clone below its Linux home, never `/mnt/c`.
- Enter the WSL shell first and run the printed commands there. Do not wrap repository
  commands containing `$PWD` in a double-quoted PowerShell invocation such as
  `wsl.exe ... bash -lc "..."`: PowerShell expands `$PWD` to a Windows path before Bash
  sees it. Do not invent quoting workarounds; use the distro shell as documented.
- If Docker Desktop is missing, stop and give the user
  <https://docs.docker.com/desktop/setup/install/windows-install/>.
- The user starts Docker Desktop manually and waits for **Engine running**. Do not create
  autostart tasks, WSL keepalives or other persistence automation.
- WSL Integration is a human GUI step. Tell the user to enable the exact distro under
  Docker Desktop **Settings → Resources → WSL Integration** and choose **Apply &
  restart**. Never edit `%APPDATA%\Docker\settings-store.json`, the registry or other
  Docker Desktop internal state to toggle it.
- `docker info --format '{{.OperatingSystem}}|{{.OSType}}'` must print
  `Docker Desktop|linux`. Ubuntu means a conflicting native daemon. Tell the user to back
  up that daemon's volumes before manually removing its Engine/CLI and enabling WSL
  Integration. Do not perform package removal without that data decision.
- Never install NVIDIA Container Toolkit inside WSL. Docker Desktop uses the Windows
  driver; preflight proves GPU passthrough with a real CUDA container.

### Native Linux

- If `nvidia-smi -L` sees a GPU and the runtime is missing, run the pre-authorized
  `sh scripts/install-nvidia-toolkit.sh`. It may restart Docker. If privileges are
  unavailable, report the blocker instead of choosing CPU.
- Docker socket permission failures must be resolved, not bypassed. A stale group session
  can use `newgrp docker`; adding a user to the group requires administrator authority.

### macOS

- Use Docker Desktop and the CPU path. Metal has no supported container passthrough.
- If Docker Desktop is missing, stop and give the user
  <https://docs.docker.com/desktop/setup/install/mac-install/>. Tell them to choose the
  installer matching Apple silicon or Intel, open Docker.app and wait for **Engine
  running** before resuming.
- Do not offer a native Docker Engine or an unverified alternative VM/runtime as a
  supported fallback.
- Preflight detects `/var/run/docker.sock` or `$HOME/.docker/run/docker.sock`, then probes
  the container-visible GID and real Engine access instead of trusting macOS host
  ownership.
- Enhanced Container Isolation requires the Docker socket image list to allow
  `python:3.11-slim-bookworm` with `allowDerivedImages: true`. This covers the preflight
  probe and the locally built backend, which uses the identical final base. Do not use a
  wildcard exception and do not silently omit the sandbox.

AMD GPUs are unsupported and use CPU. AMD processors themselves are supported.

## Human steps after verification

After `verify.sh` passes, report that the local deployment works and that the
installation is completed in the browser:

1. Open `http://localhost` (or the machine address). The sign-in page shows the setup
   wizard until it has been completed.
2. The wizard asks, in order: interface language; the OpenRouter **Management API Key**;
   the password of the `admin` account; the categories to anonymize; optionally the first
   terms to always anonymize; the chat rules (required/optional anonymization, models
   without Zero Data Retention); optionally the default chat model. It does not sign in: afterwards the administrator logs in
   with the chosen password.
3. Tell the user to create the Management API Key in their own browser at
   <https://openrouter.ai/settings/management-keys> and paste it only into the wizard.
   It is not a normal inference key: it creates a separate inference key for every
   application user (the administrator included) and reads cost data. The wizard rejects
   inference keys.
4. Tell the user to complete the wizard immediately: until it is completed, anyone who
   reaches the sign-in page can complete it and become the administrator.

This is a **mandatory human step**. Do not ask the user for the key, do not invent the
administrator password, and do not try to complete the wizard yourself. The key is stored
in `data/openrouter_management.key`; generated inference keys are stored in PostgreSQL.
Read [docs/OPENROUTER.md](docs/OPENROUTER.md) before guiding credential rotation.

Web search is enabled by default and can be disabled in admin settings.

## Source-of-truth references

- Full platform procedure: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- OpenRouter modes and credential handling: [docs/OPENROUTER.md](docs/OPENROUTER.md)
- Failure handling: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
- Updates and backups: [docs/OPERATIONS.md](docs/OPERATIONS.md)
- Docker socket and sandbox model: [docs/SANDBOX.md](docs/SANDBOX.md)
- Runtime truth: `scripts/preflight.sh` and `scripts/verify.sh`
