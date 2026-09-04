# Code-interpreter sandbox

The code interpreter is a required component of the supported deployment. The backend
mounts the Docker Engine socket and creates short-lived sibling containers from
`blockingbear-sandbox:1`.

## Security boundary

A Docker socket is a powerful capability:

- on native Linux, control of the socket is effectively root-equivalent on the host;
- on Windows and macOS, Docker Desktop's Linux VM is an additional host boundary, but the
  backend can still control that Engine and access paths shared with Docker Desktop;
- anyone who can modify or control the backend container can use the same capability.

This exposure must be disclosed to the operator. The repository owner has made the
sandbox mandatory for this deployment policy, so an automated installer enables it and
does not silently degrade to a no-sandbox installation.

## Isolation model

Each conversation receives a separate container with a private tmpfs workspace. The
backend streams only that conversation's inputs into it using `docker exec`, then streams
declared output artifacts back. Sandbox containers do not mount the application's data
directory and have no network access.

The host daemon never needs to reinterpret a conversation workspace path. This keeps the
same transfer model on Linux and Docker Desktop and prevents one conversation from
listing another conversation's staging directory.

## Build and start

Use the socket and container-visible GID detected by `scripts/preflight.sh`. Preflight
mounts the socket in a probe container and requires a real Engine ping as uid 1000 with
that supplemental group before it saves the values:

```bash
docker compose --env-file backend/.env --profile build build sandbox-image
docker compose -f docker-compose.yaml -f docker-compose.sandbox.yaml --env-file backend/.env up -d --build
```

The NVIDIA override combines with the sandbox override:

```bash
docker compose -f docker-compose.yaml -f docker-compose.nvidia.yaml -f docker-compose.sandbox.yaml --env-file backend/.env up -d --build
```

On macOS, Docker Desktop may use `$HOME/.docker/run/docker.sock`. Its host-side ownership
is not authoritative because the VM mount layer can present another GID to containers.
Enhanced Container Isolation blocks the socket mount by default. Allow
`python:3.11-slim-bookworm` with `allowDerivedImages: true`: preflight uses that registry
image for the permission probe and the local backend image derives from the identical
Dockerfile base. A wildcard exception is not required and should not be used. See Docker's
[ECI socket-exception configuration](https://docs.docker.com/enterprise/security/hardened-desktop/enhanced-container-isolation/config/).

## Verification

Socket reachability alone is not success. `scripts/verify.sh` waits until a real Python
cell executes in a sandbox and an artifact is retrieved from its tmpfs workspace.
