"""
Credenziali: USERNAME, PASSWORD, SECRET. Rete regex di CONTESTO, senza modello.

Una password non ha un formato: la si riconosce dalla frase che la introduce
("la password è ...", "pwd: ...", "user mrossi / pass Abc123", "DB_PASSWORD=").
I segreti strutturati (chiavi API con prefisso noto, JWT, blocchi PEM, header
Authorization, URI con credenziali) hanno invece un formato e si prendono da
soli. Ogni regola cattura SOLO il valore: la parola chiave resta in chiaro.

Come `detectors.py`, il modulo lavora su stringhe e non importa torch/fitz:
`core.detect_regex` ne consuma `detect_credentials`, e la fusione con il
modello avviene in `core._merge`. Le tre label stanno in EXACT_SPAN_LABELS:
una password è case-sensitive e può finire con un simbolo, quindi la fusione
non deve né allargare la span ai confini di parola né toglierle la
punteggiatura ai bordi, e il confronto fra superfici deve essere esatto.

Filosofia dei falsi positivi (da gitleaks): una parola chiave da sola non basta.
Servono un separatore, un valore che non sia una parola di servizio ("password:
obbligatoria", "utente finale") né un segnaposto ("${DB_PASSWORD}", "********",
"<password>"), e nei casi meno ancorati una forma da segreto (cifre, simboli,
maiuscole miste, entropia). Il rilevamento vive nel contesto della chat, dove
il costo di un falso positivo è basso (l'utente può escludere il tag) e quello
di una credenziale in chiaro verso l'esterno è alto: nel dubbio si maschera.
"""

import math
import re

from . import lexicon as _lx

CREDENTIAL_LABELS = frozenset({"PASSWORD", "USERNAME", "SECRET"})

_LOWER_UPPER = re.compile(r"[a-z].*[A-Z]|[A-Z].*[a-z]")
def _is_name_word(v):
    """Una parola con la sola iniziale maiuscola è un nome (Mario, Müller, García)."""
    return len(v) >= 2 and v[0].isupper() and v[1:].isalpha() and v[1:].islower()


# ----------------------------------------------------------------------------
# Valori di servizio: parole che seguono spesso la parola chiave ma non sono
# credenziali ("la password è scaduta", "utente finale", "token: obbligatorio").
# ----------------------------------------------------------------------------
_STOP_VALUES = frozenset("""
sbagliata sbagliato errata errato corretta corretto giusta giusto scaduta
scaduto expired wrong incorrect correct right invalid valid valida valido
non not stata stato sempre ancora obbligatoria obbligatorio necessaria
necessario richiesta richiesto required mandatory optional opzionale
opzionali facoltativa facoltativo sicura sicuro insicura debole forte lunga
corta troppo molto poco uguale uguali diversa diverso diversi stessa stesso
quella quello questa questo queste questi la il lo le gli i un una uno l
nuova nuovo vecchia vecchio cambiata cambiato modificata modificato
resettata resettato reimpostata reimpostato dimenticata dimenticato persa
perso smarrita smarrito bloccata bloccato bloccati disabilitata disabilitato
abilitata abilitato attiva attivo attivi disattivato disattivata salvata
salvato memorizzata memorizzato visibile visibili nascosta nascosto criptata
criptato cifrata cifrato hashata inserita inserito inseriti ricordata
leggibile leggibili temporanea temporaneo provvisoria provvisorio iniziale
casuale random generata generato composta composto formata formato segreta
segreto personale univoca univoco unica unico minimo massimo caratteri numeri
lettere simboli maiuscole minuscole almeno reset changed forgotten lost
blocked locked disabled enabled saved stored visible hidden encrypted hashed
entered remembered temporary generated secret personal unique minimum maximum
characters chars numbers digits letters symbols uppercase lowercase least too
very same different empty blank missing needed strong weak long short good
bad ok okay fine set unset none null nil undefined true false yes no on off si
sì vuota vuoto vuote nessuna nessuno niente nulla nothing string str text int
integer bool boolean value valore valori varchar char field campo campi column
colonna type tipo name nome description descrizione example esempio
placeholder default predefinita predefinito standard password passwords
username usernames user users utente utenti login logins email mail token
tokens secret secrets key keys chiave chiavi codice code id nome names
vedi see come as sopra sotto above below allegato attached inviata inviato
sent via per tramite chiedi chiedere ask the a an your tua tuo mia mio my our
nostra nostro suo sua his her their loro di del della dello dei degli delle
da in su con and or e o ma but se if che that this these those who which
when where why how what quale quali cosa dove quando perché is was are were
has have had can could must should will would does did do be been being
ha hanno può possono deve devono viene vengono sarà saranno era erano sono
già poi anche ancora solo soltanto solamente qui here là there sopra
finale medio tipo base registrato registrati registrata anonimo anonima
generico generica esterno esterni interno interni esistente corrente
attuale loggato autenticato connesso singolo multiplo premium business
experience interface agent agents guide manual story stories group groups
table list ids data input output error errors session sessions profile
settings role roles types management manager model service count info
defined friendly facing generated provided specified space
n/a na tbd todo xxx xxxx yyy zzz test testing prova demo sample dummy
changeme change_me replace_me redacted hidden omitted omessa omesso
inserisci inserire insert enter digita digitare choose scegli
aziendali aziendale personali solite stesse nuove vecchie corrette giuste
temporanee fornite indicate ricevute inviate seguenti precedenti attuali
condivise salvate memorizzate sbagliate errate valide scadute provvisorie
iniziali predefinite amministrative obbligatori obbligatorie richiesti
necessari validi corretti sbagliati errati vuoti mancanti mancante identici
identiche uguali diversi differenti avviene avvengono funziona funzionano
serve servono basta bastano permette consente
voglio vorrei posso devo dovrei chiedo preferisco riesco cerco provo vedo apro
clicco inserisco accedo entro premo seleziono scelgo digito scrivo ricevo
ottengo vado torno resto rimango faccio dico penso credo vuole riesce cerca
prova vede apre clicca inserisce accede preme seleziona sceglie riceve
ottiene va torna resta rimane fa dice pensa crede
tipico specifico normale semplice comune avanzato esperto principiante
inattivo sbloccato collegato disconnesso autorizzato successivo cancellato
eliminato creato aggiornato selezionato indicato specificato interessato
coinvolto responsabile gestore proprietario titolare amministratore
wants needs clicks enters opens sees gets logs selects chooses receives
journey persona research acceptance
""".split()) | _lx.STOP_VALUES          # fr/de/es/nl: lexicon.py

# Segnaposto e riferimenti di codice: non sono un valore, sono il posto dove
# un valore andrà messo ("${DB_PASSWORD}", "<password>", "os.environ[...]").
_PLACEHOLDER_RES = [
    re.compile(r"^\[[A-Za-z0-9_]+_\d+\]$"),              # già anonimizzato
    re.compile(r"^<[^<>]*>$"),                           # <password>, <YOUR_KEY>
    re.compile(r"^\[[^\[\]]*\]$"),                       # [password]
    re.compile(r"^\$\{?[A-Za-z_][\w.:\-]*\}?$"),         # $PW, ${DB_PASSWORD}
    re.compile(r"^%[\w.]+%$"),                           # %PASSWORD%
    re.compile(r"^\{\{.*\}\}$|^\{[\w.\-]+\}$"),          # {{ pw }}, {password}
    re.compile(r"^__[A-Za-z0-9_]+__$"),                  # __PASSWORD__
    re.compile(r"^(?:[*•·xX#.\-_?●○◦]+|\.{3,}|…+)$"),      # ********, xxxx, ...
    re.compile(r"^[A-Z]+(?:_[A-Z0-9]+)+$"),              # YOUR_PASSWORD_HERE
    re.compile(r"^[A-Za-z_]\w*(?:\.\w+)*[\[(].*$"),           # f(x), env["X"], a.b.c()
    re.compile(r"^(?:os\.|process\.|env\.|System\.|config\.|settings\.|self\.|this\."
               r"|request\.|req\.|params\.|args\.|opts\.|options\.|vars\.|var\."
               r"|secrets\.|getenv|env\(|ENV\[|Environment\.)"),
    re.compile(r"^(?:string|str|text|int|integer|bool|boolean|float|varchar|char"
               r"|required|optional|nullable|unique|primary)(?:\(\d+\))?$", re.I),
    re.compile(r"^r?[\"']{2}$"),                         # stringa vuota
]

# a parole intere: "here" dentro "vsphere.local" o "qui" dentro "requiem" non
# sono parole guida
_PLACEHOLDER_WORDS = re.compile(
    r"(?<![a-z])(?:your|tua|tuo|tuoi|vostr[aeio]|inserisci|inserire|here|qui|example|esempio"
    r"|placeholder|dummy|sample|redacted|hidden|omitted|omess[aeio]|changeme|change_me"
    r"|replace|sostituisci|xxx+|password_here|secret_here|fill|todo|tbd"
    r"|" + _lx.alt(_lx.PLACEHOLDER_WORDS) + r")(?![a-z])", re.I)


def _entropy(s):
    """Entropia di Shannon in bit/carattere."""
    if not s:
        return 0.0
    freq = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in freq.values())


def _is_stop(value):
    v = value.strip().strip("()[]{}\"'«»“”‘’`").casefold()
    if not v:
        return True
    if v in _STOP_VALUES:
        return True
    # "campo obbligatorio", "vedi sopra": due parole di servizio
    parts = v.split()
    return bool(parts) and all(p in _STOP_VALUES for p in parts)


def _is_placeholder(value, quoted=False):
    v = value.strip()
    if not v:
        return True
    for rx in _PLACEHOLDER_RES:
        if rx.match(v):
            return True
    # "your_password", "<inserisci qui>", "la-tua-password": parole guida
    # dentro un valore corto e senza cifre
    if len(v) <= 32 and not re.search(r"\d", v) and _PLACEHOLDER_WORDS.search(v):
        return True
    return False


def _secret_shape(value):
    """Ha l'aria di un segreto: cifre, simboli o maiuscole miste."""
    v = value.strip()
    if re.search(r"\d", v) and re.search(r"[A-Za-z]", v):
        return True
    if re.search(r"[^\w\s]", v):
        return True
    if _LOWER_UPPER.search(v) and not _is_name_word(v):
        return True
    return False


def _identifier_shape(value):
    """Ha l'aria di uno username: mrossi, m.rossi, mrossi92, DOM\\mrossi,
    mario@dominio. Una parola con la sola iniziale maiuscola è un nome."""
    v = value.strip()
    if len(v) < 2 or len(v) > 64 or " " in v:
        return False
    if _is_name_word(v):
        return False
    return bool(re.match(r"^[\w.@+\-\\/]+$", v))


def _bad_value(value, quoted=False):
    return _is_stop(value) or _is_placeholder(value, quoted)


# ----------------------------------------------------------------------------
# Estrazione del valore dopo il separatore.
# ----------------------------------------------------------------------------
_QUOTES = {'"': '"', "'": "'", "`": "`", "«": "»", "“": "”", "‘": "’", "„": "“"}
_TRAIL_PUNCT = ".,;:)]}»”’\"'`?"
_MAX_VALUE = 160


def _read_value(text, pos):
    """(start, end, quoted) del valore che comincia a `pos`, o None.

    Tra virgolette il valore può contenere spazi ("la password è 'ciao mondo 1'")
    e finisce alla virgoletta di chiusura; senza virgolette finisce allo
    spazio, ma anche a ',' e ';' quando separano un altro campo
    (Password=abc;Server=x) e alla punteggiatura che chiude la frase.
    """
    n = len(text)
    while pos < n and text[pos] in " \t":
        pos += 1
    if pos >= n or text[pos] in "\r\n":
        return None
    ch = text[pos]
    if ch in _QUOTES:
        close = _QUOTES[ch]
        end = text.find(close, pos + 1)
        nl = text.find("\n", pos + 1)
        if end != -1 and (nl == -1 or end < nl) and end - pos - 1 <= _MAX_VALUE:
            if end > pos + 1:
                return pos + 1, end, True
            return None                     # stringa vuota
        pos += 1                            # virgoletta orfana: si prosegue
        if pos >= n or text[pos] in " \t\r\n":
            return None
    m = re.compile(r"[^\s]+").match(text, pos)
    if not m:
        return None
    raw = m.group(0)[:_MAX_VALUE]
    # fine di un campo: ";Server=", ", utente:" oppure ',' / ';' seguiti da spazio
    for sep in (";", ",", "&"):
        i = raw.find(sep)
        while i != -1:
            rest = raw[i + 1:]
            after = text[pos + i + 1:pos + i + 2]
            if (not rest or (sep != "&" and after in (" ", "\t", "\r", "\n", ""))
                    or re.match(r"[\w\s.\-]{1,40}[=:]", rest)):
                raw = raw[:i]
                break
            i = raw.find(sep, i + 1)
    # markup: <password>abc</password>, valore seguito da un tag
    i = raw.find("</")
    if i != -1:
        raw = raw[:i]
    i = raw.find("<")
    if i > 0 and re.match(r"<[A-Za-z/]", raw[i:]):
        raw = raw[:i]
    # punteggiatura che chiude la frase, non il valore
    while raw and raw[-1] in _TRAIL_PUNCT:
        if raw[-1] == ")" and "(" in raw[:-1]:
            break
        if raw[-1] == "]" and "[" in raw[:-1]:
            break
        if raw[-1] == "}" and "{" in raw[:-1]:
            break
        raw = raw[:-1]
    if not raw:
        return None
    return pos, pos + len(raw), False


# ----------------------------------------------------------------------------
# Parole chiave, per classe. Ordine: le forme composte PRIMA di quelle brevi,
# così "user name" non si ferma a "user" e "api key" non si ferma a "key".
# ----------------------------------------------------------------------------
_KW_PASSWORD = (
    r"passwords?|passwd|passw|pass(?:word)?phrases?|passphrases?|passcodes?|pwd|psw|pswd"
    r"|parola[ _\-]?(?:chiave|d[’']ordine|segreta|di[ _]accesso)"
    r"|chiave[ _\-]d[i’'][ _]?accesso|codice[ _\-]d[i’'][ _]?accesso|access[ _\-]?code"
    r"|codice[ _\-](?:segreto|di[ _]sblocco|di[ _]conferma)"
    r"|mot[ _\-]de[ _\-]passe|contrase[ñn]a|passwort|kennwort|senha|wachtwoord|has[łl]o"
    r"|pw|pass"
    + "|" + _lx.alt(*_lx.KW_PASSWORD.values())          # fr/de/es/nl: lexicon.py
)
# PIN/OTP/CVV: valori numerici corti, forma controllata a parte.
_KW_PIN = (
    r"codice[ _\-](?:pin|otp|di[ _]verifica|di[ _]sicurezza|monouso|temporaneo|2fa|mfa)"
    r"|token[ _\-]di[ _](?:verifica|sicurezza|conferma)|codice[ _\-]di[ _]conferma"
    r"|one[ _\-]?time[ _\-]?(?:code|password|pin)|verification[ _\-]?code"
    r"|security[ _\-]?code|2fa[ _\-]?code|mfa[ _\-]?code|totp|otp|pin|cvv2?|cvc2?"
    + "|" + _lx.alt(*_lx.KW_PIN.values())
)
_KW_USERNAME = (
    r"user[ _\-]?names?|nome[ _\-]?utente|nomi[ _\-]?utente|login[ _\-]?(?:name|id)"
    r"|user[ _\-]?ids?|userid|account[ _\-]?name|screen[ _\-]?name|sign[ _\-]?in[ _\-]?name"
    r"|id[ _\-]utente|identificativo[ _\-]utente|codice[ _\-]utente|nome[ _\-]account"
    r"|nickname|nick|handle|utenza|utente|usr|uid|user|login|logon"
    + "|" + _lx.alt(*_lx.KW_USERNAME.values())
)
# Segreti "forti": la parola basta, il valore deve solo essere lungo un minimo.
_KW_SECRET_STRONG = (
    r"api[ _\-]?keys?|api[ _\-]?secrets?|apikey|secret[ _\-]?keys?|secretkey"
    r"|client[ _\-]?secrets?|app[ _\-]?secrets?|consumer[ _\-]?secrets?|webhook[ _\-]?secrets?"
    r"|signing[ _\-]?secrets?|shared[ _\-]?secrets?|encryption[ _\-]?keys?|master[ _\-]?keys?"
    r"|service[ _\-]?keys?|account[ _\-]?keys?|licen[cs]e[ _\-]?keys?|product[ _\-]?keys?"
    r"|activation[ _\-]?(?:keys?|codes?)|serial[ _\-]?keys?|secret[ _\-]?access[ _\-]?keys?"
    r"|access[ _\-]?keys?(?:[ _\-]?id)?|private[ _\-]?keys?|privkey|app[ _\-]?keys?|appkey"
    r"|consumer[ _\-]?keys?|access[ _\-]?tokens?|refresh[ _\-]?tokens?|id[ _\-]?tokens?"
    r"|session[ _\-]?(?:tokens?|keys?|secrets?)|auth[ _\-]?tokens?|bearer[ _\-]?tokens?"
    r"|personal[ _\-]?access[ _\-]?tokens?|api[ _\-]?tokens?|oauth[ _\-]?tokens?|sas[ _\-]?tokens?"
    r"|token[ _\-](?:api|di[ _]accesso|di[ _]autenticazione|segreto|bearer|oauth)"
    r"|chiave[ _\-](?:api|segreta|privata|di[ _]cifratura|di[ _]crittografia|master"
    r"|di[ _]licenza|di[ _]attivazione|ssh|pgp|gpg|di[ _]firma)|codice[ _\-](?:di[ _])?licenza"
    r"|codice[ _\-]di[ _]attivazione|chiavi[ _\-]api|secrets?|tokens?"
    + "|" + _lx.alt(*_lx.KW_SECRET_STRONG.values())
)
# Segreti "deboli": la parola è comune, serve un valore lungo con entropia.
_KW_SECRET_WEAK = (
    r"auth|keys?|pat|jwt|signature|sig|cookie|(?:php)?sessid|jsessionid"
    r"|csrf[ _\-]?token|xsrf[ _\-]?token|credentials?|credenziali|chiave"
    + "|" + _lx.alt(*_lx.KW_SECRET_WEAK.values())
)

# Dopo la parola chiave: parole che dicono "non è il valore" (password_hash,
# password_min_length, user_count, token_expiry, key_file).
_SUFFIX_NOT_VALUE = (
    r"hash(?:ed|er)?|salt|digest|len(?:gth)?|min(?:imum|len|length)?|max(?:imum|len|length)?"
    r"|polic(?:y|ies)|reset|expir\w*|scad\w*|field|input|label|placeholder|regex|pattern"
    r"|rules?|strength|confirm\w*|conferma|repeat|ripeti|hint|error|errors|msg|message"
    r"|required|visible|show|hide|toggle|changed|updated|created|attempts?|tentativ\w*"
    r"|count|type|format|form|page|screen|dialog|manager|generator|checker|validator"
    r"|store|storage|file|path|list|history|age|prompt|protected|less|based|change"
    r"|recovery|forgot(?:ten)?|requirements?|complexity|criteria|mismatch|match|verify"
    r"|verification|check|box|icon|eye|mask(?:ed)?|encrypt\w*|strategy|service|controller"
    r"|component|module|view|model|schema|dto|entity|class|param(?:eter)?s?|arg(?:ument)?s?"
    r"|option|setting|settings|config(?:uration)?|env|var(?:iable)?|agents?|names?|data|info"
    r"|role|roles|group|groups|level|status|state|table|ids|profile|preferences?"
    r"|permissions?|limit|quota|mode|stor(?:y|ies)|interface|experience|guide|manual|docs?"
    r"|management|defined|friendly|facing|generated|provided|specified|space|header"
    r"|prefix|suffix|missing|invalid|rotation|rotate|version|ttl|alg(?:orithm)?|url|uri"
    r"|endpoint|provider|ring|vault|pairs?|code|codes|usage|scope|scopes|meta|metadata"
    r"|length_min|length_max|dimenticata|dimenticato|persa|smarrita|scaduta|sicura"
    r"|debole|forte"
    + "|" + _lx.alt(*_lx.SUFFIX_NOT_VALUE.values())
)

_STRICT_SEP = r"[ \t]*(?:==|:=|=>|->|→|[:=|])[ \t]*|\t+"
_NATURAL_SEP = (
    r"[ \t]+(?:è|e'|é|sarebbe|era|sono|resta|rimane|diventa|diventerà|sarà|corrisponde[ \t]+a"
    r"|equivale[ \t]+a|is|was|are|equals|would[ \t]+be|will[ \t]+be|should[ \t]+be|:[ \t]*is"
    r"|" + _lx.alt(_lx.NATURAL_COPULA) +                    # est, ist, lautet, es, luidt...
    r"|(?:(?:l[’']ho|l[’']abbiamo|ho|abbiamo|hanno|ha|hai|è[ \t]+stat[ao]|e'[ \t]+stat[ao]|viene"
    r"|va|andrà|verrà|has[ \t]+been|was|is|" + _lx.alt(_lx.NATURAL_AUX) + r")[ \t]+)?"
    r"(?:impostat[ao]|settat[ao]|mess[ao]|cambiat[ao]|modificat[ao]|reimpostat[ao]|configurat[ao]"
    r"|set|changed|updated|reset|configured|" + _lx.alt(_lx.NATURAL_PARTICIPLE) + r")[ \t]+"
    r"(?:a|su|in|come|to|as|" + _lx.alt(_lx.NATURAL_PREP) + r")"
    # "password ruotata gH7$kP2m", "password rotated Xk9#pL2m": participio senza
    # preposizione; _accept pretende una forma da segreto (_PARTICIPLE_SEP)
    r"|(?:(?:è[ \t]+stat[ao]|e'[ \t]+stat[ao]|was|has[ \t]+been|is)[ \t]+)?"
    r"(?:ruotat[ao]|rotated|rigenerat[ao]|regenerated|resettat[ao]|reimpostat[ao]))(?:[ \t]*:)?[ \t]+"
    r"(?:(?:la|il|lo|l[’']|un|una|uno|a|an|the|semplicemente|sempre|ancora|invece|ora|adesso"
    r"|now|still|just|currently|attualmente|quella|questa|quest[’']|questo|this|the[ \t]+following"
    r"|following|seguente|sempre[ \t]+stata|stata|stato|been|rimasta|rimasto|diventata"
    r"|diventato|impostata[ \t]+(?:a|su)|settata[ \t]+(?:a|su)|set[ \t]+to|uguale[ \t]+a"
    r"|pari[ \t]+a|" + _lx.alt(_lx.NATURAL_ARTICLES) + r")[ \t]*:?[ \t]+){0,3}"
)
# Parole ammesse tra la parola chiave e il separatore ("password del portale INPS:").
_FILL_WORDS = (
    r"del|della|dello|dei|degli|delle|di|d[’']|dell[’']|per|al|alla|allo|ai|agli|alle|sul|sulla"
    r"|of|for|to|the|my|your|our|his|her|their|mio|mia|tuo|tua|suo|sua|nostro|nostra|vostro|vostra"
    r"|attuale|corrente|nuova|nuovo|vecchia|vecchio|temporanea|temporaneo|provvisoria|provvisorio"
    r"|iniziale|current|new|old|temporary|temp|initial|default|admin|amministratore|root|utente"
    r"|account|accesso|login|wifi|wi-fi|rete|portale|sito|server|db|database|email|mail|pec|spid"
    r"|inps|home|banking|banca|app|gestionale|sistema|vpn|ftp|ssh|windows|pc|computer|router"
    r"|modem|dominio|azienda|aziendale|ufficio|servizio|posta|cloud|admin|amministrazione"
    r"|principale|secondaria|secondario|personale|condivisa|condiviso|shared|master|locale|remoto"
    r"|remota|service|user|utenza|profilo|profile|api|rest|sql|mysql|postgres|oracle|redis|smtp"
    r"|imap|pop3|nas|backup|cassaforte|keepass|bitwarden|gmail|outlook|office|365|google|apple"
    r"|microsoft|amazon|aws|azure|github|gitlab|docker|kubernetes|k8s|jenkins|wordpress|wp|cms"
    r"|" + _lx.alt(*_lx.FILL_WORDS.values()) +                 # fr/de/es/nl: lexicon.py
    r"|[A-Z][\w.\-]{1,24}|\([^()\n]{1,30}\)"
)
_FILL = r"(?:[ \t]+(?:" + _FILL_WORDS + r")){0,4}"


def _kw_rx(kws, plural_s=False):
    """Parola chiave case-insensitive, con confine destro. Il confine sinistro
    lo controlla `_left_boundary`, perché dipende dal caso: DB_PASSWORD e
    dbPassword sono ammessi, "compass" no."""
    return r"(?P<kw>" + kws + r")(?![^\W\d_])"


def _left_boundary(text, start, kw):
    """La parola chiave comincia una parola: preceduta da un carattere non
    alfabetico, oppure camelCase (dbPassword), oppure maiuscolo glued su
    maiuscolo (PGPASSWORD, MYSQL_PWD)."""
    if start == 0:
        return True
    prev = text[start - 1]
    if not prev.isalpha():
        return True
    if kw[0].isupper() and (prev.islower() or prev.isdigit()):
        return True                                     # dbPassword
    if kw.isupper() and prev.isupper() and len(kw) >= 3:
        return True                                     # PGPASSWORD
    return False


_SUFFIX_RX = re.compile(r"[ _\-.]?(?:" + _SUFFIX_NOT_VALUE + r")(?![^\W\d_])", re.I)
_SUFFIX_ALLOWED_ID = re.compile(r"access[ _\-]?key", re.I)   # access_key_id resta


def _suffix_blocks(text, kw_end, kw):
    m = _SUFFIX_RX.match(text, kw_end)
    if not m:
        return False
    if m.group(0).strip(" _-.").lower() in ("id", "ids") and _SUFFIX_ALLOWED_ID.search(kw):
        return False
    return True


# Le regex di contesto: (classe, regex). `kw` + opzionale virgoletta di chiusura
# (JSON: "password": "x") + riempitivo + separatore, poi il valore lo legge
# `_read_value`, che conosce virgolette, campi e punteggiatura di chiusura.
_FILL_LAZY = r"(?:[ \t]+(?:" + _FILL_WORDS + r")){0,4}?"
_CQ = r"(?P<cq>[\"'`”’])?"
# Dopo la parola chiave: riempitivo PIGRO + separatore esplicito (il riempitivo
# si allunga solo quanto serve a trovare il separatore: "password del portale
# INPS è"), altrimenti il solo spazio ("utente mrossi", "password Abc123!").
_SEP_RX = re.compile(_CQ + _FILL_LAZY + r"(?:(?P<strict>" + _STRICT_SEP + r")|(?P<natural>"
                     + _NATURAL_SEP + r"))", re.I)
_SEP_NOFILL_RX = re.compile(_CQ + r"(?:(?P<strict>" + _STRICT_SEP + r")|(?P<natural>"
                            + _NATURAL_SEP + r"))", re.I)
_SPACE_RX = re.compile(_CQ + r"[ \t]+")

# parola chiave di segreto "forte" che comincia dove comincia una parola chiave
# PASSWORD ("clave API", "clé secrète"): vince la più lunga, la regola PASSWORD
# lascia il passo
_STRONG_AT = re.compile(_kw_rx(_KW_SECRET_STRONG), re.I)

_CTX_RULES = [
    ("PASSWORD", re.compile(_kw_rx(_KW_PASSWORD), re.I), _SEP_RX),
    ("PIN", re.compile(_kw_rx(_KW_PIN), re.I), _SEP_RX),
    ("USERNAME", re.compile(_kw_rx(_KW_USERNAME), re.I), _SEP_RX),
    ("SECRET", re.compile(_kw_rx(_KW_SECRET_STRONG), re.I), _SEP_RX),
    ("WEAK", re.compile(_kw_rx(_KW_SECRET_WEAK), re.I), _SEP_NOFILL_RX),
]
# PIN/OTP scritti a gruppi: "5 4 3 2", "123 456", "1234-5678".
_NUMERIC_AT = re.compile(r"[ \t]*(?P<v>\d(?:[ \-]?\d){2,7})(?!\d)")
# passphrase senza virgolette: "correct horse battery staple" fino a fine riga o campo
_PHRASE_AT = re.compile(r"[ \t]*(?P<v>[^\s\"';,][^\n\"';,]{6,118}?)[ \t]*(?=[;,\n]|\.\s|$)")

# Con "=" come separatore il riempitivo deve essere di parole note: in
# `type="password" name="pwd"` la parola dopo "password" non introduce il valore.
_KNOWN_FILL = re.compile(r"^(?:[ \t]+(?:" + _FILL_WORDS + r"))*$", re.I)
_PAREN_FILL = re.compile(r"^[ \t]*\([^()\n]{1,30}\)[ \t]*$")
_XML_RX = re.compile(
    r"<(?P<kw>" + _KW_PASSWORD + r"|" + _KW_USERNAME + r"|" + _KW_SECRET_STRONG
    + r")(?:\s[^<>\n]*)?>[ \t]*(?P<val>[^<\n]{1,160}?)[ \t]*</", re.I)
_NUMERIC = re.compile(r"^\d(?:[ \-]?\d){2,7}$")
_DIGITS = re.compile(r"\d")


# ----------------------------------------------------------------------------
# Formati noti (SECRET): si riconoscono da soli, senza parola chiave.
# ----------------------------------------------------------------------------
_PEM_RX = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
    r"(?:[A-Za-z0-9+/=\-\s:,]|\\n){20,}?"
    r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|(?=\n[ \t]*\n)|\Z)")
_JWT_RX = re.compile(r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}"
                     r"\.[A-Za-z0-9_\-]{8,}(?![A-Za-z0-9_\-])")
_PREFIX_RX = re.compile(
    r"(?<![A-Za-z0-9_\-])(?:"
    r"sk-[A-Za-z0-9_\-]{20,}"                          # OpenAI, Anthropic, OpenRouter, DeepSeek
    r"|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}|whsec_[A-Za-z0-9]{20,}"   # Stripe
    r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{60,}"          # GitHub
    r"|gl(?:pat|rt|ptt|dt|ft|soat|agent)-[A-Za-z0-9_\-]{20,}"           # GitLab
    r"|(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}"                              # AWS
    r"|xox[abeprs]-[A-Za-z0-9\-]{20,}"                                   # Slack
    r"|AIza[0-9A-Za-z_\-]{35}|ya29\.[0-9A-Za-z_\-]{30,}|GOCSPX-[0-9A-Za-z_\-]{20,}"
    r"|1//0[0-9A-Za-z_\-]{30,}"                                          # Google
    r"|hf_[A-Za-z0-9]{30,}"                                              # Hugging Face
    r"|npm_[A-Za-z0-9]{36}|pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_\-]{40,}"
    r"|rubygems_[a-f0-9]{48}|oy2[a-z0-9]{43}"
    r"|dckr_pat_[A-Za-z0-9_\-]{20,}"                                     # Docker
    r"|\d{8,10}:AA[A-Za-z0-9_\-]{33}"                                    # Telegram bot
    r"|[MN][A-Za-z0-9]{23,}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}"     # Discord
    r"|SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"                     # SendGrid
    r"|key-[a-f0-9]{32}|[a-f0-9]{32}-us\d{1,2}|xkeysib-[a-f0-9]{64}-[A-Za-z0-9]{16}"
    r"|SK[a-f0-9]{32}"                                                   # Twilio
    r"|shp(?:at|ca|pa|ss)_[a-fA-F0-9]{32}"                               # Shopify
    r"|sq0(?:atp|csp)-[A-Za-z0-9_\-]{22,}"                               # Square
    r"|do[opr]_v1_[a-f0-9]{64}"                                          # DigitalOcean
    r"|dapi[a-f0-9]{32}"                                                 # Databricks
    r"|lin_api_[A-Za-z0-9]{40}"                                          # Linear
    r"|ntn_[A-Za-z0-9]{40,}|secret_[A-Za-z0-9]{43}"                      # Notion
    r"|pat[A-Za-z0-9]{14}\.[a-f0-9]{64}"                                 # Airtable
    r"|ATATT3[A-Za-z0-9_\-=]{100,}|ATBB[A-Za-z0-9]{32}"                   # Atlassian
    r"|hv[sbr]\.[A-Za-z0-9_\-]{24,}"                                     # Vault
    r"|PMAK-[a-f0-9]{24}-[a-f0-9]{34}"                                   # Postman
    r"|gl(?:c|sa)_[A-Za-z0-9_=\-]{30,}"                                  # Grafana
    r"|pscale_(?:pw|tkn|oauth)_[A-Za-z0-9_.\-]{30,}"                     # PlanetScale
    r"|sbp_[a-f0-9]{40}"                                                 # Supabase
    r"|gsk_[A-Za-z0-9]{40,}|r8_[A-Za-z0-9]{30,}|pplx-[A-Za-z0-9]{40,}|xai-[A-Za-z0-9]{40,}"
    r"|tvly-[A-Za-z0-9\-]{20,}|pcsk_[A-Za-z0-9_]{30,}|fw_[A-Za-z0-9]{20,}"
    r"|pat-(?:na|eu)\d-[a-f0-9\-]{36}"                                   # HubSpot
    r"|sl\.[A-Za-z0-9_\-]{130,}"                                         # Dropbox
    r"|EAA[A-Za-z0-9]{30,}"                                              # Facebook
    r"|AAAAAAAAAAAAAAAAAAAAA[A-Za-z0-9%]{40,}"                            # Twitter
    r"|AGE-SECRET-KEY-1[A-Z0-9]{58}"
    r"|[A-Za-z0-9_~.\-]{3}8Q~[A-Za-z0-9_~.\-]{31,34}"                     # Azure client secret
    r"|LTAI[A-Za-z0-9]{20}|AKID[A-Za-z0-9]{32}"                          # Alibaba, Tencent
    r"|HRKU-[A-Za-z0-9_\-]{50,}|nfp_[A-Za-z0-9]{36,}|dp\.(?:pt|st|ct|sa)\.[A-Za-z0-9]{40,}"
    r"|pul-[a-f0-9]{40}|sgp_[A-Za-z0-9_]{40,}|[A-Za-z0-9]{14}\.atlasv1\.[A-Za-z0-9_=\-]{60,}"
    r"|NRAK-[A-Z0-9]{27}|CFPAT-[A-Za-z0-9_\-]{43}|rdme_[a-z0-9]{70}|CLOJARS_[a-z0-9]{60}"
    r"|sntrys_[A-Za-z0-9_\-]{50,}|pnu_[a-z0-9]{36}|aio_[A-Za-z0-9]{28}|ico-[A-Za-z0-9]{32}"
    r"|AAAA[A-Za-z0-9_\-]{7}:[A-Za-z0-9_\-]{140}"                         # Firebase
    r"|vault:v\d+:[A-Za-z0-9+/=]{20,}"                                    # Vault transit
    r")(?![A-Za-z0-9_\-])")

_URI_RX = re.compile(
    r"(?<![\w.])(?P<scheme>[a-z][a-z0-9+.\-]{1,30})://"
    r"(?P<user>[^\s/:@'\"<>]{1,64}):(?P<pass>[^\s@/'\"<>]{1,160})@", re.I)

_AUTH_RX = re.compile(
    r"(?<![\w\-])(?:(?:proxy-)?authorization[ \t]*[:=]?[ \t]*)?"
    r"(?P<scheme>bearer|basic|token|digest|negotiate|ntlm|oauth)[ \t]+"
    r"(?P<val>[A-Za-z0-9\-._~+/=:,\"]{8,})", re.I)
_APIKEY_HEADER_RX = re.compile(
    r"(?<![\w\-])(?:x-api-key|x-auth-token|x-access-token|x-amz-security-token|private-token"
    r"|api-key|x-token|x-secret|x-client-secret|ocp-apim-subscription-key|x-goog-api-key"
    r"|x-hub-signature(?:-256)?|x-gitlab-token|x-vault-token|x-shopify-access-token"
    r"|x-auth-key|x-functions-key|x-rapidapi-key|x-figma-token|x-secret-key)"
    r"[ \t]*[:=][ \t]*[\"']?(?P<val>[A-Za-z0-9\-._~+/=]{8,})", re.I)
_COOKIE_RX = re.compile(r"(?<![\w\-])(?:cookie|set-cookie)[ \t]*:[ \t]*(?P<val>[^\r\n]{16,})", re.I)

_SEED_RX = re.compile(
    r"(?<![a-z])(?:(?:seed|mnemonic|recovery|backup)[ _\-]?(?:phrase|words?|frase|parole)"
    r"|frase[ _\-]?(?:seed|mnemonica|di[ _]recupero|segreta)"
    r"|parole[ _\-]?(?:seed|di[ _]recupero|mnemoniche))(?![a-z])"
    r"[^\n]{0,24}?[:=]?[ \t]*\n?[ \t]*"
    r"(?P<val>(?:[a-z]{3,8}[ ,\n]+){11,23}[a-z]{3,8})(?![a-z])", re.I)
_RECOVERY_RX = re.compile(
    r"(?<![a-z])(?:(?:recovery|backup)[ _\-]?codes?|codic[ei][ _\-]di[ _](?:recupero|backup"
    r"|ripristino|emergenza))(?![a-z])[^\n]{0,20}?[:=]?[ \t]*"
    r"(?P<val>[A-Za-z0-9]{4,}(?:[\- ][A-Za-z0-9]{4,}){1,7})(?![A-Za-z0-9])", re.I)

# Riga di comando.
_CLI_RULES = [
    # curl -u user:pass, --user user:pass
    (re.compile(r"(?<!\S)(?:-u|--user)(?:[ \t]+|=)[\"']?(?P<u>[^\s:\"']{1,64}):(?P<p>[^\s\"']{1,160})"),
     ("u", "USERNAME"), ("p", "PASSWORD")),
    # smbclient -U user%pass
    (re.compile(r"(?<!\S)-U[ \t]*[\"']?(?P<u>[^\s%\"']{1,64})%(?P<p>[^\s\"']{1,160})"),
     ("u", "USERNAME"), ("p", "PASSWORD")),
    # net use ... /user:DOM\utente password
    (re.compile(r"/user:(?P<u>[^\s/]{1,64})[ \t]+(?P<p>[^\s/\-][^\s]{2,159})", re.I),
     ("u", "USERNAME"), ("p", "PASSWORD")),
    # -u user -p pass, -U user -P pass, --username user --password pass, -pPass attaccato
    (re.compile(r"(?<!\S)(?:-[uU]|--user(?:name)?)(?:[ \t]+|=)[\"']?(?P<u>[^\s\-\"'][^\s\"']{0,63})[\"']?"
                r"[ \t]+(?P<pf>-[pP]|--pass(?:word|wd)?)(?P<psep>[ \t]+|=)?[\"']?(?P<p>[^\s\-\"'][^\s\"']{0,159})"),
     ("u", "USERNAME"), ("p", "PASSWORD")),
    # mysql ... -pSegreto (attaccato), sshpass -p segreto
    (re.compile(r"\b(?:mysql\w*|mariadb\w*|mysqladmin|mysqldump)\b[^\n]*?[ \t]-p(?P<p>[^\s\-][^\s]{2,159})", re.I),
     ("p", "PASSWORD")),
    (re.compile(r"\bsshpass[ \t]+-p[ \t]*[\"']?(?P<p>[^\s\"']{3,160})", re.I),
     ("p", "PASSWORD")),
]
_PSQL_LINE = re.compile(r"\b(?:psql|pg_dump|pg_restore|pg_dumpall|createdb|dropdb)\b", re.I)
_MYSQL_LINE = re.compile(r"\b(?:mysql\w*|mariadb\w*|mysqladmin|mysqldump)\b", re.I)

# Coppie: "credenziali: mrossi / Abc123!", "user e password sono mrossi e Abc123!"
_PAIR_KW = (
    r"credenziali|credentials|creds|login|accesso|account|utenza"
    r"|nome[ \t]utente[ \t]e[ \t]password"
    r"|user(?:name)?[ \t]*(?:/|e|and|&|-|,)[ \t]*(?:pass(?:word)?|pwd|pw)"
    r"|utente[ \t]*(?:/|e|and|&|-|,)[ \t]*(?:password|pwd|pw)"
    r"|u/p|user/pass|utente/password|username/password|login/password"
    + "|" + _lx.alt(_lx.PAIR_KW)
)
_VAL_ID = r"[\"']?[\w.@+\-\\/]{2,64}[\"']?"
_PAIR_SEP = (r"(?P<ps>[ \t]*/[ \t]*|[ \t]*\|[ \t]*|[ \t]*[,;][ \t]*"
             r"|[ \t]+(?:e|and|&|-|–|con|with|pw|pwd|pass|password|passwd|" + _lx.alt(_lx.PAIR_CONJ)
             + r")(?:[ \t]*:)?[ \t]+|[ \t]+)")
_PAIR_RX = re.compile(
    r"(?<![^\W\d_])(?P<kw>" + _PAIR_KW + r")(?![^\W\d_])" + _FILL
    + r"(?:" + _STRICT_SEP + r"|" + _NATURAL_SEP + r"|[ \t]+)"
    + r"(?P<u>" + _VAL_ID + r")" + _PAIR_SEP + r"(?P<p>[\"']?\S{3,160}[\"']?)", re.I)

# "accedi con mrossi e Estate2024!", "logged in as jdoe"
_LOGIN_VERBS = (
    r"acced\w*|entr(?:a|are|o|i|ate|ano)|collegat\w*|collegar\w*|connett\w*|logg\w*"
    r"|autentic\w*|log[ \t]?in|login|sign[ \t]?in|logged[ \t]?in|connect(?:ed|s|ing)?"
    r"|authenticat\w*"
    + "|" + _lx.alt(_lx.LOGIN_VERBS)
)
_USE_VERBS = r"us[ae](?:re|ndo|i|te)?|utilizz\w*|use|using|" + _lx.alt(_lx.USE_VERBS)
_LOGIN_RX = re.compile(
    r"(?<![^\W\d_])(?:(?P<login>" + _LOGIN_VERBS + r")|(?P<use>" + _USE_VERBS + r"))[ \t]+"
    r"(?:con|come|with|as|using|usando|tramite|le[ \t]credenziali|credenziali|l[’']utente|utente"
    r"|user|account|l[’']account|" + _lx.alt(_lx.LOGIN_PREP) + r")"
    r"(?:[ \t]+(?:l[’']utente|utente|user|account|le[ \t]credenziali|credenziali|di|del|della|il|lo|la"
    r"|" + _lx.alt(_lx.LOGIN_FILL) + r"))*"
    r"[ \t]+(?P<u>" + _VAL_ID + r")"
    r"(?:(?P<ps>[ \t]*/[ \t]*|[ \t]*[,;][ \t]*|[ \t]+(?:e|and|&|con|with|pw|pwd|pass|password|passwd"
    r"|" + _lx.alt(_lx.PAIR_CONJ) + r")"
    r"(?:[ \t]*:)?[ \t]+)(?P<p>[\"']?\S{3,160}[\"']?))?", re.I)

_SHORT_KW = frozenset({"pass", "pw", "psw", "pwd", "usr", "uid", "nick", "sig", "pat", "auth", "key", "keys"})
_SPACE_USER_KW = re.compile(r"^(?:utente|user|username|user[ _\-]?name|nome[ _\-]?utente|login|user[ _\-]?id|userid|uid|utenza"
                            r"|" + _lx.alt(_lx.SPACE_USER_KW) + r")$", re.I)
_WEAK_KEY_PREV = re.compile(
    r"(?:primary|foreign|public|pub|sort|partition|hot|shortcut|hash|cache|index|column|dict"
    r"|map|lookup|search|ssh|gpg|pgp|host|composite|unique|candidate|super|natural|surrogate"
    r"|row|range|partition|encryption|decryption|api|the|a|una|la|di|del|della"
    r"|" + _lx.alt(_lx.WEAK_KEY_PREV) + r")[ \t]*$", re.I)
# le "chiavi" deboli su cui vale il controllo della parola precedente
_WEAK_KEY_WORD = re.compile(r"^(?:keys?|chiave|cl[ée]s?|clefs?|schl[üu]ssel|sleutels?)$", re.I)
# parola prima della parola chiave che ne cambia il senso ("palabra clave" è
# una keyword, non una password)
_KW_PREV_BLOCK = [(re.compile(r"^(?:" + k + r")$", re.I), re.compile(p, re.I)) for k, p in _lx.KW_PREV_BLOCK]
# copula plurale: "le password sono obbligatorie" non introduce un valore
_PLURAL_COPULA = re.compile(r"(?<![^\W\d_])(?:sono|are|were|" + _lx.alt(_lx.NATURAL_PLURAL) + r")(?![^\W\d_])", re.I)
# parole della classe PIN dopo le quali il codice può essere alfanumerico
_PIN_ALNUM = ("otp", "verif", "2fa", "mfa", "one") + _lx.PIN_ALNUM_HINTS
_PATH_LIKE = re.compile(r"^(?:[~./\\]|[A-Za-z]:\\)|\.(?:pem|key|p12|pfx|json|txt|crt|pub|cer|der|ppk|yaml|yml|env)$", re.I)
_NAME_NEXT = re.compile(r"^[ \t]+(?:(?:de|der|den|van|von|ter|te|du|da|di|del|della|dell[’']|le|la|des|dos|das|el|y|e|zu|zur|af|op|'t)"
                        r"[ \t]+)*[A-ZÀ-Þ][a-zß-ÿ]+")
_CLAUSE_END = re.compile(r"^[ \t]*(?:[.,;:!?)»”\"']|\n|$)")


def _class_of(kw):
    for cls, rx in (("PASSWORD", _KW_PASSWORD), ("PIN", _KW_PIN), ("USERNAME", _KW_USERNAME),
                    ("SECRET", _KW_SECRET_STRONG), ("WEAK", _KW_SECRET_WEAK)):
        if re.fullmatch(rx, kw, re.I):
            return cls
    return None


_PARTICIPLE_SEP = re.compile(r"ruotat|rotated|rigenerat|regenerated|resettat|reimpostat|"
                             + _lx.alt(_lx.PARTICIPLE_SEP), re.I)


def _accept(cls, kw, kind, text, s, e, quoted, sep=""):
    """Il valore letto va bene per questa classe e questo tipo di separatore?
    Ritorna la label finale o None."""
    v = text[s:e]
    if _bad_value(v, quoted):
        return None
    kwl = kw.lower()
    if cls == "PIN":
        if kwl.startswith("cv"):
            ok = bool(re.fullmatch(r"\d{3,4}", v))
        elif any(h in kwl for h in _PIN_ALNUM):
            ok = bool(_NUMERIC.fullmatch(v) or re.fullmatch(r"[A-Za-z0-9]{4,10}", v)) and bool(_DIGITS.search(v))
        else:
            ok = bool(_NUMERIC.fullmatch(v)) and len(re.sub(r"\D", "", v)) >= 4
        return "PASSWORD" if ok else None
    if cls == "PASSWORD":
        if kwl == "pass" and kind != "strict":
            return None                             # "pass" è anche una parola
        if kind == "strict":
            return "PASSWORD" if (len(v) >= 4 or (quoted and len(v) >= 3)) else None
        if len(v) < 4:
            return None
        if quoted or _secret_shape(v):
            return "PASSWORD" if (kind == "natural" or len(v) >= 5) else None
        # "la password è pippolandia.": parola minuscola, ma chiude la frase e
        # la copula è singolare ("le password sono obbligatorie" no); dopo un
        # participio no ("la password è stata ruotata stamattina.")
        if kind == "natural" and v.isalpha() and v.islower() and len(v) >= 6 \
                and not _PARTICIPLE_SEP.search(sep) \
                and _CLAUSE_END.match(text[e:e + 3]) \
                and not _PLURAL_COPULA.search(sep):
            return "PASSWORD"
        return None
    if cls == "USERNAME":
        if kind == "strict":
            if len(v) < 2:
                return None
            if _is_name_word(v) and _NAME_NEXT.match(text[e:e + 40]):
                return None                     # "Utente: Mario Rossi" -> nome
            return "USERNAME"
        if not (quoted or _identifier_shape(v)):
            return None
        if kind == "space":
            if not _SPACE_USER_KW.match(kw):
                return None
            if len(v) < 3 or not (v.islower() or _DIGITS.search(v) or re.search(r"[._@\-\\]", v)):
                return None
            # "login avviene con SPID": con "login" da solo serve una forma da identificativo
            if kwl == "login" and not (_DIGITS.search(v) or re.search(r"[._@\-\\]", v)):
                return None
        return "USERNAME"
    if cls == "SECRET":
        if kind == "strict":
            return "SECRET" if len(v) >= 8 else None
        if kind == "natural":
            return "SECRET" if (len(v) >= 12 and _secret_shape(v) and _entropy(v) >= 3.0) else None
        return "SECRET" if (len(v) >= 16 and _secret_shape(v) and _entropy(v) >= 3.0) else None
    if cls == "WEAK":
        if kind != "strict" or len(v) < 16 or not _secret_shape(v) or _entropy(v) < 3.0:
            return None
        if _PATH_LIKE.search(v):
            return None
        return "SECRET"
    return None


def _ctx_hits(text):
    out = []
    for cls, kw_rx, sep_rx in _CTX_RULES:
        for m in kw_rx.finditer(text):
            kw = m.group("kw")
            kw_end = m.end("kw")
            if not _left_boundary(text, m.start("kw"), kw):
                continue
            if _suffix_blocks(text, kw_end, kw):
                continue
            before = text[max(0, m.start("kw") - 20):m.start("kw")]
            if any(k.match(kw) and p.search(before) for k, p in _KW_PREV_BLOCK):
                continue
            if cls == "PASSWORD":
                ms = _STRONG_AT.match(text, m.start("kw"))
                if ms and ms.end("kw") > kw_end:
                    continue
            if cls == "WEAK" and _WEAK_KEY_WORD.match(kw) \
                    and _WEAK_KEY_PREV.search(text[max(0, m.start("kw") - 20):m.start("kw")]):
                continue
            m2 = sep_rx.match(text, kw_end)
            if m2 is not None:
                if m2.group("strict") is not None:
                    kind, sep_start = "strict", m2.start("strict")
                else:
                    kind, sep_start = "natural", m2.start("natural")
            else:
                m2 = _SPACE_RX.match(text, kw_end)
                if m2 is None:
                    continue
                kind = "space"
                sep_start = m2.end("cq") if m2.group("cq") else kw_end
            cq = m2.group("cq")
            fill_text = text[(m2.end("cq") if cq else kw_end):sep_start]
            if cq and fill_text.strip():
                continue
            sep = text[sep_start:m2.end()]
            if kind == "strict" and "=" in sep and fill_text \
                    and not (_KNOWN_FILL.match(fill_text) or _PAREN_FILL.match(fill_text)):
                continue
            if kind == "strict" and sep.strip() == "|" and fill_text.strip():
                continue
            got = None
            if cls == "PIN":
                m3 = _NUMERIC_AT.match(text, m2.end())
                if m3:
                    got = (m3.start("v"), m3.end("v"), False)
            elif cls == "PASSWORD" and "phrase" in kw.lower() and kind == "strict":
                # una passphrase è fatta di più parole: fino a fine riga/campo
                m3 = _PHRASE_AT.match(text, m2.end())
                if m3 and len(m3.group("v").split()) >= 2:
                    got = (m3.start("v"), m3.end("v"), False)
            if got is None:
                got = _read_value(text, m2.end())
            if got is None:
                continue
            s, e, quoted = got
            label = _accept(cls, kw, kind, text, s, e, quoted, sep)
            if label is None:
                continue
            prio = 2 if kind == "strict" else 1
            out.append((prio, s, e, label, False))
    for m in _XML_RX.finditer(text):
        cls = _class_of(m.group("kw"))
        if cls is None:
            continue
        s, e = m.start("val"), m.end("val")
        label = _accept(cls, m.group("kw"), "strict", text, s, e, False)
        if label:
            out.append((3, s, e, label, False))
    return out


def _strip_quotes(text, s, e):
    if e - s >= 2 and text[s] in "\"'" and text[e - 1] == text[s]:
        return s + 1, e - 1
    return s, e


def _pair_hits(text):
    out = []
    for rx, is_login in ((_PAIR_RX, False), (_LOGIN_RX, True)):
        for m in rx.finditer(text):
            us, ue = _strip_quotes(text, m.start("u"), m.end("u"))
            u = text[us:ue]
            has_p = m.group("p") is not None
            if not _identifier_shape(u) or _bad_value(u):
                continue
            if is_login:
                if m.group("use") is not None and not (has_p or _DIGITS.search(u) or re.search(r"[._@\-\\]", u)):
                    continue
                if not has_p and re.fullmatch(r"[A-Za-z]+", u) and not u.islower():
                    continue
            p = None
            if has_p:
                ps, pe = _strip_quotes(text, m.start("p"), m.end("p"))
                pv = text[ps:pe]
                # punteggiatura di chiusura frase
                while pe > ps and text[pe - 1] in ".,;:)»\"'?" and not (text[pe - 1] == ")" and "(" in pv):
                    pe -= 1
                    pv = text[ps:pe]
                quoted = text[m.start("p")] in "\"'"
                if pv and pv != u and not _bad_value(pv, quoted) and (quoted or _secret_shape(pv)):
                    p = (ps, pe)
                elif not is_login:
                    continue                        # coppia senza password: non è una coppia
            elif not is_login:
                continue
            out.append((3, us, ue, "USERNAME", False))
            if p:
                out.append((3, p[0], p[1], "PASSWORD", False))
    return out


def _cli_hits(text):
    out = []
    for rx, *groups in _CLI_RULES:
        for m in rx.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            line = text[line_start:text.find("\n", m.start()) if text.find("\n", m.start()) != -1 else len(text)]
            for g, label in groups:
                s, e = m.start(g), m.end(g)
                v = text[s:e]
                if _bad_value(v):
                    continue
                if label == "PASSWORD" and v.isdigit() and _PSQL_LINE.search(line):
                    continue                        # psql -p 5432 è la porta
                if label == "PASSWORD" and "psep" in m.groupdict() and (m.group("psep") or "").strip() == "" \
                        and m.group("psep") and _MYSQL_LINE.search(line):
                    continue                        # mysql -u root -p mydb: -p chiede la password, mydb è il db
                out.append((3, s, e, label, False))
    return out


def _format_hits(text):
    out = []
    for m in _PEM_RX.finditer(text):
        out.append((5, m.start(), m.end(), "SECRET", True))
    for m in _JWT_RX.finditer(text):
        out.append((5, m.start(), m.end(), "SECRET", True))
    for m in _PREFIX_RX.finditer(text):
        out.append((5, m.start(), m.end(), "SECRET", False))
    for m in _URI_RX.finditer(text):
        if not _bad_value(m.group("user")):
            out.append((4, m.start("user"), m.end("user"), "USERNAME", False))
        if not _bad_value(m.group("pass")):
            out.append((4, m.start("pass"), m.end("pass"), "PASSWORD", False))
    for m in _AUTH_RX.finditer(text):
        s, e = m.start("val"), m.end("val")
        v = text[s:e].rstrip(",:\"")
        e = s + len(v)
        scheme = m.group("scheme").lower()
        validated = False
        if scheme == "basic":
            import base64
            try:
                validated = b":" in base64.b64decode(v + "=" * (-len(v) % 4), validate=False)
            except Exception:
                validated = False
            if not validated and not re.fullmatch(r"[A-Za-z0-9+/]{12,}={0,2}", v):
                continue
        elif scheme in ("bearer", "token", "oauth"):
            if len(v) < 16 or _bad_value(v):
                continue
        elif len(v) < 16:
            continue
        out.append((4, s, e, "SECRET", validated))
    for m in _APIKEY_HEADER_RX.finditer(text):
        if not _bad_value(m.group("val")):
            out.append((4, m.start("val"), m.end("val"), "SECRET", False))
    for m in _COOKIE_RX.finditer(text):
        v = m.group("val").rstrip(" \t\"';")
        if "=" in v and len(v) >= 16:
            out.append((4, m.start("val"), m.start("val") + len(v), "SECRET", False))
    for m in _SEED_RX.finditer(text):
        out.append((4, m.start("val"), m.end("val"), "SECRET", False))
    for m in _RECOVERY_RX.finditer(text):
        if _DIGITS.search(m.group("val")) or re.search(r"[A-Z]", m.group("val")):
            out.append((4, m.start("val"), m.end("val"), "SECRET", False))
    return out


def _resolve(hits):
    """Senza sovrapposizioni: priorità, poi lunghezza."""
    kept = []
    for prio, s, e, label, validated in sorted(hits, key=lambda h: (-h[0], -(h[2] - h[1]), h[1])):
        if e <= s:
            continue
        if any(s < ke and e > ks for _, ks, ke, _, _ in kept):
            continue
        kept.append((prio, s, e, label, validated))
    return sorted(kept, key=lambda h: h[1])


def detect_credentials(text):
    """Entità USERNAME / PASSWORD / SECRET, nella forma di `detect_regex`."""
    if not text:
        return []
    hits = _format_hits(text) + _cli_hits(text) + _pair_hits(text) + _ctx_hits(text)
    ents = []
    for prio, s, e, label, validated in _resolve(hits):
        ents.append({
            "label": label,
            "start": s,
            "end": e,
            "score": 0.95 if prio >= 4 else (0.9 if label == "SECRET" else 0.85),
            "validated": validated,
            "source": "regex",
        })
    return ents
