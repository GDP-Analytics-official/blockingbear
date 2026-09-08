# Troubleshooting

Always start in the repository root:

```bash
sh scripts/preflight.sh
sh scripts/verify.sh
```

Preflight checks whether the host can start the supported Compose deployment. Verify
checks whether the resulting application actually works.

## Symptom, cause and correction

| Symptom | Likely cause | Correction |
| --- | --- | --- |
| `permission denied ... /var/run/docker.sock` | Current session cannot use Docker | If the user is already listed by `getent group docker`, start a new session or use `newgrp docker`; otherwise an administrator must add the user to the group |
| macOS preflight says `docker` is not installed | Docker Desktop is missing | Install [Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/) for the machine architecture, open Docker.app and wait for **Engine running** |
| Native Linux preflight says `docker` is not installed | Docker Engine is missing | Follow the [Docker Engine instructions](https://docs.docker.com/engine/install/) for that distribution, then start its service |
| Docker is installed but `docker info` fails | Daemon stopped or inaccessible | Start Docker Engine, or open Docker Desktop and wait for **Engine running** |
| Windows preflight reports Ubuntu instead of Docker Desktop | A second Docker daemon is running inside WSL | Back up its volumes, remove that Engine/CLI manually, and enable the distro in Docker Desktop WSL Integration |
| Model download appears under `/mnt/c` or another unexpected path | PowerShell expanded `$PWD` before `wsl.exe` started Bash | Open the WSL distro shell, `cd` to the Linux clone and run the printed command there; do not nest it in a double-quoted PowerShell `bash -lc` string |
| The setup wizard fails when saving the Management API Key, or HTTP 500 on first login | `./data` is not writable by uid 1000: the key is stored in `data/openrouter_management.key` | Run the data-directory correction printed by preflight, then paste the key into the wizard again |
| Setup wizard step 4 (categories to anonymize) shows no categories and cannot continue, or Settings shows a `PII model not installed` error | Checkpoint missing from the mounted model folder: `GET /api/tags` answers 503 `pii_model_missing` and `GET /api/health` reports `model_loaded: false` | Run the model download from step 3 of the deployment guide into `backend/models/rizzo-pii-0.3B-v1.5.0`, check that `config.json` is there, then press **Retry** in the wizard |
| `model_loaded` stays false | Checkpoint missing, incomplete, mismounted, or first inference failed | Check `backend/models/rizzo-pii-0.3B-v1.5.0/config.json` and backend logs; re-run the model download |
| `Permission denied` inside RapidOCR model files | OCR weights were downloaded at runtime into a read-only image location | Rebuild the current backend image; the Dockerfile must preload the PP-OCRv5 weights |
| `could not select device driver` | NVIDIA passthrough is unavailable | Native Linux: install NVIDIA Container Toolkit; Windows: update driver, WSL and Docker Desktop |
| GPU attached but inference uses CPU | CPU image tag was reused | Re-run the NVIDIA command with `--build` |
| Triton asks for `cc`, `gcc` or `clang` | NVIDIA image lacks its compile toolchain | Rebuild with `docker-compose.nvidia.yaml`; do not patch the running container |
| `port is already allocated` | Another service owns port 80 | Stop or reconfigure that external service; the repository publishes port 80 |
| Exit 137 / `OOMKilled` | Docker VM memory too low | Allocate slightly more than 8 GiB so VM overhead still leaves at least 8 GiB visible to Docker |
| Sandbox unavailable | Socket path/GID, image build or ECI policy is wrong | Re-run preflight: it probes the container-visible socket GID and uid 1000 Engine access. Then run its complete sandbox build/start commands so the backend is recreated. With Docker Desktop ECI, allow `python:3.11-slim-bookworm` with `allowDerivedImages: true`, not a wildcard |
| Application unavailable after Windows login | Docker Desktop has not been started | Open Docker Desktop, wait for **Engine running**, then run `verify.sh` in WSL2 |

## Expected states that are not failures

- `blockingbear-frontend-build` is **Exited (0)**. It is a one-shot build container; nginx
  waits for that successful exit.
- `model_loaded: false` for the first seconds of each worker is expected. It is a failure
  only when it remains false after the verifier timeout.
- Hugging Face may warn about unauthenticated requests or an invalid upstream
  `model-index` while downloading the public checkpoint. The download is usable when
  preflight finds `config.json`, the weights and approximately 1.2 GB of model data.
- Camofox may install noVNC packages on its first start and needs egress for that step.

## Useful diagnostics

```bash
docker compose --env-file backend/.env ps -a
docker compose --env-file backend/.env logs --tail=100 backend
docker compose --env-file backend/.env logs --tail=100 nginx
curl -fsS http://127.0.0.1:8000/api/health
```

Do not consider an installation complete until `sh scripts/verify.sh` exits zero.
