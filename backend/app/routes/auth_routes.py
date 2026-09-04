"""Login, profilo, cambio password."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import settings_store
from ..auth import (current_user_password_pending, hash_password, make_token,
                    verify_password)
from ..db import User, get_session
from ..errors import ApiError

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8)


@router.post("/login")
def login(body: LoginIn, session: Session = Depends(get_session)):
    """Autentica username + password e restituisce il token di sessione.

    Nella risposta viaggiano anche i dati che il frontend deve avere subito,
    prima di qualunque altra chiamata: il ruolo (`admin` / `standard`), la
    lingua del profilo e `must_change_password`. Credenziali sbagliate: 401
    `bad_credentials`, senza distinguere fra utente inesistente e password
    errata."""
    user = session.query(User).filter_by(username=body.username.strip()).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise ApiError(401, "bad_credentials", "Credenziali non valide.")
    # must_change_password vero = il frontend mostra SOLO la schermata di
    # cambio password (e il backend blocca comunque tutto il resto, vedi
    # user_from_token)
    return {"token": make_token(user), "username": user.username,
            "role": user.role, "lang": settings_store.user_lang(user),
            "must_change_password": bool(user.must_change_password),
            "tour_done": bool(user.tour_done)}


@router.get("/me")
def me(user: User = Depends(current_user_password_pending)):
    """Profilo di chi possiede il token: username, ruolo, lingua e se deve
    ancora cambiare la password.

    È l'unico endpoint, con POST /password, raggiungibile mentre il cambio
    password è pendente: serve al frontend per sapere che deve mostrare quella
    schermata."""
    # `lang` può essere None: significa «questo profilo non ha mai scelto»,
    # e il frontend allora ci salva la lingua scelta nel browser (si scrive
    # con PUT /api/settings/my-language).
    return {"username": user.username, "role": user.role,
            "lang": settings_store.user_lang(user),
            "must_change_password": bool(user.must_change_password),
            "tour_done": bool(user.tour_done)}


@router.post("/password")
def change_password(body: PasswordIn,
                    user: User = Depends(current_user_password_pending),
                    session: Session = Depends(get_session)):
    """Cambia la password dell'utente autenticato e azzera l'obbligo di
    cambio al primo accesso.

    La nuova password è lunga almeno 8 caratteri e deve essere diversa
    dall'attuale (422 `password_same`). Password attuale sbagliata: 403
    `wrong_password`, non 401."""
    # 403 e non 401: per il frontend un 401 con token in mano significa
    # «sessione morta» e fa il logout d'ufficio (api.js) — sbagliare la
    # vecchia password non deve buttare fuori nessuno
    if not verify_password(body.old_password, user.password_hash):
        raise ApiError(403, "wrong_password",
                       "La password attuale non è corretta.")
    if body.new_password == body.old_password:
        # con l'obbligo di cambio al primo accesso, riconfermare la password
        # d'ufficio vanificherebbe il flag
        raise ApiError(422, "password_same",
                       "La nuova password deve essere diversa da quella attuale.")
    db_user = session.get(User, user.id)
    db_user.password_hash = hash_password(body.new_password)
    db_user.must_change_password = False
    session.commit()
    return {"ok": True}
