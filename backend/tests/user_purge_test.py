"""Cancellazione di un utente da parte dell'amministratore: deve riuscire
SEMPRE e non lasciare niente dietro di sé (app/purge.py).

App FastAPI ridotta (auth + utenti + progetti + chat), finto OpenRouter,
nessun modello PII e nessuna coda: i file di progetto si caricano in un
progetto IN CHIARO (save_clear, nessuna inferenza) e le righe che di solito
scrive l'anonimizzazione — registro, messaggi — si seminano a mano. Quello
che si prova qui è la CASCATA, non il rilevatore.

Copre: l'utente con progetti si elimina senza errori, progetti +
file + byte su disco + registro di progetto, chat di progetto e chat libere
con messaggi, allegati e cartelle, biglietti di sonda pre-upload, job in coda,
la riga utente con le sue impostazioni personali; la roba dell'admin resta
intatta; le due cascate più piccole (DELETE chat, DELETE progetto) restano
indipendenti da quella dell'utente.

Uso:
    python backend/tests/user_purge_test.py
"""
import io
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))

import chat_mock_openrouter as mock                              # noqa: E402

os.environ["BLOCKINGBEAR_DATA_DIR"] = str(HERE / "data" / "test_user_purge")
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
os.environ["BLOCKINGBEAR_OPENROUTER_BASE_URL"] = mock.BASE_URL

from fastapi import FastAPI                                      # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import probe_store, settings_store                      # noqa: E402
from app.auth import seed_admin                                  # noqa: E402
from app.config import DATA_DIR                                  # noqa: E402
from app.db import (Attachment, ChatMessage, Conversation,       # noqa: E402
                    ConversationEntity, ConversationEntityAlias, Job,
                    Project, ProjectFile, User, init_db)
from app.openrouter import client as or_client                    # noqa: E402
from app.openrouter import sandbox                                # noqa: E402
from app.routes import (auth_routes, chat_routes, projects,       # noqa: E402
                        settings_routes, users)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def seed_registry(SessionLocal, scope, value, n):
    """Una entità col suo alias nello scope dato (progetto o chat libera):
    è la forma delle righe che scrive l'anonimizzazione."""
    with SessionLocal() as s:
        e = ConversationEntity(conv_id=scope, label="FULLNAME",
                               placeholder=f"[FULLNAME_{n}]",
                               canonical_value=value)
        s.add(e)
        s.commit()
        s.add(ConversationEntityAlias(entity_id=e.id, original_surface=value,
                                      normalized_key=value.casefold()))
        s.commit()
        return e.id


def seed_message(SessionLocal, conv_id, text, seq=1):
    with SessionLocal() as s:
        s.add(ChatMessage(conv_id=conv_id, seq=seq, role="user",
                          content=text, display_content=text,
                          model_content=text))
        s.commit()


def make_env(client, auth, SessionLocal, label, username):
    """Un utente col suo mondo: progetto in chiaro con un file, chat di
    progetto, chat libera con allegato, registri, messaggi, un job in coda e
    un biglietto di sonda. Torna gli id per i controlli."""
    p = client.post("/api/projects", json={"name": f"Commessa {label}",
                                           "anonymized": False},
                    headers=auth).json()
    pid = p["id"]
    up = client.post(f"/api/projects/{pid}/files",
                     files={"file": (f"note_{label}.txt",
                                     io.BytesIO(b"Cliente: Mario Rossi\n"),
                                     "text/plain")}, headers=auth)
    fid = up.json()["file"]["id"]
    pconv = client.post("/api/chats", json={"project_id": pid,
                                            "title": "chat di progetto"},
                        headers=auth).json()["id"]
    seed_message(SessionLocal, pconv, "riassumi i file")
    # anche la chat di progetto ha la sua cartella su disco
    client.post(f"/api/chats/{pconv}/attachments",
                files={"file": (f"in_chat_{label}.txt",
                                io.BytesIO(b"Allegato del turno\n"),
                                "text/plain")}, headers=auth)
    free = client.post("/api/chats", json={"title": "chat libera",
                                           "anonymized": True},
                       headers=auth).json()["id"]
    seed_message(SessionLocal, free, "ciao")
    att = client.post(f"/api/chats/{free}/attachments",
                      files={"file": (f"allegato_{label}.txt",
                                      io.BytesIO(b"Referente: Mario Rossi\n"),
                                      "text/plain")},
                      headers=auth).json()["id"]
    ent_project = seed_registry(SessionLocal, pid, f"Mario Rossi {label}", 1)
    ent_free = seed_registry(SessionLocal, free, f"Anna Bianchi {label}", 2)
    probe_id = client.post("/api/projects/probe",
                           files={"file": (f"sonda_{label}.txt",
                                           io.BytesIO(b"x" * 32),
                                           "text/plain")},
                           headers=auth).json()["probe_id"]
    with SessionLocal() as s:
        owner = s.query(User).filter_by(username=username).one().id
        job = Job(owner_id=owner, filename=f"in_coda_{label}.pdf",
                  kind="project_upload", project_id=pid, status="queued")
        s.add(job)
        s.commit()
        job_id = job.id
    return {"project": pid, "file": fid, "project_chat": pconv, "free": free,
            "attachment": att, "ent_project": ent_project,
            "ent_free": ent_free, "probe": probe_id, "job": job_id,
            "owner": owner}


def rows_of(SessionLocal, env, owner_id):
    """Conta tutto quello che deve sparire con l'utente."""
    with SessionLocal() as s:
        return {
            "projects": s.query(Project).filter_by(owner_id=owner_id).count(),
            "project_files": s.query(ProjectFile).filter_by(
                project_id=env["project"]).count(),
            "conversations": s.query(Conversation).filter_by(
                owner_id=owner_id).count(),
            "messages": s.query(ChatMessage).filter(
                ChatMessage.conv_id.in_((env["project_chat"], env["free"]))
            ).count(),
            "attachments": s.query(Attachment).filter(
                Attachment.conv_id.in_((env["project_chat"], env["free"]))
            ).count(),
            "entities": s.query(ConversationEntity).filter(
                ConversationEntity.conv_id.in_((env["project"], env["free"]))
            ).count(),
            "aliases": s.query(ConversationEntityAlias).filter(
                ConversationEntityAlias.entity_id.in_(
                    (env["ent_project"], env["ent_free"]))).count(),
            "jobs": s.query(Job).filter_by(owner_id=owner_id).count(),
        }


def dirs_of(env):
    return {
        "project_dir": (DATA_DIR / "projects" / env["project"]).exists(),
        "project_chat_dir": (DATA_DIR / "chats" / env["project_chat"]).exists(),
        "free_chat_dir": (DATA_DIR / "chats" / env["free"]).exists(),
        "probe_file": (DATA_DIR / "tmp" / "probes"
                       / f"{env['probe']}.bin").exists(),
    }


def main():
    db_file = Path(os.environ["BLOCKINGBEAR_DATA_DIR"]) / "blockingbear.db"
    db_file.unlink(missing_ok=True)
    stop_mock = mock.serve()

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(users.router)
    app.include_router(chat_routes.router)
    app.include_router(projects.router)
    app.include_router(settings_routes.router)
    SessionLocal = init_db()
    with SessionLocal() as s:
        seed_admin(s)
        settings_store.set_anon_defaults(s, [], [])
    sandbox.status = lambda: {"available": False, "detecting": False}

    client = TestClient(app)
    token = client.post("/api/auth/login", json={
        "username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    mock.grant_key(SessionLocal)

    try:
        # --- due mondi paralleli: test2 e l'admin -------------------------
        r = client.post("/api/users", json={"username": "test2",
                                            "password": "test2pass",
                                            "role": "standard"}, headers=auth)
        uid = r.json()["id"]
        check("utente test2 creato", r.status_code == 201, str(uid))
        t2 = client.post("/api/auth/login", json={
            "username": "test2", "password": "test2pass"}).json()["token"]
        auth2 = {"Authorization": f"Bearer {t2}"}
        # impostazioni personali dell'utente (colonne della sua riga)
        client.put("/api/settings/my-language", json={"lang": "en"},
                   headers=auth2)
        client.put("/api/settings/my-terms", json={"custom_terms": [
            {"text": "Progetto Delta", "tag": "CUSTOM"}]}, headers=auth2)
        with SessionLocal() as s:
            u = s.get(User, uid)
            check("impostazioni personali salvate",
                  u.lang == "en" and "Delta" in (u.anon_terms_json or ""))

        env2 = make_env(client, auth2, SessionLocal, "test2", "test2")
        env_admin = make_env(client, auth, SessionLocal, "admin", "admin")
        before = rows_of(SessionLocal, env2, uid)
        check("mondo di test2 popolato",
              all(before[k] >= 1 for k in before), str(before))
        check("file del progetto su disco",
              (DATA_DIR / "projects" / env2["project"]).is_dir())
        check("cartelle degli allegati su disco",
              (DATA_DIR / "chats" / env2["free"]).is_dir()
              and (DATA_DIR / "chats" / env2["project_chat"]).is_dir())
        check("biglietto di sonda su disco",
              (DATA_DIR / "tmp" / "probes"
               / f"{env2['probe']}.bin").is_file())

        # --- conteggi nella lista utenti (la conferma in UI li mostra) ----
        row = next(u for u in client.get("/api/users", headers=auth).json()
                   if u["id"] == uid)
        check("lista utenti: conteggi per la conferma",
              row["n_projects"] == 1 and row["n_chats"] == 2,
              f"{row.get('n_projects')} progetti, {row.get('n_chats')} chat")

        # --- la cancellazione ---------------------------------------------
        r = client.delete(f"/api/users/{uid}", headers=auth)
        check("utente CON progetti eliminato",
              r.status_code == 200 and r.json().get("ok") is True,
              f"{r.status_code} {r.text[:120]}")

        after = rows_of(SessionLocal, env2, uid)
        for key, n in after.items():
            check(f"niente {key} residui", n == 0, f"{n} righe")
        for key, present in dirs_of(env2).items():
            check(f"niente {key} residuo", present is False)
        with SessionLocal() as s:
            check("riga utente (e sue impostazioni) eliminata",
                  s.get(User, uid) is None
                  and s.query(User).filter_by(username="test2").count() == 0)
        check("test2 non accede più",
              client.post("/api/auth/login", json={
                  "username": "test2",
                  "password": "test2pass"}).status_code == 401)
        check("token di test2 non vale più",
              client.get("/api/projects", headers=auth2).status_code == 401)

        # --- l'admin non ha perso niente ----------------------------------
        mine = rows_of(SessionLocal, env_admin,
                       env_admin["owner"])
        check("mondo dell'admin intatto",
              all(mine[k] >= 1 for k in mine), str(mine))
        check("file dell'admin ancora su disco",
              (DATA_DIR / "projects" / env_admin["project"]).is_dir()
              and (DATA_DIR / "chats" / env_admin["free"]).is_dir())
        check("biglietto di sonda dell'admin intatto",
              probe_store.take(env_admin["probe"],
                               env_admin["owner"]) is not None)

        # --- le due cascate più piccole: DELETE chat, DELETE progetto ----
        free = client.post("/api/chats", json={"title": "usa e getta",
                                               "anonymized": True},
                           headers=auth).json()["id"]
        seed_message(SessionLocal, free, "ciao")
        ent = seed_registry(SessionLocal, free, "Carlo Verdi", 9)
        client.post(f"/api/chats/{free}/attachments",
                    files={"file": ("x.txt", io.BytesIO(b"Carlo Verdi\n"),
                                    "text/plain")}, headers=auth)
        check("DELETE chat libera",
              client.delete(f"/api/chats/{free}",
                            headers=auth).status_code == 200)
        with SessionLocal() as s:
            check("chat libera: righe e registro via",
                  s.get(Conversation, free) is None
                  and s.get(ConversationEntity, ent) is None
                  and s.query(ChatMessage).filter_by(conv_id=free).count() == 0
                  and s.query(Attachment).filter_by(conv_id=free).count() == 0)
        check("chat libera: cartella via",
              not (DATA_DIR / "chats" / free).exists())

        # la chat di progetto NON porta via il registro del progetto
        pconv = env_admin["project_chat"]
        check("DELETE chat di progetto",
              client.delete(f"/api/chats/{pconv}",
                            headers=auth).status_code == 200)
        with SessionLocal() as s:
            check("registro del progetto sopravvive alla sua chat",
                  s.get(ConversationEntity,
                        env_admin["ent_project"]) is not None)

        check("DELETE progetto",
              client.delete(f"/api/projects/{env_admin['project']}",
                            headers=auth).status_code == 200)
        with SessionLocal() as s:
            check("progetto: file, registro e cartella via",
                  s.get(Project, env_admin["project"]) is None
                  and s.get(ProjectFile, env_admin["file"]) is None
                  and s.get(ConversationEntity,
                            env_admin["ent_project"]) is None
                  and not (DATA_DIR / "projects"
                           / env_admin["project"]).exists())
    finally:
        stop_mock()

    print(f"\n  {PASS} PASS, {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
