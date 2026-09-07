"""
Identificativi tecnici del gruppo «Cybersecurity»: rete regex di FORMATO, senza
modello. Dieci label:

  IP_ADDRESS       IPv4 (anche CIDR) e IPv6 (anche zona e prefisso)
  MAC_ADDRESS      sei coppie hex con ":" o "-", oppure tre gruppi Cisco con "."
  PASSWORD_HASH    modular crypt format ($2b$, $6$, $argon2id$...), LDAP {SSHA},
                   Django, MySQL, coppia LM:NT, hash hex con parola chiave
  TOTP_SECRET      URI otpauth:// e seed base32 in contesto 2FA/authenticator
  SSH_KEY          chiavi pubbliche OpenSSH e blocchi SSH2 PUBLIC KEY
  SSH_FINGERPRINT  SHA256:<43 base64> e sedici coppie hex MD5
  CERTIFICATE      blocchi PEM pubblici (CERTIFICATE, REQUEST, PUBLIC KEY, CRL, PKCS7)
  WINDOWS_SID      S-1-5-21-... e S-1-12-1-... (account di dominio / Azure AD)
  CRYPTO_WALLET    Bitcoin & co. base58check, bech32/bech32m, Ethereum (EIP-55)
                   (una chiave privata WIF, base58check a 37/38 byte, esce come SECRET)
  PRODUCT_KEY      chiavi Windows/Office 5x5

Sono tutti dati con una forma inconfondibile, e dove un checksum esiste lo si
verifica (base58check, bech32, EIP-55, decodifica del blob SSH e del DER):
`validated=True` vuol dire proprio questo, come per IBAN e carte. Dove il
checksum non c'è la precisione viene dal formato e dalle esclusioni (loopback,
netmask, SID ben noti, segnaposto XXXXX-XXXXX). Gli hash hex nudi restano in
chiaro: non sono segreti e senza contesto sono indistinguibili da qualsiasi
altro esadecimale.

Come `credentials.py`, il modulo lavora su stringhe e non importa torch/fitz:
`detectors.detect_regex` ne consuma `detect_cyber`. Le label stanno tutte in
`detectors.EXACT_SPAN_LABELS`: un hash finisce spesso con "." o "=", un MAC è
case-sensitive nel registro della chat, quindi niente allargamento ai confini
di parola, niente taglio della punteggiatura, confronto esatto.
"""

import base64
import hashlib
import ipaddress
import re

from .text_patterns import normalized_detector

from . import lexicon as _lx

CYBER_LABELS = frozenset({
    "IP_ADDRESS", "MAC_ADDRESS", "PASSWORD_HASH", "TOTP_SECRET", "SSH_KEY",
    "SSH_FINGERPRINT", "CERTIFICATE", "WINDOWS_SID", "CRYPTO_WALLET", "PRODUCT_KEY",
})

_TRAIL = ".,;:)]}»\"'"


# ----------------------------------------------------------------------------
# IP_ADDRESS
# ----------------------------------------------------------------------------
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4_RX = re.compile(
    r"(?<![\w.:\-])" + _OCTET + r"(?:\." + _OCTET + r"){3}"
    r"(?:/(?:3[0-2]|[12]?\d))?"
    r"(?![\w]|\.\w)")
# "version 2.4.1.0", "kernel al 6.12.0.1", "Chrome 128.0.6613.84": un numero di
# versione a quattro campi ha la stessa forma di un IPv4. Lo dice la parola prima,
# anche con una o due parole di raccordo in mezzo ("Nginx è alla versione ...").
_VERSION_BEFORE_RX = re.compile(
    r"(?:\b(?:v|ver|vers|version|versione|versi[óo]n|versie|release|rel|build|firmware|fw|kernel"
    r"|python|node|java|php|openssl|nginx|apache|chrome|firefox|edge|safari"
    r"|windows|ubuntu|debian|centos|rhel|ios|android|macos|tomcat|mysql|postgres"
    r"|postgresql|docker|kubernetes|k8s|helm|go|golang|rust|ruby|perl|dotnet|jdk"
    r"|jre|sdk|ndk|bios|uefi)\.?(?:[ \t]+(?:al|alla|all'|a|la|il|in|di|del|della|e|è|is|at|to|the)){0,2}"
    r"|\bv)[ \t]*$",
    re.IGNORECASE)
_IPV6_RX = re.compile(
    r"(?<![\w:])(?P<ip>(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:\.\d{1,3}){0,3})"
    r"(?:%(?P<zone>[\w.\-]+))?(?:/(?P<pfx>\d{1,3}))?(?![\w:])")


def _ipv4_hits(text):
    out = []
    for m in _IPV4_RX.finditer(text):
        raw = m.group(0)
        if _VERSION_BEFORE_RX.search(text, max(0, m.start() - 24), m.start()):
            continue
        try:
            addr = ipaddress.IPv4Address(raw.split("/", 1)[0])
        except ValueError:
            continue
        first = int(addr) >> 24
        # 0.0.0.0, 127.x (loopback), 224+ (multicast, riservati, 255.x netmask
        # e broadcast): non identificano nessuno e servono all'LLM in chiaro
        if first == 0 or first == 127 or first >= 224:
            continue
        out.append((3, m.start(), m.end(), "IP_ADDRESS", False))
    return out


def _ipv6_hits(text):
    out = []
    for m in _IPV6_RX.finditer(text):
        raw = m.group("ip")
        if raw.count(":") < 2:
            continue
        end = m.end()
        try:
            addr = ipaddress.IPv6Address(raw)
        except ValueError:
            # "2001:db8::1:" a fine frase: il due punti finale non è suo
            if raw.endswith(":") and not raw.endswith("::"):
                try:
                    addr = ipaddress.IPv6Address(raw[:-1])
                    end = m.start() + len(raw) - 1
                except ValueError:
                    continue
            else:
                continue
        if addr.is_loopback or addr.is_unspecified:
            continue
        # An invalid prefix is not part of the address. Match the address only,
        # consistently with IPv4, while keeping the malformed suffix visible.
        if m.group("pfx") and int(m.group("pfx")) > 128:
            end = min(end, m.start("pfx") - 1)
        # senza "::" servono tutti e otto i gruppi: "12:30:45" o un MAC a sei
        # gruppi non passano il parser, ma "1:2:3:4:5:6:7:8" sì ed è corretto
        out.append((3, m.start(), end, "IP_ADDRESS", False))
    return out


# ----------------------------------------------------------------------------
# MAC_ADDRESS
# ----------------------------------------------------------------------------
_MAC_RX = re.compile(
    r"(?<![0-9A-Fa-f:\-.])(?:[0-9A-Fa-f]{2}(?P<sep>[:\-])(?:[0-9A-Fa-f]{2}(?P=sep)){4}[0-9A-Fa-f]{2}"
    r"|[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4})(?![0-9A-Fa-f:\-]|\.[0-9A-Fa-f])")


def _mac_hits(text):
    out = []
    for m in _MAC_RX.finditer(text):
        raw = m.group(0)
        hexs = re.sub(r"[^0-9A-Fa-f]", "", raw).lower()
        if hexs in ("000000000000", "ffffffffffff"):
            continue
        # un MAC "in cifre" va bene ("00-11-22-33-44-55"); una data o un orario
        # non hanno mai sei gruppi, quindi non serve altro
        out.append((3, m.start(), m.end(), "MAC_ADDRESS", False))
    return out


# ----------------------------------------------------------------------------
# PASSWORD_HASH
# ----------------------------------------------------------------------------
# Modular crypt format: $id$[param$]salt$hash. Gli id sono quelli di crypt(3),
# passlib, hashcat e dei principali framework; un id ignoto non basta ("$5$" in
# una formula LaTeX è un prezzo, non un hash).
_MCF_IDS = (r"1|2[abxy]?|3|5|6|7|y|gy|P|H|S|apr1|md5|sha1|sha256|sha512|argon2id?|argon2d"
            r"|scrypt|bcrypt-sha256|pbkdf2(?:-sha(?:1|256|512))?|ssha(?:256|512)?|sm3"
            r"|krb5tgs|krb5asrep|DCC2|NT|LM|ml|md5crypt|sha512crypt")
_MCF_RX = re.compile(
    r"(?<![\w$])\$(?P<id>" + _MCF_IDS + r")\$"
    r"(?P<body>[A-Za-z0-9./+=,\-_*]+(?:\$[A-Za-z0-9./+=,\-_*]+)*)")
# lunghezza esatta dell'ultimo campo dove il formato la fissa: così un "." di
# fine frase non viene inglobato ("...l'hash è $6$salt$xxxx.")
_MCF_FIXED_LAST = {"1": 22, "apr1": 22, "md5": 22, "5": 43, "6": 86, "2": 53,
                   "2a": 53, "2b": 53, "2x": 53, "2y": 53, "NT": 32, "LM": 32}
_LDAP_HASH_RX = re.compile(
    r"(?<![\w{])\{(?:S?SHA(?:1|224|256|384|512)?|S?MD5|CRYPT|PBKDF2(?:[-_]SHA\d+)?"
    r"|ARGON2|BCRYPT|SCRYPT)\}(?P<val>[A-Za-z0-9+/=$.]{16,})", re.IGNORECASE)
_DJANGO_HASH_RX = re.compile(
    r"(?<![\w$])(?:pbkdf2_sha(?:1|256|512)|scrypt|sha1|md5|unsalted_sha1|unsalted_md5)"
    r"\$[0-9]{1,8}\$[^\s$]{1,64}\$[A-Za-z0-9+/=]{16,}")
_MYSQL_HASH_RX = re.compile(r"(?<![\w*])\*[0-9A-F]{40}(?![0-9A-Za-z])")
# la coppia LM:NT sta in una riga pwdump "utente:RID:LM:NT:::": i due punti
# intorno sono suoi vicini legittimi, un carattere hex no
_LMNT_RX = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32}:[0-9A-Fa-f]{32}(?![0-9A-Fa-f])")
# hash hex NUDO: solo con la parola chiave che lo dichiara hash di password
_KW_HASH_RX = re.compile(
    r"(?<![\w\-])(?:(?:nt|lm|ntlm|net-?ntlmv?2|md5|sha-?1|sha-?256|sha-?512|password|pwd|pass"
    r"|passwd|passwort|wachtwoord|contrase[ñn]a|mot[ _-]de[ _-]passe)[ _\-]?hash|hash[ _\-]?(?:nt|lm|ntlm|md5|sha-?1|sha-?256|sha-?512)"
    r"|hash[ _\-]*(?:(?:della|of the|of|du|de la|des|van het)[ _\-]+)?"
    r"(?:password|passwort|wachtwoord|contrase[ñn]a|mot[ _-]de[ _-]passe)|ntlm|nthash|lmhash)"
    r"[^\n]{0,20}?[:=]?[ \t]*[\"'`]?"
    r"(?P<val>(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{32}|[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}|[0-9A-Fa-f]{128})(?![0-9A-Za-z]))",
    re.IGNORECASE)


def _hash_hits(text):
    out = []
    for m in _MCF_RX.finditer(text):
        ident = m.group("id")
        body = m.group("body")
        segs = body.split("$")
        fixed = _MCF_FIXED_LAST.get(ident)
        if fixed is not None:
            if len(segs[-1]) < fixed:
                continue
            segs[-1] = segs[-1][:fixed]
            body = "$".join(segs)
        else:
            # la punteggiatura di fine frase non appartiene all'hash; se il "."
            # era davvero l'ultimo carattere dell'hash si maschera un carattere
            # in meno, che da solo non rivela nulla
            body = body.rstrip(".,-")
        end = m.start("body") + len(body)
        if end - m.start() < 16 or not re.search(r"[A-Za-z0-9]{8}", body):
            continue
        out.append((4, m.start(), end, "PASSWORD_HASH", False))
    for m in _LDAP_HASH_RX.finditer(text):
        val = m.group("val").rstrip(".")
        out.append((4, m.start(), m.start("val") + len(val), "PASSWORD_HASH", False))
    for m in _DJANGO_HASH_RX.finditer(text):
        out.append((4, m.start(), m.end(), "PASSWORD_HASH", False))
    for m in _MYSQL_HASH_RX.finditer(text):
        out.append((4, m.start(), m.end(), "PASSWORD_HASH", False))
    for m in _LMNT_RX.finditer(text):
        out.append((4, m.start(), m.end(), "PASSWORD_HASH", False))
    for m in _KW_HASH_RX.finditer(text):
        out.append((4, m.start("val"), m.end("val"), "PASSWORD_HASH", False))
    return out


# ----------------------------------------------------------------------------
# TOTP_SECRET
# ----------------------------------------------------------------------------
_OTPAUTH_RX = re.compile(r"(?<![\w.])otpauth://[th]otp/[^\s\"'<>]*secret=[A-Za-z2-7]{16,}[^\s\"'<>]*",
                         re.IGNORECASE)
# la coda di 2-3 caratteri copre i seed a 26 caratteri, ma solo attaccata: con lo
# spazio davanti prenderebbe la parola dopo ("...3PXP era")
_B32_RX = re.compile(
    r"(?<![A-Za-z0-9])(?P<val>[A-Za-z2-7]{4}(?:[ \-]?[A-Za-z2-7]{4}){3,15}[A-Za-z2-7]{0,3}={0,6})"
    r"(?![A-Za-z0-9=])")
_TFA_CUE_RX = re.compile(
    r"(?:\b(?:2fa|mfa|totp|hotp|otp|authenticator|authy|autenticazione|autenticatore"
    r"|two[ \-]?factor|2[ \-]?factor|due fattori|due passaggi|two[ \-]step|2[ \-]step"
    r"|verification code app|codice di verifica|seed|base ?32|secondo fattore|second factor"
    r"|qr(?: ?code)?|setup key|chiave di configurazione"
    r"|" + _lx.alt(_lx.TFA_CUE) + r")\b)", re.IGNORECASE)      # fr/de/es/nl: lexicon.py


def _totp_hits(text):
    out = []
    for m in _OTPAUTH_RX.finditer(text):
        end = m.end()
        while end > m.start() and text[end - 1] in _TRAIL:
            end -= 1
        out.append((5, m.start(), end, "TOTP_SECRET", False))
    for m in _B32_RX.finditer(text):
        val, s, e = m.group("val"), m.start(), m.end()
        # a gruppi ("JBSW Y3DP EHPK 3PXP"): tutti della stessa lunghezza (l'ultimo
        # può essere più corto) e dello stesso caso. Una parola davanti al seed
        # ("tweestapsverificatie JBSWY3DPEHPK3PXP") è fatta di lettere base32 e
        # la regex la inghiotte: allora il seed è il solo ultimo gruppo
        groups = [g for g in re.split(r"[ \-]", val.rstrip("=")) if g]
        if len(groups) > 1 and (len({len(g) for g in groups[:-1]}) > 1 or len(groups[-1]) > len(groups[0])
                                or (any(g.islower() for g in groups) and any(g.isupper() for g in groups))):
            val = groups[-1]
            s = m.start() + m.group("val").rstrip("=").rfind(val)
            e = s + len(val)
        core_val = re.sub(r"[ \-=]", "", val)
        if len(core_val) < 16:
            continue
        window = text[max(0, s - 100):s]
        if "\n\n" in window[-60:] or not _TFA_CUE_RX.search(window):
            continue
        # tutto maiuscolo e in un pezzo è la forma canonica (JBSWY3DPEHPK3PXP);
        # minuscolo o a gruppi di quattro potrebbe essere una frase di parole di
        # quattro lettere: serve almeno una cifra
        canonical = val.isupper() and " " not in val and "-" not in val
        if not canonical and not re.search(r"[2-7]", core_val):
            continue
        if not re.search(r"[A-Za-z]", core_val):
            continue
        out.append((4, s, e, "TOTP_SECRET", False))
    return out


# ----------------------------------------------------------------------------
# SSH_KEY / SSH_FINGERPRINT
# ----------------------------------------------------------------------------
_SSH_TYPES = (r"ssh-(?:rsa|dss|ed25519|ed448)|ecdsa-sha2-nistp(?:256|384|521)"
              r"|sk-(?:ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com|rsa-sha2-(?:256|512)")
# il blob base64 inizia con la lunghezza e il nome del tipo: "AAAAB3NzaC1yc2E"
# è "ssh-rsa", "AAAAC3NzaC1lZDI1NTE5" è "ssh-ed25519", "AAAAE2VjZHNh" è ecdsa
_SSH_KEY_RX = re.compile(
    r"(?:(?<![\w\-])(?:" + _SSH_TYPES + r")[ \t]+)?"
    r"(?<![A-Za-z0-9+/])(?P<blob>AAAA(?:B3NzaC1yc2E|C3NzaC1lZDI1NTE5|E2VjZHNh|B3NzaC1kc3M|GnNrLXNzaC1lZDI1NTE5|InNrLWVjZHNh)"
    r"[A-Za-z0-9+/]{20,}={0,3})"
    r"(?P<comment>[ \t]+[^\s@]{1,64}@[^\s]{1,128})?")
_SSH2_BLOCK_RX = re.compile(
    r"---- BEGIN SSH2 PUBLIC KEY ----(?:[A-Za-z0-9+/=\s:\"\-]|\\n){20,}?---- END SSH2 PUBLIC KEY ----")
_SSH_FP_SHA_RX = re.compile(r"(?<![A-Za-z0-9+/])SHA256:[A-Za-z0-9+/]{43}(?![A-Za-z0-9+/=])")
_SSH_FP_MD5_RX = re.compile(r"(?<![0-9A-Fa-f:])(?:MD5:)?(?:[0-9a-f]{2}:){15}[0-9a-f]{2}(?![0-9A-Fa-f:])",
                            re.IGNORECASE)


def _b64(s):
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
    except Exception:
        return None


def _ssh_blob_ok(blob):
    raw = _b64(blob)
    if not raw or len(raw) < 12:
        return False
    n = int.from_bytes(raw[:4], "big")
    if not 7 <= n <= 64 or len(raw) < 4 + n:
        return False
    name = raw[4:4 + n]
    return bool(re.fullmatch(rb"(?:ssh-|ecdsa-|sk-|rsa-)[a-z0-9@.\-]+", name))


def _ssh_hits(text):
    out = []
    for m in _SSH_KEY_RX.finditer(text):
        ok = _ssh_blob_ok(m.group("blob"))
        end = m.end("comment") if m.group("comment") else m.end("blob")
        while end > m.start() and text[end - 1] in _TRAIL:
            end -= 1
        # senza il prefisso "ssh-..." e senza decodifica valida è solo base64
        if not ok and m.start("blob") == m.start():
            continue
        out.append((5, m.start(), end, "SSH_KEY", ok))
    for m in _SSH2_BLOCK_RX.finditer(text):
        out.append((5, m.start(), m.end(), "SSH_KEY", False))
    for m in _SSH_FP_SHA_RX.finditer(text):
        raw = _b64(m.group(0)[7:])
        out.append((4, m.start(), m.end(), "SSH_FINGERPRINT", raw is not None and len(raw) == 32))
    for m in _SSH_FP_MD5_RX.finditer(text):
        out.append((4, m.start(), m.end(), "SSH_FINGERPRINT", False))
    return out


# ----------------------------------------------------------------------------
# CERTIFICATE (PEM pubblico; le chiavi private sono SECRET in credentials.py)
# ----------------------------------------------------------------------------
_PEM_PUBLIC_RX = re.compile(
    r"-----BEGIN (?P<kind>(?:TRUSTED |NEW )?CERTIFICATE(?: REQUEST)?|PUBLIC KEY|RSA PUBLIC KEY"
    r"|X509 CRL|PKCS7|CMS|ATTRIBUTE CERTIFICATE)-----"
    r"(?P<body>(?:[A-Za-z0-9+/=\s]|\\n){40,}?)"
    r"(?:-----END (?P=kind)-----|(?=\n[ \t]*\n)|\Z)")


def _cert_hits(text):
    out = []
    for m in _PEM_PUBLIC_RX.finditer(text):
        body = re.sub(r"\\n|\s", "", m.group("body"))
        raw = _b64(body)
        # DER: una SEQUENCE (0x30) con lunghezza lunga (0x82) apre ogni
        # certificato, CSR, SubjectPublicKeyInfo o CRL reale
        ok = raw is not None and len(raw) >= 64 and raw[0] == 0x30
        out.append((5, m.start(), m.end(), "CERTIFICATE", ok))
    return out


# ----------------------------------------------------------------------------
# WINDOWS_SID
# ----------------------------------------------------------------------------
# Solo i SID con l'identificatore di dominio/macchina (5-21) o di Azure AD
# (12-1): sono quelli che dicono "quale dominio, quale account". S-1-5-18,
# S-1-1-0, S-1-5-32-544 sono uguali su ogni macchina del mondo e restano in chiaro.
_SID_RX = re.compile(r"(?<![\w\-])S-1-(?:5-21|12-1)-\d{1,10}-\d{1,10}-\d{1,10}(?:-\d{1,10})?(?![\w\-])")


def _sid_hits(text):
    # validated: la forma S-1-5-21 + tre sotto-autorità è una prova di per sé, e
    # serve a battere la carta di credito che per caso passa il Luhn su
    # "1-5-21-1876949484" (validata anch'essa, poi decide la lunghezza)
    return [(4, m.start(), m.end(), "WINDOWS_SID", True) for m in _SID_RX.finditer(text)]


# ----------------------------------------------------------------------------
# CRYPTO_WALLET
# ----------------------------------------------------------------------------
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}
_B58_RX = re.compile(r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{25,35}(?![A-Za-z0-9])")
_BECH32_RX = re.compile(r"(?<![A-Za-z0-9])(?:bc|tb|ltc|bcrt)1[02-9ac-hj-np-z]{6,87}(?![A-Za-z0-9])",
                        re.IGNORECASE)
_ETH_RX = re.compile(r"(?<![A-Za-z0-9])0x[0-9a-fA-F]{40}(?![A-Za-z0-9])")
# chiave privata Bitcoin in formato WIF: 51 caratteri da "5" (non compressa) o 52
# da "K"/"L" (compressa); testnet "9"/"c". È materiale segreto, non un indirizzo.
_WIF_RX = re.compile(r"(?<![A-Za-z0-9])[5KL9c][1-9A-HJ-NP-Za-km-z]{50,51}(?![A-Za-z0-9])")
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _b58check_ok(s, sizes=(25,)):
    n = 0
    for c in s:
        n = n * 58 + _B58_INDEX[c]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    raw = b"\x00" * pad + raw
    if len(raw) not in sizes:
        return False
    digest = hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()
    return digest[:4] == raw[-4:]


def _bech32_polymod(values):
    gen = (0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1ffffff) << 5) ^ v
        for i in range(5):
            if (top >> i) & 1:
                chk ^= gen[i]
    return chk


def _bech32_ok(s):
    if s.lower() != s and s.upper() != s:
        return False
    s = s.lower()
    hrp, data = s.rsplit("1", 1)
    try:
        vals = [_BECH32_CHARSET.index(c) for c in data]
    except ValueError:
        return False
    if len(vals) < 6:
        return False
    exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    return _bech32_polymod(exp + vals) in (1, 0x2bc830a3)


# keccak-256 in puro Python (EIP-55 usa Keccak, NON sha3_256 di hashlib: il
# padding è diverso). Poche righe, nessuna dipendenza: l'indirizzo Ethereum a
# maiuscole miste diventa verificabile e un hex qualunque a 40 cifre no.
_KECCAK_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
_KECCAK_ROT = (
    (0, 36, 3, 41, 18), (1, 44, 10, 45, 2), (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56), (27, 20, 39, 8, 14),
)
_M64 = (1 << 64) - 1


def _rol(v, n):
    n %= 64
    return ((v << n) | (v >> (64 - n))) & _M64 if n else v


def _keccak_f(a):
    for rc in _KECCAK_RC:
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [a[i] ^ d[i % 5] for i in range(25)]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(a[x + 5 * y], _KECCAK_ROT[x][y])
        a = [b[i] ^ ((b[(i % 5 + 1) % 5 + 5 * (i // 5)] ^ _M64) & b[(i % 5 + 2) % 5 + 5 * (i // 5)])
             for i in range(25)]
        a[0] ^= rc
    return a


def _keccak256(data):
    rate = 136
    msg = bytearray(data) + b"\x01"
    msg += b"\x00" * (-len(msg) % rate)
    msg[-1] |= 0x80
    a = [0] * 25
    for off in range(0, len(msg), rate):
        block = msg[off:off + rate]
        for i in range(rate // 8):
            a[i] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        a = _keccak_f(a)
    return b"".join(lane.to_bytes(8, "little") for lane in a)[:32]


def _eip55_ok(addr):
    hex_part = addr[2:]
    digest = _keccak256(hex_part.lower().encode("ascii")).hex()
    for ch, h in zip(hex_part, digest):
        if ch.isalpha() and (ch.isupper() != (int(h, 16) >= 8)):
            return False
    return True


def _wallet_hits(text):
    out = []
    for m in _B58_RX.finditer(text):
        s = m.group(0)
        if s[0] not in "13LMDTXA9" or not re.search(r"\d", s) or not re.search(r"[a-z]", s):
            continue
        if _b58check_ok(s):
            out.append((4, m.start(), m.end(), "CRYPTO_WALLET", True))
    for m in _WIF_RX.finditer(text):
        if _b58check_ok(m.group(0), sizes=(37, 38)):
            out.append((5, m.start(), m.end(), "SECRET", True))
    for m in _BECH32_RX.finditer(text):
        if _bech32_ok(m.group(0)):
            out.append((4, m.start(), m.end(), "CRYPTO_WALLET", True))
    for m in _ETH_RX.finditer(text):
        s = m.group(0)
        hex_part = s[2:]
        mixed = hex_part.lower() != hex_part and hex_part.upper() != hex_part
        if mixed:
            if _eip55_ok(s):
                out.append((4, m.start(), m.end(), "CRYPTO_WALLET", True))
            continue
        # tutto minuscolo o maiuscolo: nessun checksum possibile, vale il formato
        if re.search(r"\d", hex_part) and re.search(r"[A-Fa-f]", hex_part):
            out.append((4, m.start(), m.end(), "CRYPTO_WALLET", False))
    return out


# ----------------------------------------------------------------------------
# PRODUCT_KEY (Windows / Office, 5x5 sull'alfabeto a 25 simboli)
# ----------------------------------------------------------------------------
_PKEY_RX = re.compile(r"(?<![A-Za-z0-9\-])(?:[BCDFGHJKMNPQRTVWXY2346789]{5}-){4}[BCDFGHJKMNPQRTVWXY2346789]{5}(?![A-Za-z0-9\-])")


def _pkey_hits(text):
    out = []
    for m in _PKEY_RX.finditer(text):
        s = m.group(0).replace("-", "")
        # "XXXXX-XXXXX-XXXXX-XXXXX-XXXXX" è il segnaposto della documentazione;
        # una chiave vera mescola lettere e cifre
        if len(set(s)) < 6 or not re.search(r"\d", s) or not re.search(r"[A-Z]", s):
            continue
        out.append((4, m.start(), m.end(), "PRODUCT_KEY", False))
    return out


# ----------------------------------------------------------------------------
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


@normalized_detector
def detect_cyber(text):
    """Entità delle dieci label, nella forma di `detect_regex`."""
    if not text:
        return []
    hits = (_cert_hits(text) + _ssh_hits(text) + _totp_hits(text) + _hash_hits(text)
            + _sid_hits(text) + _wallet_hits(text) + _pkey_hits(text)
            + _ipv4_hits(text) + _ipv6_hits(text) + _mac_hits(text))
    return [{
        "label": label,
        "start": s,
        "end": e,
        "score": 1.0,
        "validated": validated,
        "source": "regex",
    } for _, s, e, label, validated in _resolve(hits)]
