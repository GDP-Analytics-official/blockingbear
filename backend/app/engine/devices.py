r"""
Nomi di macchina e identificativi di dispositivo del gruppo «Cybersecurity»:
due label regex, senza modello.

  HOSTNAME   il nome di una macchina, ma SOLO nelle forme ancorate:
               - FQDN con suffisso interno (.local, .lan, .intra, .internal,
                 .corp, .localdomain, .priv, .dmz, .home.arpa, .intern, .lokal);
               - percorso UNC \\host\share;
               - nomi di default di Windows (DESKTOP-XXXXXXX, LAPTOP-..., WIN-...);
               - riga di comando (ssh, scp, ping, nslookup, telnet, nmap,
                 mstsc /v:, Enter-PSSession...) e prompt "utente@host:~$";
               - riga di syslog (BSD e RFC 5424);
               - CN= di un distinguished name seguito da OU=/CN=;
               - parola chiave + identificativo ("host srv-app01",
                 "hostname: WKS-042", "nodo k8s-worker-3", "server di posta mail01").
  DEVICE_ID  IMEI (Luhn + prefisso di ente, o parola chiave), ICCID della SIM
             (prefisso 89 + Luhn, o parola chiave), IMSI con parola chiave,
             numero di serie / service tag / asset tag con parola chiave, MEID,
             UDID, Android ID, hardware id e simili con parola chiave.

Il nome host nudo ("il jumphost", "bastion", "switch core") non ha forma e
resta in chiaro: senza un'ancora non si distingue da una parola qualunque, e
la convenzione di naming del cliente si copre con i termini personalizzati.
Ma un nome ancorato UNA volta vale per tutto il testo: trovato "workstation
WKS-042", anche il "WKS-042" nudo tre righe dopo è HOSTNAME (`_propagate`), e
lo stesso per un numero di serie ripetuto. Per i nomi che sono anche parole
("ssh mail") la propagazione non si fa.
Nella forma a parola chiave il valore deve AVERE la forma di un identificativo
(inizia con una lettera e contiene una cifra o un trattino): "server web",
"host europeo", "Windows Server 2019" non lo sono.

A differenza di cyber.py queste due label NON stanno in EXACT_SPAN_LABELS: un
hostname è case-insensitive per definizione (SRV01 e srv01 sono la stessa
macchina e devono avere lo stesso placeholder), un numero di serie idem, e la
punteggiatura ai bordi ("host srv01.") non appartiene al valore.

IMEI e ICCID superano il Luhn come una carta di credito: `is_device_number`
li esclude dalla forma carta (`detectors.card_ok`), perché nessun circuito
emette carte a 15 cifre con prefisso 00/01/35/86/99 né carte con prefisso 89,
che ISO/IEC 7812 riserva alle telecomunicazioni. Così DEVICE_ID le trova
senza contendersi la span con CREDITCARDNUMBER.
"""

import re

from .text_patterns import normalized_detector

from . import lexicon as _lx

DEVICE_LABELS = frozenset({"HOSTNAME", "DEVICE_ID"})

# prefissi degli enti che assegnano i TAC (Reporting Body Identifier)
IMEI_RBI = frozenset({"00", "01", "35", "86", "99"})

# etichetta DNS: lettere, cifre, trattini interni, al massimo 63 caratteri
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
_FQDN = _LABEL + r"(?:\." + _LABEL + r")*"

# TLD pubblici della lista chiusa del detector URL (lexicon.PUBLIC_TLD, la
# stessa che usa detectors.DETECTORS): un dominio che finisce così è già un
# URL, e HOSTNAME non deve contenderglielo
_PUBLIC_TLD = _lx.PUBLIC_TLD


def _luhn(d):
    tot, alt = 0, False
    for ch in reversed(d):
        n = int(ch)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        tot += n
        alt = not alt
    return tot % 10 == 0


def is_device_number(digits):
    """La sequenza di sole cifre ha la forma di un IMEI (15 cifre, prefisso di
    ente) o di un ICCID (18-22 cifre, prefisso 89)? Serve a `detectors.card_ok`
    per non scambiarli per carte di credito: sono forme che nessun circuito usa."""
    n = len(digits)
    return (n == 15 and digits[:2] in IMEI_RBI) or (18 <= n <= 22 and digits.startswith("89"))


# ----------------------------------------------------------------------------
# HOSTNAME
# ----------------------------------------------------------------------------
_INTERNAL_TLD = (r"(?:local|lan|intra|intranet|internal|corp|localdomain|priv|dmz|home\.arpa|"
                 + _lx.alt(_lx.INTERNAL_TLD) + r")")
_FQDN_INTERNAL_RX = re.compile(
    r"(?<![\w.\-@])(?P<host>(?:" + _LABEL + r"\.)+" + _INTERNAL_TLD + r")(?![\w\-]|\.\w)",
    re.IGNORECASE)
# \\host\share (anche con le barre raddoppiate di JSON/PowerShell); "\\?\" e
# "\\.\" sono percorsi di device, non macchine
_UNC_RX = re.compile(
    r"(?<![\w\\])(?:\\\\){1,2}(?P<host>[A-Za-z0-9][A-Za-z0-9\-.]{0,252}[A-Za-z0-9])"
    r"(?=\\|[\s,;:)\]\"']|$)")
# nome generato da Windows all'installazione: prefisso fisso, suffisso maiuscolo
# alfanumerico con almeno una cifra
_WIN_DEFAULT_RX = re.compile(
    r"(?<![\w\-])(?P<host>(?:DESKTOP|LAPTOP|WIN)-(?=[A-Z0-9]*\d)[A-Z0-9]{4,11})(?![\w\-])")
# comandi che prendono un host come argomento (dopo eventuali flag e "utente@")
_SSH_LIKE_RX = re.compile(
    r"(?<![\w\-/.])(?P<cmd>ssh-copy-id|ssh|sftp|ping6?|traceroute6?|tracert|mtr|nslookup|dig|telnet"
    r"|nc|ncat|nmap|rdesktop|Test-Connection|Test-NetConnection|Enter-PSSession|Invoke-Command)"
    r"(?:[ \t]+-{1,2}(?:[pilocJFbeLRDWmBSEwA][ \t]*\S+|ComputerName|Port[ \t]+\d+|[A-Za-z0-9]+))*"
    r"[ \t]+(?:[^\s@:/\\]{1,64}@)?(?P<host>" + _FQDN + r")(?![\w\-]|\.\w)")
# scp/rsync/sftp: l'host è il token con i due punti ("utente@host:/percorso")
_SCP_RX = re.compile(
    r"(?<![\w\-/.])(?P<cmd>scp|rsync|sftp)\b[^\n]*?(?<![\w.\-@/\\])(?:[^\s@:/\\]{1,64}@)?"
    r"(?P<host>" + _FQDN + r"):(?=[/~\w]|$)")
_RDP_RX = re.compile(
    r"(?<![\w\-/.])(?P<cmd>mstsc|xfreerdp|wfreerdp)\b(?:[ \t]+/(?!v:)\w+(?::\S+)?)*[ \t]+/v:"
    r"(?P<host>" + _FQDN + r")(?![\w\-]|\.\w)", re.IGNORECASE)
_PSEXEC_RX = re.compile(
    r"(?<![\w\-/.])(?P<cmd>psexec|paexec|winrs[ \t]+-r:)(?:[ \t]*\\\\|[ \t]*)(?P<host>" + _FQDN + r")(?![\w\-]|\.\w)",
    re.IGNORECASE)
# prompt di shell: "root@srv01:~$", "[admin@srv01 ~]$", "user@srv01:/var/log#"
_PROMPT_RX = re.compile(
    r"(?<![\w.\-@])\[?(?P<user>[A-Za-z0-9._\-]{1,64})@(?P<host>" + _FQDN + r")"
    r"(?:[ :][^\s\]$#]{0,120})?\]?[ \t]?[$#](?=\s|$)")
# syslog BSD: "Sep  2 10:15:32 srv01 sshd[1234]: ..."; RFC 5424: "<34>1 2026-... srv01 app - - -"
_SYSLOG_RX = re.compile(
    r"^(?:<\d{1,3}>)?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[ \t]+\d{1,2}[ \t]+"
    r"\d{2}:\d{2}:\d{2}[ \t]+(?P<host>" + _FQDN + r")[ \t]+[\w.\-/]+(?:\[\d+\])?:", re.MULTILINE)
_SYSLOG5424_RX = re.compile(
    r"^<\d{1,3}>1[ \t]+\S+[ \t]+(?P<host>" + _FQDN + r")[ \t]+\S+", re.MULTILINE)
# CN= di un computer in Active Directory: seguito da OU= o CN=, e il valore ha
# la forma di un identificativo (CN=Mario Rossi e CN=Administrator non passano)
_DN_CN_RX = re.compile(
    r"(?<![\w\-])CN=(?P<host>[A-Za-z][A-Za-z0-9\-]{1,62})(?=,[ \t]*(?:OU|CN)=)", re.IGNORECASE)

# parola chiave + identificativo. Tra le due al massimo tre parole di raccordo
# ("server di posta mail01", "la macchina virtuale di test vm-42").
_HOST_KW = (
    r"host[ \-]?names?|nom[ei][ \-]host|nom[ei][ \-](?:computer|macchin[ae]|server|dispositiv[oi]|nod[oi]|pc)"
    r"|(?:computer|machine|server|device|netbios|node|vm)[ \-]?names?"
    r"|hosts?|servers?|srv|workstations?|postazion[ei]|macchin[ae]|nod[oi]|nodes?|vms?"
    r"|virtual[ \-]machines?|macchin[ae][ \-]virtual[ei]|computers?|pc|desktops?|laptops?|notebooks?"
    r"|domain[ \-]controllers?|controller[ \-]di[ \-]dominio|hypervisor|nas|firewall|router|switch"
    r"|stampant[ei]|printers?|jump[ \-]?hosts?|jump[ \-]?servers?|bastion|proxy|gateway|appliance"
    r"|clusters?|endpoints?|storage|san|agent|sensor|sonda"
    + "|" + _lx.alt(*_lx.HOST_KW.values()))                   # fr/de/es/nl: lexicon.py
_HOST_FILLER = (
    r"di|del|della|dello|dell'|dei|degli|delle|of|the|for|per|su|on|a|al|alla|in|primari[oa]|primary"
    r"|secondari[oa]|secondary|principale|nuov[oa]|vecchi[oa]|new|old|posta|mail|backup|file|web"
    r"|database|db|dns|dhcp|print|stampa|dominio|domain|produzione|production|prod|test|staging"
    r"|collaudo|sviluppo|dev|virtuale|virtual|fisic[oa]|physical|linux|windows|remot[oa]|remote"
    r"|locale|local|intern[oa]|internal|estern[oa]|external|compromess[oa]|compromised|infett[oa]"
    r"|infected|colpit[oa]|affected|interessat[oa]|coinvolt[oa]|involved|chiamat[oa]|named|called"
    r"|denominat[oa]|di[ \t]nome|è|e'|is|was|era"
    + "|" + _lx.alt(*_lx.HOST_FILLER.values()))
_HOST_KW_RX = re.compile(
    r"(?<![\w\-])[\"'`]?(?P<kw>" + _HOST_KW + r")(?![\w\-])[\"'`]?"
    r"(?:[ \t]+(?:" + _HOST_FILLER + r")(?![\w\-])){0,3}"
    r"(?:[ \t]*[:=][ \t]*|[ \t]+)[\"'`«]?(?P<host>" + _FQDN + r")(?![\w\-]|\.\w)",
    re.IGNORECASE)
# continuazione di un elenco: "host srv01, srv02 e srv03"
_HOST_LIST_RX = re.compile(
    r"[ \t]*(?:,|;|/|\be\b|\band\b|\bo\b|\bor\b|" + _lx.alt(_lx.HOST_LIST_CONJ) + r")[ \t]*[\"'`«]?"
    r"(?P<host>" + _FQDN + r")(?![\w\-]|\.\w)",
    re.IGNORECASE)
# la parola chiave subito prima è già un IP o una porta ("host 10.0.0.1:22 srv"):
# non si entra qui, l'IP lo prende cyber.py e "srv" da solo non ha forma.

# valori che hanno la forma di un identificativo ma non sono nomi di macchina:
# sistemi operativi e prodotti con la versione attaccata, sigle tecniche con un
# numero, modelli di hardware, ticket, architetture
_HOST_STOP_RX = re.compile(
    r"^(?:(?:windows|win|ubuntu|debian|centos|rhel|rocky|alma|fedora|suse|sles|esxi|vsphere|vcenter"
    r"|sql|office|exchange|sharepoint|server|http|https|ipv|ip|utf|iso|rfc|cve|tcp|udp|md|sha|aes|rsa"
    r"|wpa|dns|dhcp|ntp|smtp|imap|pop|ldap|ssl|tls|ssh|rdp|smb|cifs|nfs|vlan|wifi|wi-fi|usb|hdmi|pci|pcie"
    r"|sata|nvme|ddr|gpu|cpu|ram|gb|tb|mb|kb|v|ver|version|versione|rev|build|release|patch|php|python"
    r"|java|node|angular|react|vue|dotnet|net|log4j|openssl|openssh|nginx|apache|tomcat|mysql|postgres"
    r"|mongodb|redis|kafka|docker|k8s|kubernetes|helm|oracle|db2|android|ios|macos|osx|win|x|i|amd|arm"
    r"|core|ryzen|xeon|epyc|pentium|celeron|snapdragon|apple|iphone|ipad|galaxy|pixel|s|a|m|h|b|e|g|p|t"
    r"|inc|req|chg|prb|ritm|task|sr|tkt|ticket|case|bug|issue|pr|mr|jira|kb|ms|cvss|owasp|nist|pci-dss"
    r"|gdpr|iso-|en|din|uni|art|articolo|comma|pag|pagina|fig|tab|tabella|cap|capitolo|sez|par|nr|n)"
    r"-?\d+[a-z0-9.\-]*"
    r"|(?:dl|ml|bl|r|t|c|n|e|x|xps|latitude|precision|optiplex|thinkpad|elitebook|probook|proliant"
    r"|poweredge|catalyst|nexus|fortigate|fortinet|palo|pa|asa|isr|asr|mx|srx|ex)-?\d{2,5}[a-z]{0,3}(?:-\S+)?"
    r"|(?:i[3579]|e[3-7]|r[3579]|m[1-4])-\S+"
    r"|x86(?:-64)?|x64|amd64|arm64|aarch64|i386|i686|utf-?8|iso-?8859-?\d*|wi-?fi|ipv[46]|802\.\S+"
    r"|covid-?19|h[1-9]n[1-9]|e-mail|e-commerce|2fa|mfa|3d|4k|5g|4g|3g|2g|o365|m365|e[1-5]|f[1-9]|g[1-9]"
    r"|p[1-4]|s[1-4]|l[1-3]|t[1-3]|r[1-3]|a[1-4]|b[1-3]|c[1-3]|d[1-3]|k[1-3]|z[1-3]|q[1-4]|w[1-3])$",
    re.IGNORECASE)


def _host_shape_ok(host):
    """Il valore ha la forma di un nome di macchina (e non di una parola)?"""
    if not 3 <= len(host) <= 253 or not re.search(r"[A-Za-z]", host):
        return False
    if not host[0].isalpha():
        return False
    labels = host.split(".")
    if len(labels) > 1:
        if labels[-1].lower() in _PUBLIC_TLD:
            return False                     # è un URL: lo tagga detectors.py
        if not re.fullmatch(_INTERNAL_TLD, labels[-1], re.IGNORECASE) and len(labels[-1]) <= 3 \
                and not re.search(r"\d", host.replace(".", "")) and "-" not in host:
            # "p.iva", "S.r.l", "n.ro": abbreviazioni, non nomi
            return False
    if not re.search(r"\d", host) and "-" not in host and "." not in host:
        return False                         # parola nuda
    return not _HOST_STOP_RX.match(host)


def _cli_host_ok(text, cmd_start, cmd, host):
    """In riga di comando anche una parola nuda è un host ("ssh bastion"), ma
    solo se il comando sta a inizio riga o dopo un prompt/separatore di shell,
    e non è anche una parola inglese ("dig deeper", "nc"); nella prosa
    ("l'accesso ssh al bastion") vale la forma dell'identificativo."""
    if _host_shape_ok(host):
        return True
    if cmd.lower() not in _CLI_PLAIN_OK:
        return False
    if not 2 <= len(host) <= 63 or not host[0].isalpha() or host.lower() in _CLI_STOP or "." in host:
        return False
    before = text[max(0, cmd_start - 24):cmd_start]
    return bool(re.search(r"(?:^|[\n\r$#>`;|]|&&|\|\||\bsudo|\bthen|\bdo|\bexec|\brun|\bxargs)[ \t]*$", before))


_CLI_PLAIN_OK = frozenset("""
ssh ssh-copy-id sftp scp rsync ping ping6 traceroute traceroute6 tracert nslookup telnet nmap
test-connection test-netconnection enter-pssession invoke-command mstsc xfreerdp wfreerdp psexec paexec
""".split())


_CLI_STOP = frozenset("""
al alla allo a in di da su per con the to into on via from and or key keys config tunnel access
accesso chiave chiavi server client daemon agent session sessione port porta login command comando
connection connessione error errore failed timeout me it him them test localhost host hosts target
options option help version verbose quiet remote local user utente password passwd pass
au à vers sur avec pour zum zur auf nach über mit für al hacia para con naar met op voor
""".split())


def _anchored_host_ok(host):
    """Dentro un'ancora forte (UNC, prompt, syslog) basta che sia un nome e non
    un IP, localhost o un dominio pubblico (quello è un URL)."""
    if not re.search(r"[A-Za-z]", host) or host.lower() in ("localhost", "localhost.localdomain"):
        return False
    if "." in host and host.rsplit(".", 1)[1].lower() in _PUBLIC_TLD:
        return False
    return not _HOST_STOP_RX.match(host)


def _hostname_hits(text):
    out = []

    def add(prio, s, e, host):
        out.append((prio, s, e, "HOSTNAME", False, host))

    for m in _FQDN_INTERNAL_RX.finditer(text):
        add(3, m.start("host"), m.end("host"), m.group("host"))
    for m in _UNC_RX.finditer(text):
        h = m.group("host")
        if _anchored_host_ok(h):
            add(3, m.start("host"), m.end("host"), h)
    for m in _WIN_DEFAULT_RX.finditer(text):
        h = m.group("host")
        if h.split("-", 1)[1].lower() not in ("amd64", "arm64", "x64", "x86", "i386", "win32", "win64", "utf8"):
            add(3, m.start("host"), m.end("host"), h)
    for rx in (_SSH_LIKE_RX, _SCP_RX, _RDP_RX, _PSEXEC_RX):
        for m in rx.finditer(text):
            h = m.group("host")
            if _cli_host_ok(text, m.start("cmd"), m.group("cmd").split()[0], h):
                add(3, m.start("host"), m.end("host"), h)
    for m in _PROMPT_RX.finditer(text):
        h = m.group("host")
        if _anchored_host_ok(h):
            add(3, m.start("host"), m.end("host"), h)
    for rx in (_SYSLOG_RX, _SYSLOG5424_RX):
        for m in rx.finditer(text):
            h = m.group("host")
            if _anchored_host_ok(h):
                add(3, m.start("host"), m.end("host"), h)
    for m in _DN_CN_RX.finditer(text):
        h = m.group("host")
        if (re.search(r"\d", h) or "-" in h) and not _HOST_STOP_RX.match(h):
            add(3, m.start("host"), m.end("host"), h)
    for m in _HOST_KW_RX.finditer(text):
        h = m.group("host")
        if not _host_shape_ok(h):
            continue
        add(2, m.start("host"), m.end("host"), h)
        pos = m.end("host")
        while True:
            lm = _HOST_LIST_RX.match(text, pos)
            if not lm or not _host_shape_ok(lm.group("host")):
                break
            add(2, lm.start("host"), lm.end("host"), lm.group("host"))
            pos = lm.end("host")
    return out


# ----------------------------------------------------------------------------
# DEVICE_ID
# ----------------------------------------------------------------------------
# IMEI: 15 cifre (16 con la versione software) compatte o a gruppi 2-6-6-1 / 8-6-1
# ai bordi niente cifre né "separatore + cifra" (2.353918107123453, 353918107123453-1),
# ma il punto che chiude la frase va bene
_IMEI_RX = re.compile(
    r"(?<!\d)(?<!\d[\-.])(?P<v>\d{2}(?P<sep>[ \-.]?)\d{6}(?P=sep)\d{6}(?P=sep)\d{1,2}"
    r"|\d{8}(?P<sep2>[ \-.]?)\d{6}(?P=sep2)\d{1,2})(?!\d|[\-.]\d)")
_IMEI_CUE_RX = re.compile(r"\bIMEI(?:SV)?\b", re.IGNORECASE)
# ICCID: 18-22 cifre, sempre "89" (telecomunicazioni, ISO 7812), compatte o a gruppi
_ICCID_RX = re.compile(r"(?<!\d)(?<!\d-)(?P<v>89(?:[ \-]?\d){16,20})(?!\d|-\d)")
_ICCID_CUE_RX = re.compile(r"\b(?:ICCID|SIM|eSIM|scheda|" + _lx.alt(_lx.ICCID_CUE) + r")\b", re.IGNORECASE)
_IMSI_RX = re.compile(r"(?<![\w\-])IMSI\b[^\n\d]{0,20}(?P<v>[2-7]\d{14})(?!\d)", re.IGNORECASE)

_SERIAL_KW = (
    r"serial(?:[ \-]?(?:number|numbers|no\.?|nr\.?|num\.?|n\.?|#))?|(?-i:S/?N)"
    r"|numer[oi][ \t]+di[ \t]+serie|n(?:\.|umero)?[ \t]*(?:di[ \t]+)?serie|serial[ei]"
    r"|matricol[ae][ \t]+(?:del(?:la|lo|l')?[ \t]*)?(?:dispositiv[oi]|device|pc|portatil[ei]|laptop|notebook"
    r"|server|telefon[oi]|smartphone|tablet|stampant[ei]|apparat[oi]|switch|router|firewall|nas"
    r"|access[ \t]?point|terminal[ei]|macchin[ae])"
    r"|service[ \-]?tag|express[ \t]service[ \t]code|asset[ \-]?(?:tag|id|number)"
    r"|inventory[ \-]?(?:tag|id|number)|numer[oi][ \t]+(?:di[ \t]+)?inventario"
    + "|" + _lx.alt(*_lx.SERIAL_KW.values()))
_SERIAL_FILLER = (
    r"del|della|dello|dell'|dei|delle|di|of|the|is|è|e'|era|was|risulta|seguente|following"
    r"|dispositivo|device|pc|portatile|laptop|notebook|server|telefono|smartphone|tablet|stampante"
    r"|apparato|switch|router|firewall|nas|macchina|prodotto|product|unit|unità|sim|scheda|chassis"
    + "|" + _lx.alt(_lx.SERIAL_FILLER))
_SERIAL_RX = re.compile(
    r"(?<![\w\-/])(?P<kw>" + _SERIAL_KW + r")"
    r"(?:[ \t]+(?:" + _SERIAL_FILLER + r")){0,4}"
    r"[ \t]*[:=#]?[ \t]*[\"'`«]?(?P<v>[A-Za-z0-9](?:[A-Za-z0-9\-/]{4,38})[A-Za-z0-9])(?![A-Za-z0-9])",
    re.IGNORECASE)
# porte e standard che seguono la parola "seriale"/"serial": non sono numeri di serie
_SERIAL_STOP_RX = re.compile(r"^(?:rs-?\d{3}|com\d+|usb\d*|tty\S*|ata|sata|\d{1,6}|[a-z]+)$", re.IGNORECASE)
_PKEY_SHAPE_RX = re.compile(r"^(?:[A-Z0-9]{5}-){4}[A-Z0-9]{5}$")

_DEVID_KW = (
    r"udid|meid|esn|device[ \-]?id|id[ \t]+(?:del[ \t]+)?dispositivo|identificativo[ \t]+(?:del[ \t]+)?dispositivo"
    r"|android[ \-]?id|hardware[ \-]?id|hwid|machine[ \-]?id|intune[ \t]+device[ \t]+id|azure[ \t]+ad[ \t]+device[ \t]+id"
    r"|aad[ \t]+device[ \t]+id|ms-?ppid|installation[ \-]?id|idfa|idfv|gaid|advertising[ \-]?id"
    + "|" + _lx.alt(_lx.DEVID_KW))
_DEVID_RX = re.compile(
    r"(?<![\w\-/])(?P<kw>" + _DEVID_KW + r")[ \t]*[:=#]?[ \t]*[\"'`«]?"
    r"(?P<v>[A-Za-z0-9](?:[A-Za-z0-9\-]{6,62})[A-Za-z0-9])(?![A-Za-z0-9\-])",
    re.IGNORECASE)


def _cue_before(rx, text, start, window=40):
    return rx.search(text, max(0, start - window), start) is not None


def _device_hits(text):
    out = []

    def add(prio, s, e, validated):
        out.append((prio, s, e, "DEVICE_ID", validated, text[s:e]))

    for m in _IMEI_RX.finditer(text):
        digits = re.sub(r"\D", "", m.group("v"))
        cue = _cue_before(_IMEI_CUE_RX, text, m.start("v"))
        if len(digits) == 15:
            ok = _luhn(digits)
            if ok and digits[:2] in IMEI_RBI:
                add(4, m.start("v"), m.end("v"), True)
            elif cue:
                add(4, m.start("v"), m.end("v"), ok)
        elif cue:                            # IMEISV: 16 cifre, nessun checksum
            add(4, m.start("v"), m.end("v"), False)
    for m in _ICCID_RX.finditer(text):
        digits = re.sub(r"\D", "", m.group("v"))
        if _luhn(digits):
            add(4, m.start("v"), m.end("v"), True)
        elif _cue_before(_ICCID_CUE_RX, text, m.start("v")):
            add(4, m.start("v"), m.end("v"), False)
    for m in _IMSI_RX.finditer(text):
        add(4, m.start("v"), m.end("v"), False)
    for m in _SERIAL_RX.finditer(text):
        v = m.group("v")
        if not re.search(r"\d", v) or _SERIAL_STOP_RX.match(v):
            continue
        if v.isdigit() and len(v) < 7:
            continue
        # "serial" nel senso di licenza: la chiave 5x5 è PRODUCT_KEY (cyber.py)
        if _PKEY_SHAPE_RX.match(v):
            continue
        add(3, m.start("v"), m.end("v"), False)
    for m in _DEVID_RX.finditer(text):
        v = m.group("v")
        if not re.search(r"\d", v):
            continue
        add(3, m.start("v"), m.end("v"), False)
    return out


# ----------------------------------------------------------------------------
def _resolve(hits):
    """Senza sovrapposizioni: priorità, poi lunghezza."""
    kept = []
    for h in sorted(hits, key=lambda h: (-h[0], -(h[2] - h[1]), h[1])):
        prio, s, e = h[0], h[1], h[2]
        if e <= s:
            continue
        if any(s < ke and e > ks for _, ks, ke, *_ in kept):
            continue
        kept.append(h)
    return sorted(kept, key=lambda h: h[1])


def _propagable(label, val):
    if label == "HOSTNAME":
        if _host_shape_ok(val):
            return True
        return len(val) >= 6 and val[0].isalpha() and val.lower() not in _CLI_STOP
    return len(re.sub(r"[^A-Za-z0-9]", "", val)) >= 6


def _propagate(text, hits):
    """Le altre occorrenze (case-insensitive, a confine di parola) dei valori già
    ancorati; di un FQDN anche la prima etichetta da sola, se ha forma di nome
    ("srv01.corp.lan" -> "srv01"). Priorità minima: non scalzano nessuna ancora."""
    values = {}
    for _, _, _, label, _, val in hits:
        if _propagable(label, val):
            values[(label, val.casefold())] = val
        if label == "HOSTNAME" and "." in val:
            first = val.split(".", 1)[0]
            if _host_shape_ok(first):
                values[(label, first.casefold())] = first
    out = []
    for (label, _), val in values.items():
        rx = re.compile(r"(?<![\w\-])" + re.escape(val) + r"(?![\w\-]|\.\w)", re.IGNORECASE)
        for m in rx.finditer(text):
            out.append((1, m.start(), m.end(), label, False, m.group(0)))
    return out


@normalized_detector
def detect_devices(text):
    """Entità HOSTNAME e DEVICE_ID, nella forma di `detect_regex`."""
    if not text:
        return []
    hits = _hostname_hits(text) + _device_hits(text)
    hits += _propagate(text, hits)
    return [{
        "label": label,
        "start": s,
        "end": e,
        "score": 1.0,
        "validated": validated,
        "source": "regex",
    } for _, s, e, label, validated, _ in _resolve(hits)]
