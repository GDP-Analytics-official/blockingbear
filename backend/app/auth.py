"""Autenticazione: pbkdf2 (stdlib, nessuna dipendenza nativa) + JWT HS256.

Due ruoli: `admin` (gestisce gli utenti, vede tutti i documenti) e `standard`
(anonimizza e vede solo i propri). Il primo admin lo crea il wizard di
installazione (routes/setup_routes.py) con la password scelta da chi installa:
nessun account nasce con una password predefinita.
"""

import datetime
import hashlib
import hmac
import secrets

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from . import settings_store
from .config import jwt_secret
from .db import User, get_session
from .errors import ApiError
from .logging_setup import get_logger

log = get_logger("blockingbear.auth")

_PBKDF2_ITER = 240_000
_bearer = HTTPBearer(auto_error=False)


def hash_password(password, salt=None, iterations=_PBKDF2_ITER):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations)
    return f"pbkdf2${iterations}${salt}${dk.hex()}"


def verify_password(password, stored):
    try:
        _, iters, salt, _ = stored.split("$")
        return hmac.compare_digest(hash_password(password, salt, int(iters)), stored)
    except (ValueError, TypeError):
        return False


def make_token(user):
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        # la durata si legge a OGNI login: modificarla dal pannello admin vale
        # per i token emessi da quel momento (quelli in giro non cambiano)
        "exp": datetime.datetime.now(datetime.timezone.utc)
               + datetime.timedelta(hours=settings_store.current("token_ttl_hours")),
    }
    return jwt.encode(payload, jwt_secret(), algorithm="HS256")


def user_from_token(token, session, allow_pending_password=False):
    """Valida un JWT "nudo" e ritorna lo User. Serve anche fuori dall'header
    Authorization: EventSource non può impostare header, quindi l'endpoint
    SSE riceve il token come query param.

    Se l'admin ha imposto il cambio password al primo accesso
    (User.must_change_password), la sessione vale SOLO per cambiarla: tutto il
    resto risponde 403 finché non l'ha fatto. Gli endpoint che devono restare
    raggiungibili (vedi current_user_password_pending) passano
    allow_pending_password=True."""
    if not token:
        raise ApiError(401, "token_missing", "Token mancante.")
    try:
        payload = jwt.decode(token, jwt_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise ApiError(401, "session_expired", "Sessione scaduta: rifai il login.")
    except jwt.InvalidTokenError:
        raise ApiError(401, "token_invalid", "Token non valido.")
    user = session.get(User, int(payload["sub"]))
    if user is None:
        raise ApiError(401, "user_not_found", "Utente non trovato.")
    if user.must_change_password and not allow_pending_password:
        # 403, non 401: il token è valido e il frontend non deve buttare
        # fuori l'utente, deve portarlo alla schermata di cambio password
        raise ApiError(403, "password_change_required",
                       "Devi scegliere una nuova password prima di continuare.")
    return user


def current_user(creds: HTTPAuthorizationCredentials = Depends(_bearer),
                 session: Session = Depends(get_session)) -> User:
    return user_from_token(creds.credentials if creds else None, session)


def current_user_password_pending(
        creds: HTTPAuthorizationCredentials = Depends(_bearer),
        session: Session = Depends(get_session)) -> User:
    """Come current_user, ma senza il blocco del cambio password obbligatorio:
    la usano /api/auth/me e /api/auth/password, gli unici endpoint che
    servono PRIMA di aver cambiato la password."""
    return user_from_token(creds.credentials if creds else None, session,
                           allow_pending_password=True)


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise ApiError(403, "admin_only",
                       "Operazione riservata all'amministratore.")
    return user


def seed_admin(session: Session, password="admin"):
    """Crea l'utente admin se non esiste nessun utente. Lo usano le SUITE DI
    TEST per avere un amministratore senza passare dal wizard; l'applicazione
    non lo chiama mai (il primo admin nasce da /api/setup/admin)."""
    if session.query(User).count() == 0:
        session.add(User(username="admin", password_hash=hash_password(password),
                         role="admin"))
        session.commit()
