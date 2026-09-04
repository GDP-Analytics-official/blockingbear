"""
Motore di anonimizzazione: il "cuore" di rizzo-pii (https://github.com/Rizzo-AI-Academy/rizzo-pii,
MIT, (c) 2026 Simone Rizzo — Rizzo AI Academy) estratto dall'app Flask e portato
avanti qui. Il testo della licenza MIT sta in fondo al NOTICE alla radice del
repository: va tenuto lì finché queste porzioni restano nel progetto.

- detectors.py   derivato da src/app/detectors.py (rete regex + checksum, standalone),
                 poi esteso in locale (checksum EU_VAT, post-check PIVA/IBAN)
- pdf_export.py  da src/app/pdf_export.py (redazione vera del PDF, standalone) con FIX
                 LOCALI da preservare: (1) _clamp_line_bleed — nei PDF a interlinea
                 stretta i bbox di righe adiacenti si sovrappongono e apply_redactions
                 cancellerebbe glifi delle righe accanto; (2) _draw_label + report["boxes"]
                 — le etichette dei placeholder sono disegnate DOPO apply_redactions
                 (MuPDF le scarta in silenzio se non ci stanno) e i box interattivi
                 dell'anteprima derivano dai rettangoli redatti, non dal testo disegnato;
                 (3) strip del TESTO FANTASMA in testa a redact_pdf (vedi pdf_ghost) —
                 senza, la trascrizione invisibile delle scansioni cercabili resta nel
                 file anonimizzato ed è estraibile con un copia-incolla.
                 Nessuno dei tre esiste a monte: una copia verbatim
                 dell'upstream li perde.
- pdf_ghost.py   riconoscimento e rimozione del testo FANTASMA dei PDF: la trascrizione
                 OCR invisibile (render mode 3) che le scansioni "cercabili" portano
                 sopra i propri pixel. Senza, la stessa frase viene analizzata due volte
                 (una dai pixel, una dalla trascrizione) con letture diverse, e i
                 placeholder si impilano. Codice locale, non c'è upstream.
- core.py        la logica di analisi di src/app/app.py (chunking, inferenza mmBERT,
                 fusione modello+regex, placeholder reversibili [FULLNAME_1], ...)
                 adattata a classe con caricamento lazy
- pdf.py         orchestrazione per i PDF: analisi -> redazione gialla -> box dei
                 placeholder per l'evidenziazione nel frontend

Nessuno dei due file è più allineato all'upstream riga per riga: sono
entrambi cresciuti a circa il doppio delle righe originali, detectors.py per i
validatori aggiunti e pdf_export.py per i tre fix qui sopra e per il matching
char-preciso condiviso con gli altri formati. La maggioranza delle righe
upstream però è ancora lì, ed è il motivo per cui l'avviso MIT deve restare
nel NOTICE e in testa ai due file.


CONTRATTO COMUNE DEI REDATTORI
------------------------------
Vale per i cinque redattori di formato — `redact_pdf` (pdf_export.py),
`redact_docx`, `redact_pptx`, `redact_xlsx`, `redact_txt` — ed è descritto qui
una volta sola invece che in ogni modulo. image_ocr.py copre il layer PIXEL con
ingressi propri, ma le stesse regole su colori e report.

  Firma       `redact_<fmt>(bytes, mapping, ...) -> (bytes, report)`, dove
              `mapping` è {"[FULLNAME_1]": "Mario Rossi", ...} prodotto da
              `core.PiiEngine.analyze`. I redattori NON chiamano il modello:
              lavorano su bytes + dizionario, quindi sono testabili senza
              torch/transformers.

  Ri-redazione
              `rebuild_<fmt>` NON è l'inverso: è la redazione con una mappa
              GIÀ data, il cuore condiviso tra la prima anonimizzazione e ogni
              modifica successiva al registro (de-anonimizzare un segnaposto,
              aggiungere un tag custom). Si riparte SEMPRE dai byte originali,
              mai dalla copia protetta.

  Matching    CHAR-PRECISO e ancorato ai confini di parola: le regex sono
              quelle di `pdf_export._value_pattern` (spazi flessibili,
              sillabazione a fine riga, guardia sui bordi numerici), riusate
              da tutti i formati. Si redige il token intero, ovunque compaia:
              "DE" non viene mai redatto dentro "CORDELLA".

  Report      Le chiavi canoniche sono documentate in `pdf_export.redact_pdf`
              (occurrences, by_placeholder, not_found, skipped, residual, più
              le voci specifiche di ogni formato). Chi aggiunge un formato
              usa le stesse chiavi.

  Residui     `report["residual"]` elenca i placeholder il cui valore è
              ANCORA leggibile rileggendo l'OUTPUT. **Deve essere vuota**: è
              la rete di sicurezza finale di ogni pipeline, e l'interfaccia
              avvisa se non lo è. `report["skipped"]` sono invece i valori
              deliberatamente lasciati in chiaro (troppo corti o ambigui): non
              sono un errore, ma vanno dichiarati all'utente.

  Colori      Due soli sfondi in tutta l'applicazione: GIALLO = redatto
              (reversibile, il valore vive nel registro), NERO = sigillato
              (area rimossa davvero, senza ritorno). Ogni formato usa il
              proprio meccanismo nativo per il giallo — w:highlight nel docx,
              a:highlight nel pptx, patternFill di cella nell'xlsx, box
              disegnato nel PDF e nei pixel — ma il colore e il significato
              non cambiano.

  OOXML       docx/pptx/xlsx girano su SOLA stdlib (zipfile + xml.etree). Le
              trappole di riserializzazione — prefissi ns0:, dichiarazioni
              xmlns perse contro mc:Ignorable, namespace di default conteso
              tra .rels e part — sono risolte una volta in
              `docx._serialize_part` / `docx._part_namespaces` e riusate dagli
              altri due.

  Inverso     Il percorso di RIPRISTINO (segnaposto -> valori reali, sui file
              che il modello produce) sta in `pdf_export.restore_pdf` per il
              PDF e in `chat_anonymization.restore_artifact` per tutto il
              resto. Stessa meccanica al contrario, ma senza caselle colorate:
              l'utente riceve un documento da spedire, non una redazione.
"""
from .core import PiiEngine  # noqa: F401
