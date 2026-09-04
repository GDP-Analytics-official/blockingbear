"""Test rapidi del registro PII della chat (nessun modello, rete o Docker)."""

import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
_tmp = tempfile.TemporaryDirectory(prefix="blockingbear-chat-anon-")
os.environ["BLOCKINGBEAR_DATA_DIR"] = _tmp.name
# DB isolato quanto i file: mai la DATABASE_URL di esercizio (backend/.env).
# Vuota = sqlite nei dati del test; BLOCKINGBEAR_TEST_DATABASE_URL = usa-e-getta.
os.environ["DATABASE_URL"] = os.environ.get("BLOCKINGBEAR_TEST_DATABASE_URL", "")
sys.path.insert(0, str(HERE))

from app import db, jobs, settings_store  # noqa: E402
from app.chat_anonymization import (ConversationEngine, StreamDecoder,
                                    TurnAnonymizationError,
                                    anon_options_view, anonymize_turn,
                                    attachment_model_name, attachment_path,
                                    conversation_lock, conversation_mapping,
                                    decode_for_display, excluded_groups,
                                    known_surface_leaks, protected_placeholders,
                                    set_anon_options,
                                    upload_eligibility)  # noqa: E402
from app.db import (Attachment, ChatMessage, Conversation, ConversationEntity,
                    ConversationEntityAlias, User, init_db)  # noqa: E402
from app.engine.txt import redact_txt  # noqa: E402

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class FakeEngine:
    """Detector deterministico: (label, valore) passati al costruttore."""

    def __init__(self, entries):
        self.entries = entries

    def analyze(self, text, excluded=None, **_kwargs):
        # come il motore vero: le categorie escluse si filtrano sulla label
        # grezza dei candidati (engine.core.analyze)
        skip = set(excluded or ())
        entities = []
        for label, value in self.entries:
            if label in skip:
                continue
            for match in re.finditer(re.escape(value), text, re.IGNORECASE):
                entities.append({
                    "label": label, "start": match.start(), "end": match.end(),
                    "value": text[match.start():match.end()], "source": "test",
                    "validated": True, "ph": "[LOCAL_1]",
                })
        entities.sort(key=lambda e: e["start"])
        return {"entities": entities, "anonymized_text": text, "mapping": {},
                "n_entities": len(entities), "n_unique": len(entities),
                "by_label": {}}


SessionLocal = init_db()


def analyze(conv_id, text, entries):
    with conversation_lock(conv_id), SessionLocal() as session:
        conv = session.get(Conversation, conv_id)
        result = ConversationEngine(FakeEngine(entries), session, conv).analyze(text)
        session.commit()
        return result


def main():
    with SessionLocal() as session:
        # l'utente serve DAVVERO: su Postgres la FK owner_id è applicata
        # (su SQLite no, e la fixture se la cavava senza)
        owner = User(username="anon_test", password_hash="x", role="standard")
        session.add(owner)
        session.flush()
        conv = Conversation(owner_id=owner.id)
        session.add(conv)
        session.commit()
        cid = conv.id
        check("policy iniziale optional",
              settings_store.chat_anonymization_policy(session) == "optional")
        settings_store.set_chat_anonymization_policy(session, "required")
        check("policy required persistita",
              settings_store.chat_anonymization_policy(session) == "required")

    first = analyze(cid, "Azienda X S.r.l.", [("ORG", "Azienda X S.r.l.")])
    second = analyze(cid, "AZIENDA X SRL", [("ORG", "AZIENDA X SRL")])
    check("alias ORG -> stesso placeholder",
          first["anonymized_text"] == "[ORG_1]"
          and second["anonymized_text"] == "[ORG_1]")
    missed = analyze(cid, "Contatta Azienda X S.r.l.", [])
    check("alias noto sostituito anche se il detector lo manca",
          missed["anonymized_text"].startswith("Contatta [ORG_1]")
          and "Azienda X" not in missed["anonymized_text"],
          missed["anonymized_text"])

    with SessionLocal() as session:
        mapping = conversation_mapping(session, cid, include_aliases=True)
        check("mapping versionato non riscrive il passato",
              conversation_mapping(session, cid, max_version=0) == {})
        aliases = session.query(ConversationEntityAlias).count()
        out, report = redact_txt(
            "Azienda X S.r.l. / AZIENDA X SRL".encode("utf-8"), mapping)
        redacted = out.decode("utf-8-sig")
        check("redattore supporta più alias per TAG",
              redacted.count("[ORG_1]") == 2 and not report["residual"],
              ascii(redacted))
        check("alias multipli persistiti", aliases == 2, str(aliases))

    # Allocazioni concorrenti: il lock conversation-scoped serializza due
    # sessioni SQLite indipendenti e i contatori non collidono.
    values = ["mario@example.it", "luigi@example.it"]
    threads = [threading.Thread(target=analyze,
                                args=(cid, value, [("EMAIL", value)]))
               for value in values]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with SessionLocal() as session:
        emails = (session.query(ConversationEntity)
                  .filter_by(conv_id=cid, label="EMAIL").all())
        check("contatori atomici nei job concorrenti",
              {e.placeholder for e in emails} == {"[EMAIL_1]", "[EMAIL_2]"},
              str(sorted(e.placeholder for e in emails)))

        canonical = conversation_mapping(session, cid)
        shown, spans = decode_for_display(
            "**[ORG_1]** e `[ORG_1]` [link](https://x/[ORG_1]) [UNKNOWN_9]",
            canonical)
        check("decode selettivo e TAG sconosciuto invariato",
              shown.startswith("**Azienda X S.r.l.**")
              and "`[ORG_1]`" in shown and "https://x/[ORG_1]" in shown
              and "[UNKNOWN_9]" in shown and len(spans) == 1,
              shown)
        hidden = protected_placeholders(
            "`[ORG_1]`\n```csv\nazienda;[ORG_1]\n```\n"
            "[link](https://x/[ORG_1])", canonical)
        check("valori protetti disponibili al renderer del codice",
              hidden == {"[ORG_1]": "Azienda X S.r.l."}, str(hidden))
        decoder = StreamDecoder(canonical)
        first = decoder.feed("Usa `[OR")
        first_hidden = decoder.take_protected()
        second = decoder.feed("G_1]`.\n```csv\nx;[OR")
        second_hidden = decoder.take_protected()
        third = decoder.feed("G_1]\n```\n")
        third_hidden = decoder.take_protected()
        check("stream codice: TAG spezzati decodificabili appena completi",
              first == ("Usa ", []) and not first_hidden
              and "`[ORG_1]`" in second[0]
              and second_hidden == {"[ORG_1]": "Azienda X S.r.l."}
              and third_hidden == {"[ORG_1]": "Azienda X S.r.l."},
              repr((first, second, third)))

        user = ChatMessage(conv_id=cid, seq=1, role="user",
                           content="Ciao [EMAIL_1]", model_content="Ciao [EMAIL_1]",
                           display_content="Ciao mario@example.it", anonymized=1)
        assistant = ChatMessage(conv_id=cid, seq=2, role="assistant",
                                content="Scrivi a [EMAIL_1]",
                                model_content="Scrivi a [EMAIL_1]", anonymized=1)
        session.add_all([user, assistant])
        session.commit()
        check("history usa model_content, UI usa display_content",
              user.to_openrouter()["content"] == "Ciao [EMAIL_1]"
              and user.descriptor()["content"] == "Ciao mario@example.it")
        desc = assistant.descriptor(canonical, decode_for_display)
        check("assistant canonico, risposta UI con span",
              assistant.to_openrouter()["content"] == "Scrivi a [EMAIL_1]"
              and desc["content"] == "Scrivi a mario@example.it"
              and len(desc.get("entities", [])) == 1)
    # --- ammissibilità all'upload (solo formato, nessun rilevamento) ------
    # con lo stack OCR installato le immagini si ACCETTANO (la lettura avviene
    # all'invio, se l'utente attiva l'OCR nel popup); senza stack, rifiuto
    # con la stessa motivazione
    from app.engine import image_ocr
    if image_ocr.available():
        check("immagine ammessa in chat anonimizzata (OCR disponibile)",
              upload_eligibility("foto.png", b"\x89PNG\r\n") is None)
    else:
        check("immagine rifiutata senza stack OCR",
              "OCR" in (upload_eligibility("foto.png", b"\x89PNG\r\n") or ""))
    check("formato ignoto rifiutato con motivo",
          upload_eligibility("programma.exe", b"MZ") is not None)
    check("csv ammesso dal redattore di testo",
          upload_eligibility("clienti.csv", b"nome;citta\nMario;Lodi\n") is None)
    check("file di testo vuoto rifiutato con motivo",
          upload_eligibility("vuoto.txt", b"   ") is not None)

    # --- il turno: due file + prompt anonimizzati insieme -------------------
    # Il caso che il test presidia: il PRIMO file contiene una superficie
    # ("Acme Analytics") che il detector vede solo nel SECONDO ("Acme
    # Analytics Srl"). Anonimizzando all'upload il primo file resterebbe in
    # chiaro e all'invio il controllo di uscita bloccherebbe la conversazione.
    # Anonimizzando il turno intero, quando si redige il registro c'è già
    # tutto.
    def make_att(session, conv_id, name, text, index):
        original = Path(_tmp.name) / f"{conv_id}_{name}"
        original.write_text(text, encoding="utf-8")
        att = Attachment(conv_id=conv_id, direction="in", source="upload",
                         filename=name, display_filename=name,
                         model_filename=f"allegato_{index:02d}.txt",
                         mime="text/plain", anonymization_status="pending",
                         size=original.stat().st_size,
                         original_path=str(original))
        session.add(att)
        session.flush()
        return att

    with SessionLocal() as session:
        turn_conv = Conversation(owner_id=1, anonymized=1)
        session.add(turn_conv)
        session.flush()
        turn_cid = turn_conv.id
        first_att = make_att(session, turn_cid, "Verbale.txt",
                             "Presente per Acme Analytics il referente.", 1)
        second_att = make_att(session, turn_cid, "Contratto.txt",
                              "Contratto con Acme Analytics Srl e Mario Rossi.", 2)
        session.commit()
        att_ids = [first_att.id, second_att.id]

    original_engine = jobs.ENGINES[0]
    jobs.ENGINES[0] = FakeEngine([("ORG", "Acme Analytics Srl"),
                                  ("FULLNAME", "Mario Rossi")])
    try:
        model_content, version, descriptors = anonymize_turn(
            turn_cid, "Che rapporto c'è con Mario Rossi?", att_ids)
    finally:
        jobs.ENGINES[0] = original_engine

    with SessionLocal() as session:
        atts = [session.get(Attachment, i) for i in att_ids]
        texts = [attachment_path(a).read_text(encoding="utf-8-sig") for a in atts]
        check("il file protetto PRIMA che la superficie fosse nota non resta in chiaro",
              "Acme Analytics" not in texts[0] and "[ORG_1]" in texts[0],
              ascii(texts[0]))
        check("secondo file redatto con lo stesso registro",
              "[ORG_1]" in texts[1] and "[FULLNAME_1]" in texts[1]
              and "Mario Rossi" not in texts[1], ascii(texts[1]))
        check("prompt anonimizzato nello stesso turno",
              model_content == "Che rapporto c'è con [FULLNAME_1]?",
              model_content)
        check("una sola mapping_version per tutto il turno",
              {a.mapping_version for a in atts} == {version},
              str(sorted(a.mapping_version for a in atts)))
        check("stato e filename neutro dopo l'invio",
              all(a.anonymization_status == "protected" for a in atts)
              and attachment_model_name(atts[0]) == "allegato_01.txt"
              and len(descriptors) == 2)
        check("nessuna fuga nel controllo di uscita",
              not known_surface_leaks(session, turn_cid, texts[0])
              and not known_surface_leaks(session, turn_cid, model_content))
        check("report senza il not_found degli altri file",
              "not_found" not in (atts[0].anonymization_report_json or ""))

    # --- categorie escluse per conversazione (pannello in toolbar) ----------
    # La scelta vale dai turni SUCCESSIVI e deve valere anche contro il
    # REGISTRO: un valore diventato noto quando la categoria era attiva
    # ricomparirebbe altrimenti come segnaposto per sempre (difesa
    # deterministica), e "da qui in poi in chiaro" non sarebbe vero.
    with SessionLocal() as session:
        excl_conv = Conversation(owner_id=1, anonymized=1)
        session.add(excl_conv)
        session.commit()
        excl_cid = excl_conv.id
        check("senza scelta la chat segue i default dell'admin",
              anon_options_view(session, excl_conv)["inherited"] is True)

    jobs.ENGINES[0] = FakeEngine([("FULLNAME", "Mario Rossi"), ("CITY", "Lodi")])
    try:
        before, _v, _d = anonymize_turn(excl_cid, "Mario Rossi vive a Lodi.", [])
        with SessionLocal() as session:
            conv = session.get(Conversation, excl_cid)
            set_anon_options(conv, ["CITY"])
            session.commit()
            view = anon_options_view(session, conv)
        after, _v, _d = anonymize_turn(excl_cid, "Mario Rossi torna a Lodi.", [])
    finally:
        jobs.ENGINES[0] = original_engine

    check("scelta della conversazione salvata e dichiarata",
          view["excluded_tags"] == ["CITY"] and view["inherited"] is False,
          json.dumps(view))
    check("prima dell'esclusione la categoria era coperta",
          before == "[FULLNAME_1] vive a [CITY_1].", before)
    check("categoria esclusa: in chiaro anche se era già nel registro",
          after == "[FULLNAME_1] torna a Lodi.", after)
    with SessionLocal() as session:
        check("il controllo di uscita rispetta l'esclusione",
              not known_surface_leaks(session, excl_cid, after,
                                      exclude=excluded_groups(["CITY"]))
              and known_surface_leaks(session, excl_cid, after),
              "senza esclusione la superficie nota risulterebbe una fuga")
        check("i segnaposto dei turni precedenti si decodificano ancora",
              conversation_mapping(session, excl_cid).get("[CITY_1]") == "Lodi")

    # Un guasto non lascia niente a metà: stato failed sul file colpevole,
    # nessun placeholder allocato, nessun file protetto scritto.
    with SessionLocal() as session:
        broken_conv = Conversation(owner_id=1, anonymized=1)
        session.add(broken_conv)
        session.flush()
        broken = make_att(session, broken_conv.id, "Sparito.txt", "x", 1)
        broken_cid, broken_id = broken_conv.id, broken.id
        session.commit()
    Path(_tmp.name, f"{broken_cid}_Sparito.txt").unlink()
    jobs.ENGINES[0] = FakeEngine([])
    try:
        anonymize_turn(broken_cid, "prova", [broken_id])
        check("turno rotto solleva TurnAnonymizationError", False)
    except TurnAnonymizationError as exc:
        check("turno rotto solleva TurnAnonymizationError",
              exc.attachment_id == broken_id, str(exc))
    finally:
        jobs.ENGINES[0] = original_engine
    with SessionLocal() as session:
        att = session.get(Attachment, broken_id)
        check("allegato colpevole marcato failed, senza protetto",
              att.anonymization_status == "failed" and not att.protected_path
              and session.query(ConversationEntity)
              .filter_by(conv_id=broken_cid).count() == 0)

    # --- fusione manuale: nota di equivalenza per il system prompt ----------
    # I contenuti già protetti conservano il segnaposto vecchio (niente
    # riscrittura retroattiva), quindi il modello LLM lo incontra ancora:
    # merge_notes produce il blocco che gli dice quali segnaposto sono la
    # stessa entità.
    from app.chat_anonymization import merge_entities, merge_notes
    from app.openrouter.chat import system_prompt

    with SessionLocal() as session:
        merge_conv = Conversation(owner_id=1, anonymized=1)
        session.add(merge_conv)
        session.commit()
        merge_cid = merge_conv.id
    analyze(merge_cid, "Il sig. Rossi", [("FULLNAME", "Rossi")])
    analyze(merge_cid, "Mario Rossi in persona", [("FULLNAME", "Mario Rossi")])
    with SessionLocal() as session:
        check("senza fusioni nessuna nota",
              merge_notes(session, merge_cid) is None)
        by_value = {e.canonical_value: e
                    for e in session.query(ConversationEntity)
                    .filter_by(conv_id=merge_cid)}
        merge_entities(session, session.get(Conversation, merge_cid),
                       by_value["Rossi"], by_value["Mario Rossi"])
        session.commit()
        notes = merge_notes(session, merge_cid)
    check("nota di equivalenza dopo la fusione",
          notes is not None and "- [FULLNAME_1] = [FULLNAME_2]" in notes,
          ascii(notes))
    check("la nota entra in coda al system prompt",
          system_prompt(anonymized=True, merge_block=notes).endswith(notes))
    prompt = system_prompt(anonymized=True)
    check("il prompt consente i placeholder nel codice ora ripristinato",
          "usa i segnaposto anche quando un dato appartiene a codice" in prompt
          and "mai tra backtick" not in prompt)
    merged = analyze(merge_cid, "Sentiamo Rossi.", [])
    check("dopo la fusione la superficie risolve sulla destinazione",
          merged["anonymized_text"] == "Sentiamo [FULLNAME_2].",
          merged["anonymized_text"])

    # --- for_text: variante dedotta vs forma attaccata dentro un URL noto ---
    # "acme analytics" (dedotta da "Acme Analytics Srl") si cerca anche come
    # "acmeanalytics": dentro `www.acmeanalytics.com` NON è un rischio se
    # l'URL è a sua volta una superficie nota (lo copre per intero, prima),
    # ed escluderla lasciava il nome in chiaro accanto a un URL coperto — e
    # il controllo di uscita, che legge il testo coperto, lo condannava.
    from app.chat_anonymization import ReplacementMapping, apply_known_surfaces
    seen = [("[ORG_1]", "Acme Analytics Srl"),
            ("[URL_1]", "https://www.acmeanalytics.com")]
    derived = [("[ORG_1]", "acme analytics")]
    full = ReplacementMapping(dict(seen), seen + derived, derived)
    text = "Acme Analytics Srl - https://www.acmeanalytics.com - Acme Analytics."
    m = full.for_text(text)
    check("forma attaccata dentro un URL noto: la variante resta cercabile",
          ("[ORG_1]", "acme analytics") in m.items())
    covered = apply_known_surfaces(text, m)
    check("copertura completa, URL intero e variante",
          covered == "[ORG_1] - [URL_1] - [ORG_1].", covered)
    check("le due letture coincidono (nessun falso allarme sul testo coperto)",
          ("[ORG_1]", "acme analytics") in full.for_text(covered).items())
    m2 = full.for_text("Seguici su @acmeanalytics. Acme Analytics dal 1999.")
    check("forma attaccata in un punto sconosciuto: variante esclusa come prima",
          ("[ORG_1]", "acme analytics") not in m2.items())

    print(f"\nTotale: {PASS} PASS, {FAIL} FAIL")
    db._engine.dispose()
    _tmp.cleanup()
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
