# Operations, updates and backups

All commands run from the repository root and use `--env-file backend/.env`.

## Status and logs

```bash
docker compose --env-file backend/.env ps -a
docker compose --env-file backend/.env logs --tail=100 backend
sh scripts/verify.sh
```

The expected steady state is four long-lived services running (`postgres`, `backend`,
`nginx`, `camofox`) and `blockingbear-frontend-build` exited successfully with code 0.

## Stop and start

The exact start command depends on CPU/NVIDIA and always includes the sandbox override.
Run `sh scripts/preflight.sh` whenever the command is uncertain.

```bash
docker compose --env-file backend/.env down
# data remains in ./data and the PostgreSQL named volume
```

On Windows, Docker Desktop is started manually. Services with
`restart: unless-stopped` return when its Engine starts.

## Update

Before updating, make a backup. Then:

```bash
git pull --ff-only
sh scripts/init-env.sh
sh scripts/preflight.sh
# run the build/start commands printed by preflight; keep --build
sh scripts/verify.sh
```

Do not edit Compose files to preserve a local variant. Supported choices are expressed by
the committed override files and the command printed by preflight.

## Data locations

- `./data`: original and protected document bytes, generated files, the JWT secret and
  the OpenRouter Management API Key entered in the setup wizard.
- `pgdata`: users, chats, decoding registries, settings and the per-user
  OpenRouter inference keys.
- `backend/.env`: local deployment secrets and optional startup configuration.
- `backend/models/`: downloaded PII checkpoint, reproducible and excluded from Git.

## Backup

A complete backup contains both PostgreSQL and `./data`. A database dump alone does not
contain document bytes; copying `./data` alone does not contain chats or registries.

```bash
docker exec blockingbear-postgres pg_dump -U blockingbear blockingbear > blockingbear.sql
```

Copy `blockingbear.sql`, `data/` and `backend/.env` into appropriately protected backup
storage. `backend/.env` and original documents contain sensitive information. The model
checkpoint and built images can be recreated and do not need backup.

For a consistent high-activity backup, briefly stop the backend before copying `data/`:

```bash
docker stop blockingbear-backend
docker exec blockingbear-postgres pg_dump -U blockingbear blockingbear > blockingbear.sql
# copy ./data and backend/.env using the organization's backup system
docker start blockingbear-backend
sh scripts/verify.sh
```

Test restore procedures on a separate Docker project/host. Never test by overwriting the
live database or live `data/` directory.

## First login and users

The `admin` account and its password are created in the setup wizard shown on the
sign-in page until it has been completed. Administrators create subsequent
users; each user receives a personal OpenRouter inference key. Deleting a user permanently removes
their projects, files, chats, attachments, registries, jobs, settings and OpenRouter key.
