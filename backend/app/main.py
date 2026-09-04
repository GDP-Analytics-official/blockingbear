"""BlockingBear — API di pseudonimizzazione eseguita dal container backend.

Il deployment supportato parte esclusivamente da Docker Compose; Dockerfile e
compose definiscono comando, dipendenze, frontend e servizi collegati.
"""

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import jobs
from .config import CORS_ORIGINS, CPU_THREADS, MODEL_DIR, N_WORKERS
from .db import init_db
from .engine import convert
from .openrouter import provisioning, sandbox
from .routes import (auth_routes, chat_routes, jobs_routes, projects,
                     settings_routes, setup_routes, usage_routes, users)

app = FastAPI(title="BlockingBear", version="0.1.0")

# in sviluppo il frontend gira su Vite (5173) e chiama l'API cross-origin;
# in produzione (frontend servito da qui) BLOCKINGBEAR_CORS_ORIGINS vuota lo spegne
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

@app.exception_handler(StarletteHTTPException)
async def _http_error(request, exc):
    """Aggiunge `code` e `params` al corpo d'errore quando l'eccezione è un
    ApiError (app/errors.py). Per tutto il resto — 404 di rotte inesistenti,
    errori sollevati da Starlette — il corpo porta il solo `detail`, come
    quello di FastAPI."""
    body = {"detail": exc.detail}
    code = getattr(exc, "code", None)
    if code:
        body["code"] = code
        if getattr(exc, "params", None):
            body["params"] = exc.params
    return JSONResponse(body, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


app.include_router(setup_routes.router)
app.include_router(auth_routes.router)
app.include_router(users.router)
app.include_router(jobs_routes.router)
app.include_router(settings_routes.router)
app.include_router(chat_routes.router)
app.include_router(projects.router)
app.include_router(usage_routes.router)


@app.get("/api/health")
def health():
    """Stato del server e delle sue parti opzionali.

    Dice se ogni worker PII ha completato una vera inferenza, se LibreOffice è
    raggiungibile (serve per le anteprime OOXML, non per i PDF), quanti worker
    girano, la coda
    pendente e lo stato della sandbox. `sandbox.smoke_tested` diventa vero solo
    dopo una cella Python reale e un artifact recuperato dal tmpfs del kernel.
    Non richiede autenticazione: è il
    punto da interrogare per sapere se l'installazione è completa."""
    return {
        "status": "ok",
        "model_dir": str(MODEL_DIR),
        "model_loaded": jobs.loaded(),
        # richiesto solo per l'anteprima dei .docx; i PDF funzionano comunque
        "libreoffice": convert.available(),
        "workers": N_WORKERS,
        "cpu_threads": CPU_THREADS,
        "queue_pending": jobs.pending_count(),
        # code interpreter della chat LLM (openrouter/sandbox.py)
        "sandbox": sandbox.status(),
    }


@app.get("/api/tags")
def tags():
    """Tag rilevabili, letti dal config del modello: si aggiornano da soli quando
    si sostituisce il modello in backend/models/."""
    return jobs.ENGINES[0].tags()


@app.on_event("startup")
async def startup():
    SessionLocal = init_db()
    with SessionLocal() as s:
        # i job rimasti a metà da prima del riavvio avevano il payload in
        # RAM: non sono più eseguibili -> failed con messaggio chiaro
        jobs.sweep_stale(s)
    # chiavi OpenRouter per-utente: chi ne è rimasto senza (OpenRouter giù al
    # momento della creazione) la riceve qui, in background: l'avvio non
    # aspetta la rete. Nessun utente esiste finché il wizard di installazione
    # (setup_routes) non crea l'admin.
    asyncio.create_task(provisioning.bootstrap(SessionLocal))
    # pool di worker + warm-up dei modelli in background: l'API risponde
    # subito, il primo documento non paga i ~10s di caricamento
    jobs.start(asyncio.get_running_loop())
    # sandbox del code interpreter: rilevazione runtime + warm pool in
    # background; senza Docker/Podman resta spenta e la chat degrada
    sandbox.start()


# frontend compilato (se esiste): installazione locale a processo singolo.
# Niente mount("/") diretto: le route del client (/projects/abc, /settings)
# non corrispondono a file su disco e al refresh devono ricevere index.html
# (fallback SPA). Le /api/* registrate sopra hanno la precedenza.
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist.is_dir():
    if (_dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        """Serve il frontend compilato, con fallback SPA su index.html.

        Registrata solo se `frontend/dist` esiste. Un percorso che
        corrisponde a un file lo restituisce; qualunque altro riceve
        index.html, così le route del client sopravvivono al refresh. Il
        controllo `is_relative_to` chiude il path traversal."""
        file = (_dist / path).resolve()
        if file.is_file() and file.is_relative_to(_dist):
            return FileResponse(file)
        return FileResponse(_dist / "index.html")
