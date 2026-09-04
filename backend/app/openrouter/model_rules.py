"""Modello predefinito della chat: si salva una REGOLA, non un id.

Due livelli, della stessa forma:
  - la regola MADRE, che l'amministratore fissa per l'installazione;
  - la regola PERSONALE di ogni utente (admin compreso), che può valere
    «default» — cioè seguire la madre.

Forme ammesse (JSON):
    {"kind": "fixed",  "model": "anthropic/claude-opus-5",
     "options": {"reasoning": {...}, "params": {...}}}      # facoltative
    {"kind": "guided", "use": "documents", "budget": "balanced",
     "images": true}

Le `options` esistono solo sul modello fisso (hanno senso solo conoscendo il
modello) e sono le stesse del pannello opzioni della chat, ricerca web
esclusa: la nuova conversazione nasce con quelle, poi ognuno le cambia.

La regola GUIDATA memorizza le tre risposte della mini procedura («per cosa
lo usate», «quanto conta il costo», «caricate immagini») e non il modello a
cui portano: quello si calcola sul catalogo del momento, così fra sei mesi
le stesse risposte danno i modelli nuovi senza che nessuno tocchi le
impostazioni. Il prezzo da pagare è che la scelta va RISOLTA a ogni uso — e
può non risolversi (catalogo irraggiungibile, valutazioni assenti): in quel
caso vale il ripiego di «nessun default», la chat nasce senza modello e lo
sceglie l'utente.

`None` = nessuna scelta: sulla regola personale significa «segui la madre»,
sulla madre significa «nessun default», e la chat nasce senza modello.

Niente liste hardcoded, come nel resto del catalogo: la regola guidata usa
i punteggi indipendenti (Artificial Analysis) che OpenRouter espone nei
metadati e soglie relative al catalogo (percentili), mai nomi di modelli né
cifre in dollari.
"""

import json

KINDS = ("fixed", "guided")
# le risposte ammesse alle tre domande della procedura guidata
USES = ("writing", "documents", "coding", "mixed")
BUDGETS = ("best", "balanced", "economy")

# quale dei tre indici misura ogni uso; "mixed" = media dei tre
_USE_INDEX = {"writing": "intelligence", "documents": "agentic",
              "coding": "coding"}
# «equilibrio» ed «economico» sono lo stesso criterio con soglie diverse: fra
# i modelli che hanno almeno questa quota del punteggio migliore, il più
# economico. Le soglie sono relative al catalogo del giorno, non cifre.
# Alternative provate sul catalogo reale e scartate: il quartile alto di
# punteggio (su 70 modelli la soglia scende troppo e «equilibrio» coincide
# con «economico») e il quartile basso di prezzo (fra i modelli con visione
# il quarto più economico è fatto solo di modelli scarsi: sceglieva un 24/100).
_NEAR_BEST = {"balanced": 0.9, "economy": 0.8}


def clean_rule(raw):
    """Valida una regola. Ritorna la forma normalizzata, oppure None per
    «nessuna scelta». ValueError con messaggio già user-facing."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("Modello predefinito: regola non valida.")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind in ("", "default"):
        return None
    if kind not in KINDS:
        raise ValueError(f"Modello predefinito: criterio sconosciuto ({kind}).")
    if kind == "fixed":
        model = str(raw.get("model") or "").strip()
        if not model:
            raise ValueError("Scegli il modello da usare come predefinito.")
        rule = {"kind": "fixed", "model": model}
        options = clean_options(raw.get("options"))
        if options:
            rule["options"] = options
        return rule
    use = str(raw.get("use") or "").strip().lower()
    budget = str(raw.get("budget") or "").strip().lower()
    images = raw.get("images")
    if use not in USES or budget not in BUDGETS or not isinstance(images, bool):
        raise ValueError("Rispondi alle tre domande per scegliere il modello.")
    return {"kind": "guided", "use": use, "budget": budget, "images": images}


_OPTION_KEYS = ("reasoning", "params")


def clean_options(raw):
    """Le opzioni iniziali della chat legate a un modello fisso: solo
    `reasoning` e `params` (dizionari), niente ricerca web né deroghe privacy,
    che sono scelte della singola conversazione. La validazione fine sui
    parametri la fa catalog.sanitize_options al momento dell'invio, sul
    catalogo del giorno. Ritorna {} se non c'è niente."""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("Modello predefinito: opzioni non valide.")
    out = {}
    for key in _OPTION_KEYS:
        value = raw.get(key)
        if value:
            if not isinstance(value, dict):
                raise ValueError("Modello predefinito: opzioni non valide.")
            out[key] = value
    return out


def load(raw):
    """Testo JSON (colonna/impostazione) -> regola, o None. Un valore corrotto
    a mano vale None: mai impedire di aprire una chat per un default illeggibile."""
    if not raw:
        return None
    try:
        return clean_rule(json.loads(raw))
    except (ValueError, TypeError):
        return None


def dump(rule):
    """Regola -> testo JSON da salvare ("" per «nessuna scelta»)."""
    return json.dumps(rule, ensure_ascii=False) if rule else ""


def effective(user, mother):
    """La regola che vale davvero per un utente: la sua, oppure quella
    dell'installazione se sta su «default». Sta qui e non nelle route perché
    la precedenza la leggono in due (la creazione della chat e la pagina
    impostazioni, che mostra il risultato) e devono dire la stessa cosa."""
    return load(user.chat_model_rule) or mother


def _price(model):
    """Costo per token: input + output. Un modello senza prezzi noti non
    partecipa ai confronti (None)."""
    p = model.get("pricing") or {}
    try:
        return float(p["prompt"]) + float(p["completion"])
    except (KeyError, TypeError, ValueError):
        return None


def _usable(model):
    """Un modello che ha senso scegliere DA SOLO, senza che nessuno guardi:
    niente varianti di routing (`:free`, `:nitro`… stanno sotto il modello
    base), niente modelli in scadenza, e strumenti supportati — senza `tools`
    l'analisi dei file in chat non funziona, e un default che rompe l'analisi
    file è peggio di nessun default."""
    return (model.get("variant_of") is None and model.get("tools")
            and not model.get("expiration_date"))


def _score(model, use):
    """Il punteggio (0-100) del modello per l'uso scelto, o None se non è
    stato valutato. Per «un po' di tutto» servono tutti e tre gli indici: una
    media su due soli favorirebbe chi manca proprio dove è debole."""
    bench = model.get("benchmarks") or {}
    if use == "mixed":
        values = [bench.get(k) for k in ("intelligence", "coding", "agentic")]
        if any(v is None for v in values):
            return None
        return sum(values) / 3
    return bench.get(_USE_INDEX[use])


def _guided(rule, models, allow_non_zdr):
    """La scelta della procedura guidata sul catalogo dato.

    1. Pool: i modelli usabili (vedi _usable) con prezzo e punteggio noti;
       con immagini in input se l'utente carica scansioni; e, se
       l'installazione vieta i provider senza Zero Data Retention, solo
       quelli con almeno un provider conforme (un default che OpenRouter
       rifiuta con 503 è il fallimento più visibile per chi non è tecnico).
    2. Budget:
       - best      il punteggio più alto (a parità, il più economico);
       - balanced  fra chi ha almeno il 90% del punteggio migliore, il più
                   economico (a parità, il più bravo);
       - economy   idem con l'80%: costa una frazione, resta un modello serio.

    Ritorna il dizionario di `explain` (modello "" se il pool è vuoto)."""
    pool = []
    for m in models:
        if not _usable(m):
            continue
        if rule["images"] and "image" not in (m.get("input_modalities") or []):
            continue
        if not allow_non_zdr and m.get("zdr_providers") == []:
            continue
        price, score = _price(m), _score(m, rule["use"])
        if price is None or score is None:
            continue
        pool.append((m, score, price))
    if not pool:
        return {"model": "", "detail": {"pool": 0}}

    best_score = max(s for _, s, _ in pool)
    if rule["budget"] == "best":
        pick = max(pool, key=lambda t: (t[1], -t[2]))
    else:
        near = [t for t in pool
                if t[1] >= best_score * _NEAR_BEST[rule["budget"]]]
        pick = min(near, key=lambda t: (t[2], -t[1]))
    model, score, _ = pick
    return {"model": model["id"],
            "detail": {"pool": len(pool), "score": round(score, 1),
                       "best_score": round(best_score, 1)}}


def explain(rule, models, allow_non_zdr=True):
    """A quale modello porta la regola sul catalogo dato, e perché.

    {"model": id o "", "detail": None} per il modello fisso (si controlla
    solo che esista ancora); per la regola guidata `detail` porta quanti
    modelli erano in gara e il punteggio dello scelto rispetto al migliore —
    è quello che la UI mostra all'amministratore prima di salvare.
    `allow_non_zdr` è l'impostazione dell'installazione (chat_allow_non_zdr):
    conta solo per la regola guidata, una scelta esplicita non si discute."""
    if not rule:
        return {"model": "", "detail": None}
    if rule["kind"] == "fixed":
        found = any(m["id"] == rule["model"] for m in models)
        return {"model": rule["model"] if found else "", "detail": None}
    return _guided(rule, models, allow_non_zdr)


def resolve(rule, models, allow_non_zdr=True):
    """L'id del modello scelto dalla regola sul catalogo dato, o "" se la
    regola non si risolve (modello ritirato, nessun modello valutato adatto).
    Il chiamante tratta "" come «nessun default»."""
    return explain(rule, models, allow_non_zdr)["model"]
