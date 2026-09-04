"""Dashboard costi OpenRouter (solo admin): tutto on-demand, nessun job.

Tre fonti, tre livelli di dettaglio:
  - GET  /api/usage/overview  saldo GLOBALE dell'account (i limiti per chiave
                              non riservano crediti: senza questo numero la
                              somma dei tetti racconta una storia falsa) +
                              l'elenco chiavi con usage totale/giornaliero/
                              settimanale/mensile e limite, già agganciato
                              agli utenti dell'app;
  - POST /api/usage/query     proxy VALIDATO verso /analytics/query: serie
                              temporali e spaccati per modello/chiave per i
                              grafici (dimensione api_key_id = NOME della
                              chiave, cioè blockingbear-<username>);
  - GET  /api/usage/meta      metriche/dimensioni disponibili, dritte da
                              OpenRouter: la dashboard non hardcoda liste
                              che l'API può ampliare.

Ogni chiamata usa la management key: gli utenti standard non passano di qui
(require_admin) e la management key non esce mai dal server."""

import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import require_admin
from ..db import User, get_session
from ..errors import ApiError
from ..openrouter import client as or_client
from ..openrouter import provisioning

router = APIRouter(prefix="/api/usage", tags=["usage"])


def _require_provisioning():
    if not provisioning.configured():
        raise ApiError(409, "provisioning_key_missing",
                       "Management key OpenRouter non configurata: "
                       "impostarla dalla pagina API keys.")


def _app_keys(session, keys):
    """(righe_app, hashes): SOLO le chiavi che appartengono a un utente
    dell'app, riconosciute per hash (o per nome blockingbear-*, rete di
    sicurezza per chiavi ricreate a mano). L'account OpenRouter è condiviso
    con altri progetti: le loro chiavi NON devono uscire da nessun endpoint,
    quindi il perimetro si impone qui, non nel frontend."""
    users = session.query(User).order_by(User.id).all()
    by_hash = {u.openrouter_key_hash: u for u in users
               if u.openrouter_key_hash}
    by_name = {provisioning.key_name(u.username): u for u in users}
    rows = []
    for k in keys:
        u = by_hash.get(k.get("hash")) or by_name.get(k.get("name"))
        if u is None:
            continue                     # chiave di un altro progetto
        rows.append({**k, "user": {"id": u.id, "username": u.username,
                                   "role": u.role}})
    return users, rows


@router.get("/overview")
async def overview(_admin: User = Depends(require_admin),
                   session: Session = Depends(get_session)):
    """Saldo account + le SOLE chiavi dell'app con consumo e limiti."""
    _require_provisioning()
    try:
        credits = await provisioning.credits()
        keys = await provisioning.list_keys(include_disabled=True)
    except or_client.OpenRouterError as e:
        raise ApiError(502, "openrouter_unreachable",
                       f"OpenRouter non risponde: {e}", error=str(e))

    users, out_keys = _app_keys(session, keys)
    # utenti rimasti senza chiave (creazione fallita): la dashboard li deve
    # mostrare, non nasconderli tra le righe mancanti
    matched_ids = {k["user"]["id"] for k in out_keys}
    missing = [{"id": u.id, "username": u.username, "role": u.role}
               for u in users if u.id not in matched_ids]
    return {"credits": credits, "keys": out_keys, "users_without_key": missing}


class UsageQuery(BaseModel):
    """Sottoinsieme dichiarato di /analytics/query. La forma si valida qui
    (tipi, tetto righe, finestra); i NOMI di metriche e dimensioni li valida
    OpenRouter, che è l'unica fonte aggiornata di quali esistono."""
    metrics: list[str] = Field(min_length=1, max_length=12)
    dimensions: list[str] | None = Field(None, max_length=2)  # tetto API
    granularity: str | None = Field(None, pattern="^(day|week|month)$")
    days: int | None = Field(None, ge=1, le=366)     # alternativa comoda...
    time_range: dict | None = None                   # ...a {start,end} ISO
    filters: list[dict] | None = Field(None, max_length=8)
    order_by: dict | None = None
    limit: int | None = Field(None, ge=1, le=500)


@router.post("/query")
async def usage_query(body: UsageQuery,
                      _admin: User = Depends(require_admin),
                      session: Session = Depends(get_session)):
    """Interroga le analytics OpenRouter (solo admin, serve la management key).

    Il corpo è la query dell'utente — metriche, dimensioni, granularità,
    ordinamento — ma il perimetro lo impone il server: qualunque filtro
    `api_key_id` in arrivo viene sostituito con l'elenco delle chiavi create
    da questa installazione, così la dashboard non può leggere il consumo di
    chiavi estranee all'account. Senza `time_range` la finestra sono gli
    ultimi `days` giorni (30 di default). Nessuna chiave ancora creata:
    risposta vuota, non un errore."""
    _require_provisioning()
    # perimetro: le analytics rispondono SOLO per le chiavi dell'app. Il
    # filtro va per HASH (verificato sul vivo: per nome l'API risponde 500);
    # un filtro api_key_id del client viene sostituito, non ampliato.
    hashes = [h for (h,) in session.query(User.openrouter_key_hash)
              .filter(User.openrouter_key_hash.isnot(None))]
    if not hashes:
        return {"data": [], "metadata": {"row_count": 0, "truncated": False}}
    filters = [f for f in (body.filters or [])
               if f.get("field") != "api_key_id"]
    filters.append({"field": "api_key_id", "operator": "in", "value": hashes})
    if body.time_range:
        start, end = body.time_range.get("start"), body.time_range.get("end")
        if not (isinstance(start, str) and isinstance(end, str)):
            raise ApiError(422, "time_range_invalid",
                           "time_range richiede start e end ISO.")
        time_range = {"start": start, "end": end}
    else:
        # default: ultimi N giorni interi UTC più oggi. ATTENZIONE alla
        # cache di OpenRouter (campo cachedAt nella risposta): è per payload
        # IDENTICO, quindi l'estremo finale è "adesso al secondo" e non la
        # mezzanotte di domani — con un end fisso, tutto il giorno riceverebbe
        # la fotografia della PRIMA interrogazione — i turni nuovi comparirebbero
        # sulle finestre ancora fredde e non su quella già in cache. start
        # resta a mezzanotte: i bucket sono giornate.
        days = body.days or 30
        now = datetime.datetime.now(datetime.timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_range = {
            "start": (today - datetime.timedelta(days=days))
                     .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    payload = {"metrics": body.metrics, "time_range": time_range,
               "filters": filters}
    if body.dimensions:
        payload["dimensions"] = body.dimensions
    if body.granularity:
        payload["granularity"] = body.granularity
    if body.order_by:
        payload["order_by"] = body.order_by
    if body.limit:
        payload["limit"] = body.limit
    try:
        return await provisioning.analytics_query(payload)
    except or_client.OpenRouterError as e:
        raise ApiError(502, "openrouter_analytics",
                       f"Analytics OpenRouter: {e}", error=str(e))


@router.get("/meta")
async def usage_meta(_admin: User = Depends(require_admin)):
    """Metriche, dimensioni e filtri che le analytics OpenRouter accettano.

    Il frontend costruisce da qui i menù della dashboard costi, invece di
    tenersi una copia dell'elenco che invecchia. Solo admin."""
    _require_provisioning()
    try:
        return await provisioning.analytics_meta()
    except or_client.OpenRouterError as e:
        raise ApiError(502, "openrouter_analytics",
                       f"Analytics OpenRouter: {e}", error=str(e))
