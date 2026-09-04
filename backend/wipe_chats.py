"""Utilità di manutenzione: cancella TUTTE le conversazioni della chat.

Porta via messaggi, allegati, registro delle entità e i byte in data/chats.
I PROGETTI e i loro file non vengono toccati: hanno un registro proprio e una
loro cancellazione a cascata (app/purge.py).

Serve per svuotare un'installazione di prova senza rifare gli utenti, e come
ultima risorsa se il registro di una conversazione va in uno stato che non si
riesce a correggere dall'interfaccia. Chiede conferma, a meno di --yes.

Uso:
    python backend/wipe_chats.py [--yes]
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))     # backend/

from app.config import DATA_DIR                                  # noqa: E402
from app.db import (Attachment, ChatMessage, Conversation,       # noqa: E402
                    ConversationEntity, ConversationEntityAlias, init_db)


def main():
    SessionLocal = init_db()
    with SessionLocal() as s:
        n = s.query(Conversation).count()
        if not n:
            print("Nessuna conversazione da cancellare.")
            return
        if "--yes" not in sys.argv:
            print(f"{n} conversazioni verranno cancellate insieme a messaggi, "
                  f"allegati e registro entità.")
            if input("Confermi? [scrivi 'si'] ").strip().lower() != "si":
                print("Annullato.")
                return
        for model in (ConversationEntityAlias, ConversationEntity,
                      Attachment, ChatMessage, Conversation):
            deleted = s.query(model).delete()
            print(f"  {model.__tablename__}: {deleted}")
        s.commit()
    shutil.rmtree(DATA_DIR / "chats", ignore_errors=True)
    print("Fatto: cartella data/chats rimossa.")


if __name__ == "__main__":
    main()
