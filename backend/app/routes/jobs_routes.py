"""Stato dei job di elaborazione: snapshot (GET) e stream di eventi (SSE).

SSE = una GET la cui risposta non finisce: Content-Type text/event-stream,
il server scrive `data: {...}\n\n` a ogni cambio di stato e un commento
`: ping` ogni 15s per tenere viva la connessione attraverso i proxy.
EventSource (browser) non può impostare header Authorization, quindi il
token JWT arriva come query param (?token=...), validazione identica.
"""

import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import jobs
from ..auth import current_user, user_from_token
from ..db import Job, User, get_session
from ..errors import ApiError

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _get_job(job_id, user, session):
    job = session.get(Job, job_id)
    if job is None or (user.role != "admin" and job.owner_id != user.id):
        raise ApiError(404, "job_not_found", "Job non trovato.")
    return job


@router.get("")
def list_jobs(user: User = Depends(current_user),
              session: Session = Depends(get_session)):
    """I propri job recenti (admin: tutti): vista coda + riaggancio del
    frontend ai job ancora attivi dopo un reload della pagina."""
    q = session.query(Job)
    if user.role != "admin":
        q = q.filter_by(owner_id=user.id)
    return [jobs.describe(j)
            for j in q.order_by(Job.created_at.desc()).limit(50)]


@router.get("/{job_id}")
def get_job(job_id: str, user: User = Depends(current_user),
            session: Session = Depends(get_session)):
    """Stato di un singolo job: fase, avanzamento ed eventuale errore.

    Ripiego per il polling quando lo stream SSE di /{job_id}/events non è
    utilizzabile. Job di un altro utente: 404, come se non esistesse."""
    job = _get_job(job_id, user, session)
    return jobs.describe(job)


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str, user: User = Depends(current_user),
               session: Session = Depends(get_session)):
    """Annulla un job: se in coda l'effetto è immediato (status `canceled`
    nella risposta); se in lavorazione l'annullamento è cooperativo — la
    risposta è ancora `processing` e lo stato finale arriva via SSE al primo
    checkpoint del worker. Su un job già concluso non fa nulla."""
    job = _get_job(job_id, user, session)
    return jobs.cancel(job, session)


@router.get("/{job_id}/events")
async def job_events(job_id: str, token: str = "",
                     session: Session = Depends(get_session)):
    """Stream SSE dello stato del job, chiuso a done/failed/canceled. Il client:
        new EventSource(`/api/jobs/${id}/events?token=${jwt}`)
    e ricordarsi es.close() sullo stato finale, o la riconnessione automatica
    di EventSource riaprirebbe lo stream all'infinito."""
    user = user_from_token(token, session)
    job = _get_job(job_id, user, session)

    async def stream():
        # PRIMA subscribe, POI fotografia dello stato: un cambio avvenuto nel
        # mezzo finisce comunque nella coda e non si perde nulla (al peggio
        # arriva un duplicato, il frontend sovrascrive e basta)
        q = jobs.subscribe(job_id)
        try:
            session.expire(job)          # rilegge dal DB, post-subscribe
            state = jobs.describe(job)
            yield f"data: {json.dumps(state)}\n\n"
            while state["status"] not in ("done", "failed", "canceled"):
                try:
                    state = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(state)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"   # keep-alive: il browser lo ignora
        finally:
            jobs.unsubscribe(job_id, q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      # disattiva il buffering dei reverse proxy
                                      "X-Accel-Buffering": "no"})
