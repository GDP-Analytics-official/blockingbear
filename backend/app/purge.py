"""Cancellazione a CASCATA: conversazione, progetto, utente.

Le tre cascate sono annidate (un utente porta i suoi progetti, un progetto
porta le sue chat, una chat porta messaggi, allegati, sandbox e byte su disco)
e vivono qui in UNA sola copia, non nelle rispettive route: è l'unico modo di
garantire la promessa che conta, **eliminare un utente non lascia niente
dietro di sé**. Duplicare una cascata significa poter dimenticare un pezzo in
una delle copie, e un pezzo dimenticato è PII orfana — esattamente ciò che
questa applicazione esiste per evitare.

Regole comuni a tutte e tre:

  - si lavora sempre dentro `chat_anonymization.conversation_lock(scope)`: un
    turno o un job che sta redigendo file dello stesso scope va ASPETTATO, non
    scavalcato (i worker prendono lo stesso lock, vedi project_files.py);
  - prima le righe (una sola commit), poi i byte: se il commit fallisce non
    resta cancellato nessun file di cui il DB parla ancora;
  - il REGISTRO delle entità si elimina col suo PORTATORE — la chat libera o
    il progetto — mai con una singola chat di progetto, dove appartiene a
    tutte le chat e ai file (vedi la nota sullo scope in db.py).
"""

import shutil

from . import chat_anonymization as chat_anon
from . import chat_staging, jobs, probe_store, project_files
from .config import DATA_DIR
from .db import (Attachment, ChatMessage, Conversation, ConversationEntity,
                 ConversationEntityAlias, Job, Project, ProjectFile)
from .openrouter import sandbox

CHATS_DIR = DATA_DIR / "chats"


def _stop_turn(conv_id):
    """Alza il flag di stop del turno in corso: si ferma da solo al primo
    checkpoint. Import ritardato perché le route importano questo modulo."""
    from .routes import chat_routes
    cancel = chat_routes._running.get(conv_id)
    if cancel is not None:
        cancel.set()


def drop_registry(session, scope_id):
    """Il registro PII di uno scope (chat libera o progetto): entità e loro
    alias. Non committa."""
    entity_ids = [row[0] for row in session.query(ConversationEntity.id)
                  .filter_by(conv_id=scope_id)]
    if entity_ids:
        (session.query(ConversationEntityAlias)
         .filter(ConversationEntityAlias.entity_id.in_(entity_ids))
         .delete(synchronize_session=False))
    session.query(ConversationEntity).filter_by(conv_id=scope_id).delete()


def _forget_conversation(session, conv):
    """Le righe di una chat: thread e allegati. Non il registro (lo elimina il
    portatore) e non committa: il chiamante chiude una transazione sola."""
    _stop_turn(conv.id)
    session.query(ChatMessage).filter_by(conv_id=conv.id).delete()
    session.query(Attachment).filter_by(conv_id=conv.id).delete()
    session.delete(conv)


def _drop_conversation_data(conv_id):
    """Tutto ciò che di una chat vive FUORI dal DB: turno in anteprima,
    buffer dell'ultimo stream, byte su disco, container della sandbox."""
    from .routes import chat_routes
    chat_staging.discard(conv_id)
    chat_routes._turn_streams.pop(conv_id, None)
    shutil.rmtree(CHATS_DIR / conv_id, ignore_errors=True)
    sandbox.release(conv_id)


def purge_conversation(session, conv):
    """Una chat, libera o di progetto (commit inclusa).

    Il registro se ne va solo con una chat LIBERA: in un progetto le entità
    hanno conv_id = project_id e appartengono a tutte le sue chat e ai suoi
    file. Per lo stesso motivo il lock di un progetto non si rilascia qui."""
    free = not conv.project_id
    scope = conv.project_id or conv.id
    with chat_anon.conversation_lock(scope):
        _forget_conversation(session, conv)
        if free:
            drop_registry(session, conv.id)
        session.commit()
        _drop_conversation_data(conv.id)
    if free:
        chat_anon.release_lock(conv.id)


def purge_project(session, project):
    """Un progetto: le sue chat (messaggi, allegati, sandbox), il registro
    condiviso, i file con i loro byte e la cartella del progetto."""
    with chat_anon.conversation_lock(project.id):
        convs = (session.query(Conversation)
                 .filter_by(project_id=project.id).all())
        for conv in convs:
            _forget_conversation(session, conv)
        drop_registry(session, project.id)
        files = (session.query(ProjectFile)
                 .filter_by(project_id=project.id).all())
        for pf in files:
            session.delete(pf)
        # I modelli non hanno relationship(), quindi la flush NON ordina le
        # DELETE tra tabelle: senza questo flush il DELETE del progetto può
        # partire prima di quello di file e chat, e Postgres — che le FK le
        # applica davvero — rifiuta. Stessa transazione: l'atomicità resta.
        session.flush()
        session.delete(project)
        session.commit()
        for conv in convs:
            _drop_conversation_data(conv.id)
        for pf in files:
            project_files.drop_file(pf)
        project_files.drop_project_dir(project.id)
    chat_anon.release_lock(project.id)


def purge_user(session, user):
    """TUTTO quello che un utente ha creato, riga utente compresa.

    L'ordine non è arbitrario:

      1. i JOB per primi. Un worker che sta elaborando un file di questo
         utente scrive righe (ProjectFile) e byte dentro il lock del
         progetto: annullarlo prima evita che ricompaia qualcosa subito dopo
         averlo eliminato. L'annullamento è cooperativo, ma le cascate qui
         sotto prendono lo stesso lock, quindi aspettano comunque il worker;
      2. i PROGETTI, che si portano dietro le proprie chat e il registro
         condiviso;
      3. le CHAT rimaste, cioè quelle libere (quelle di progetto sono già
         andate al passo 2) — e nel caso di un admin eliminato, anche le sue
         chat dentro il progetto di qualcun altro, che non tocca il registro
         di quel progetto;
      4. la RIGA utente: chiave OpenRouter, regola del modello predefinito,
         termini personali e lingua sono sue colonne, quindi se ne vanno con
         lei; niente di questo utente resta come impostazione globale.

    La chiave OpenRouter va revocata dal chiamante (è una chiamata di rete,
    async: vedi routes/users.py)."""
    for job in session.query(Job).filter_by(owner_id=user.id).all():
        if job.status in ("queued", "processing"):
            jobs.cancel(job, session)
    (session.query(Job).filter_by(owner_id=user.id)
     .delete(synchronize_session=False))
    session.commit()
    # byte di una sonda pre-upload mai consumata (data/tmp/probes)
    probe_store.drop_owner(user.id)
    for project in session.query(Project).filter_by(owner_id=user.id).all():
        purge_project(session, project)
    for conv in session.query(Conversation).filter_by(owner_id=user.id).all():
        purge_conversation(session, conv)
    session.delete(user)
    session.commit()
