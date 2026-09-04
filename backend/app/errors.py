"""Errori user-facing con un CODICE stabile accanto al testo.

Il server non sa in che lingua sta guardando l'utente (la scelta vive nel
browser, e un job può fallire in un thread che non ha nessuna richiesta HTTP
sotto), quindi la regola è la stessa delle fasi dei job in `engine/progress.py`:
**il backend manda una chiave, il frontend scrive la frase**.

Qui però la chiave si aggiunge, non sostituisce: la risposta d'errore porta
sia `code` sia `detail`.

    {"detail": "File troppo grande (max 25 MB).",
     "code": "file_too_large", "params": {"max": 25}}

`detail` è sempre una frase italiana compiuta, non un codice, per due motivi:
chi chiama l'API con curl legge una frase invece di una chiave; e un errore
non ancora presente nei cataloghi di traduzione esce comunque leggibile,
perché il frontend usa `detail` come `defaultValue`. Ne segue che un codice
nuovo è utile da subito e traducibile quando si vuole: le due cose non si
aspettano a vicenda.

`params` sono i valori che la frase interpola (il limite in MB, il nome del
foglio): devono restare NUMERI e NOMI, mai pezzi di frase già composti, o la
traduzione inglese si ritroverebbe dentro una parola italiana.

Convenzione dei codici: `oggetto_problema` in snake_case, stabile nel tempo —
è la chiave con cui il frontend lo cerca in `common.json` sotto `error.`.
Rinominarne uno vuol dire toccare i due cataloghi.
"""

from fastapi import HTTPException


class ApiError(HTTPException):
    """HTTPException con codice. Drop-in per HTTPException: i due argomenti
    posizionali di quella (status, testo) restano ai capi, il codice sta in
    mezzo."""

    def __init__(self, status_code, code, detail, **params):
        super().__init__(status_code=status_code, detail=detail)
        self.code = code
        self.params = params


def from_internal(exc, default_status=422):
    """Riavvolge in ApiError le eccezioni user-facing degli strati sotto
    (ProjectFileError, StagedEditError, e i *Error degli engine).

    Sta al posto di `raise HTTPException(e.status, str(e))`: porta avanti il
    codice dell'eccezione quando c'è, e quando non c'è produce un errore
    col solo `detail`."""
    status = getattr(exc, "status", None) or default_status
    return ApiError(status, getattr(exc, "code", None), str(exc),
                    **(getattr(exc, "params", None) or {}))
