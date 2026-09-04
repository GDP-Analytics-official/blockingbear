"""Cancellazione cooperativa e avanzamento a fasi dei job di elaborazione.

I worker (jobs.py) sono thread: non si possono uccidere, quindi l'interruzione
è COOPERATIVA — un threading.Event per job e checkpoint (`check()`) sparsi nei
punti costosi degli engine; quando l'utente annulla, il primo checkpoint
solleva JobCanceled e il worker marca il job `canceled`.

L'avanzamento è A FASI ETICHETTATE ("fase x di y"), non una percentuale
unica: le fasi si dichiarano appena la strada è nota (`phases([...])`, per
l'xlsx dopo aver capito se il foglio è tabellare) e dentro la fase corrente
`tick(done, total)` riporta il progresso REALE dove esiste una granularità
(i chunk del modello, i fogli in redazione).

Tutti gli engine ricevono un JobControl OPZIONALE: le route sincrone
(ri-redazione, anonimizzazione di testo incollato) non passano nulla e ogni
chiamata diventa no-op (`ctl = ctl or NULL`).

Il nome di una fase è una CHIAVE, non un testo: il server non sa in che
lingua sta guardando l'utente, e la card di avanzamento la scrive il frontend
(jobs.jsx, catalogo i18n/locales/*/jobs.json). PHASES qui sotto è l'elenco
completo: aggiungerne una vuole una riga anche nei due cataloghi, altrimenti
la card mostra la chiave nuda.
"""

PHASES = (
    "image_ocr",         # lettura OCR delle immagini, prima di tutto il resto
    "analysis",          # il modello cerca le entità nel testo
    "analysis_sample",   # xlsx tabellare: il modello legge un campione di righe
    "analysis_columns",  # xlsx tabellare: secondo passo, colonna per colonna
    "analysis_values",   # xlsx: ri-anonimizzazione manuale di una colonna
    "redaction",         # scrittura del file redatto
    "preview",           # conversione in PDF per l'anteprima
)


class JobCanceled(Exception):
    """Interruzione richiesta dall'utente: non è un errore di elaborazione."""


class JobControl:
    """Ponte tra il worker e gli engine: cancel flag + callback di progresso.

    on_progress riceve snapshot {phase, phase_index, phase_total, done, total}
    (indici 1-based; done/total None = fase appena iniziata, granularità
    ignota). Il throttling degli snapshot è compito del chiamante (jobs.py):
    qui si emette a ogni tick."""

    def __init__(self, cancel_event=None, on_progress=None):
        self._cancel = cancel_event
        self._emit = on_progress
        self._names = []
        self._phase = None

    def check(self):
        """Checkpoint: da chiamare nei loop costosi (batch del modello,
        part OOXML, item di sharedStrings)."""
        if self._cancel is not None and self._cancel.is_set():
            raise JobCanceled("Elaborazione annullata dall'utente.")

    def phases(self, names):
        """Dichiara la sequenza di fasi ("fase x di y") appena è nota."""
        self._names = list(names)

    def phase(self, name):
        """Entra in una fase. Una fase non dichiarata si mostra senza x/y
        (es. la conversione d'ingresso .doc -> .docx, che precede la scelta
        della pipeline)."""
        self.check()
        self._phase = name
        self._send(None, None)

    def tick(self, done, total):
        """Avanzamento dentro la fase corrente (unità reali: chunk, fogli)."""
        self.check()
        self._send(done, total)

    def _send(self, done, total):
        if self._emit is None or self._phase is None:
            return
        try:
            idx = self._names.index(self._phase) + 1
        except ValueError:
            idx = None
        self._emit({"phase": self._phase,
                    "phase_index": idx,
                    "phase_total": len(self._names) or None,
                    "done": done, "total": total})


NULL = JobControl()     # no-op condiviso: `ctl = ctl or NULL` negli engine
