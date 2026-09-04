"""Gestione utenti (solo admin) e delle loro chiavi OpenRouter personali.

Con la management key dell'installazione ogni utente nasce con la SUA chiave di inferenza blockingbear-<username>, con
l'eventuale tetto di spesa scelto qui alla creazione. La chiave segue il
proprietario: si revoca quando l'utente si elimina, si ruota se compromessa,
si aggiorna nel limite dalla dashboard costi. Se OpenRouter non risponde al
momento della creazione, l'utente nasce comunque (la chiave arriva al primo
messaggio in chat, o con POST /{id}/key): un guasto di rete non deve
impedire di creare account."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import purge
from ..auth import hash_password, require_admin
from ..db import Conversation, Project, User, get_session, iso_utc
from ..errors import ApiError
from ..openrouter import client as or_client
from ..openrouter import provisioning

router = APIRouter(prefix="/api/users", tags=["users"])


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8)
    role: str = Field(pattern="^(admin|standard)$")
    # «cambia password al primo accesso»: finché non lo fa, l'utente può
    # solo cambiarla (vedi auth.user_from_token)
    must_change_password: bool = False
    # tetto di spesa della chiave OpenRouter personale (USD; None = nessuno)
    # e sua cadenza di azzeramento.
    key_limit: float | None = Field(None, ge=0)
    key_limit_reset: str | None = Field(None,
                                        pattern="^(daily|weekly|monthly)$")


class KeyPatch(BaseModel):
    """Modifiche alla chiave OpenRouter dell'utente. `limit` assente = non
    toccare; limit=0 in UI si traduce in null a monte (il form manda None
    esplicito con `clear_limit`). Non esiste una disabilitazione: per bloccare
    qualcuno l'admin elimina l'utente, e l'eliminazione revoca la chiave."""
    limit: float | None = Field(None, ge=0)
    clear_limit: bool = False
    limit_reset: str | None = Field(None, pattern="^(daily|weekly|monthly)$")
    clear_limit_reset: bool = False


def _row(u: User, counts=None):
    d = {"id": u.id, "username": u.username, "role": u.role,
         "created_at": iso_utc(u.created_at),
         "must_change_password": bool(u.must_change_password),
         "key_name": provisioning.key_name(u.username),
         "key_hash": u.openrouter_key_hash,
         "has_key": bool(u.openrouter_key)}
    if counts is not None:
        projects, chats = counts
        # quanto si porta via la cancellazione: la conferma in UI lo dice
        d["n_projects"] = projects.get(u.id, 0)
        d["n_chats"] = chats.get(u.id, 0)
    return d


@router.get("")
def list_users(_admin: User = Depends(require_admin),
               session: Session = Depends(get_session)):
    """Elenco completo degli utenti (solo admin).

    Ogni riga porta con sé quanti progetti e quante chat possiede: è il
    numero che la conferma di cancellazione mostra all'amministratore."""
    counts = (
        dict(session.query(Project.owner_id, func.count(Project.id))
             .group_by(Project.owner_id)),
        dict(session.query(Conversation.owner_id, func.count(Conversation.id))
             .group_by(Conversation.owner_id)),
    )
    return [_row(u, counts) for u in session.query(User).order_by(User.id)]


@router.post("", status_code=201)
async def create_user(body: UserIn, _admin: User = Depends(require_admin),
                      session: Session = Depends(get_session)):
    """Crea un utente (solo admin). Username già preso: 409 `username_taken`.

    Se il server ha una management key OpenRouter, all'utente viene creata
    anche la sua chiave personale, con l'eventuale tetto di spesa richiesto.
    Se quella creazione fallisce l'utente resta comunque creato e la risposta
    porta `key_error`: la chiave si riprova dalla UI o al primo messaggio."""
    if session.query(User).filter_by(username=body.username.strip()).first():
        raise ApiError(409, "username_taken", "Username già in uso.")
    u = User(username=body.username.strip(),
             password_hash=hash_password(body.password), role=body.role,
             must_change_password=body.must_change_password)
    session.add(u)
    session.commit()
    out = _row(u)
    if provisioning.configured():
        try:
            await provisioning.ensure_user_key(
                session, u, limit=body.key_limit,
                limit_reset=body.key_limit_reset)
        except (or_client.OpenRouterError, ValueError) as e:
            # utente creato, chiave no: si riprova da UI o al primo messaggio
            out["key_error"] = str(e)
        out.update(_row(u))
    return out


@router.delete("/{user_id}")
async def delete_user(user_id: int, admin: User = Depends(require_admin),
                      session: Session = Depends(get_session)):
    """Elimina l'utente E TUTTO quello che ha creato: progetti (con file, byte
    su disco e registro), chat (messaggi, allegati, sandbox), job in coda,
    impostazioni personali e chiave OpenRouter.

    Nessun ostacolo: l'amministratore che decide di rimuovere un account deve
    poterlo fare in un colpo, e i dati di chi non c'è più non devono restare
    in giro. Nessuno stato dell'utente — progetti aperti, job in coda, turni
    in corso — rifiuta la cancellazione. La cascata sta in app/purge.py."""
    if user_id == admin.id:
        raise ApiError(400, "delete_self",
                       "Non puoi eliminare il tuo stesso account.")
    u = session.get(User, user_id)
    if u is None:
        raise ApiError(404, "user_not_found", "Utente non trovato.")
    # la chiave non deve sopravvivere al proprietario; se OpenRouter non
    # risponde ora, si segnala e si rimuove a mano dalla dashboard
    key_error = await provisioning.drop_user_key(u)
    purge.purge_user(session, u)
    return {"ok": True, **({"key_error": key_error} if key_error else {})}


@router.post("/{user_id}/key")
async def rotate_key(user_id: int, body: KeyPatch = None,
                     _admin: User = Depends(require_admin),
                     session: Session = Depends(get_session)):
    """Crea la chiave se manca, o la RUOTA (revoca + ricrea) se esiste:
    rimedio per chiave compromessa o creazione fallita a suo tempo."""
    u = session.get(User, user_id)
    if u is None:
        raise ApiError(404, "user_not_found", "Utente non trovato.")
    body = body or KeyPatch()
    limit = None if body.clear_limit else body.limit
    reset = None if body.clear_limit_reset else body.limit_reset
    try:
        await provisioning.rotate_user_key(session, u, limit=limit,
                                           limit_reset=reset)
    except (or_client.OpenRouterError, ValueError) as e:
        raise ApiError(502, "openrouter_refused",
                       f"OpenRouter ha rifiutato l'operazione: {e}", error=str(e))
    return _row(u)


@router.patch("/{user_id}/key")
async def patch_key(user_id: int, body: KeyPatch,
                    _admin: User = Depends(require_admin),
                    session: Session = Depends(get_session)):
    """Aggiorna limite/cadenza/stato della chiave su OpenRouter. Non tocca il
    testo della chiave: per quello c'è la rotazione."""
    u = session.get(User, user_id)
    if u is None:
        raise ApiError(404, "user_not_found", "Utente non trovato.")
    if not u.openrouter_key_hash:
        raise ApiError(409, "user_has_no_key",
                       "L'utente non ha ancora una chiave: creala "
                       "prima (POST /key).")
    fields = {}
    if body.clear_limit:
        fields["limit"] = None
    elif body.limit is not None:
        fields["limit"] = body.limit
    if body.clear_limit_reset:
        fields["limit_reset"] = None
    elif body.limit_reset is not None:
        fields["limit_reset"] = body.limit_reset
    if not fields:
        raise ApiError(422, "no_changes", "Nessuna modifica richiesta.")
    try:
        data = await provisioning.update_key(u.openrouter_key_hash, **fields)
    except or_client.OpenRouterError as e:
        raise ApiError(502, "openrouter_refused",
                       f"OpenRouter ha rifiutato l'operazione: {e}", error=str(e))
    return {**_row(u), "key": data}
