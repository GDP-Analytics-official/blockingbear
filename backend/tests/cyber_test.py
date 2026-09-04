"""Gruppo Cybersecurity: identificativi tecnici riconosciuti dal FORMATO, senza modello.

`engine/cyber.py` aggiunge dieci label alla rete regex: IP_ADDRESS, MAC_ADDRESS,
PASSWORD_HASH, TOTP_SECRET, SSH_KEY, SSH_FINGERPRINT, CERTIFICATE, WINDOWS_SID,
CRYPTO_WALLET, PRODUCT_KEY. Qui si verifica:
  - il set sintetico: per ogni testo le entità attese, e nessun'altra; i
    negativi (numeri di versione, orari, loopback, netmask, SID ben noti,
    formule LaTeX "$5$", hash di file senza contesto, segnaposto XXXXX-XXXXX,
    checksum sbagliati) restano in chiaro;
  - i checksum: base58check, bech32/bech32m, EIP-55 (keccak in puro Python),
    decodifica del blob SSH e del DER del certificato -> validated=True;
  - l'integrazione con `core`: le dieci label sono a span esatta (niente
    allargamento ai confini di parola, niente taglio di "=" o "/"), vincono su
    URL/TARGA/EMAIL/PASSWORD sulla stessa span, il placeholder è case-sensitive,
    tags() le elenca e le mette nel gruppo "cyber" insieme alle credenziali e a
    HOSTNAME/DEVICE_ID (devices.py, che a span esatta non sono);
  - `chat_anonymization`: superficie e ricerca esatte ("10.0.0.1" non trova
    "10.0.0.10").

Chiavi SSH e certificato generati ad hoc per il test, hash della stringa
"Prova123", wallet dai vettori di test pubblici di BIP173/BIP350/EIP-55.
Nessun dato reale.

Uso:  python backend/tests/cyber_test.py
"""
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.engine import core                                       # noqa: E402
from app.engine import cyber                                      # noqa: E402
from app.engine.credentials import CREDENTIAL_LABELS              # noqa: E402
from app.engine.cyber import CYBER_LABELS, detect_cyber           # noqa: E402
from app.engine.detectors import (DEVICE_LABELS, EXACT_SPAN_LABELS,  # noqa: E402
                                  SOFT_REGEX_LABELS, TAG_GROUPS, detect_regex)

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {msg}")


IP, MAC, HASH, TOTP, SSH, FP, CERT, SID, WAL, PK = (
    "IP_ADDRESS", "MAC_ADDRESS", "PASSWORD_HASH", "TOTP_SECRET", "SSH_KEY",
    "SSH_FINGERPRINT", "CERTIFICATE", "WINDOWS_SID", "CRYPTO_WALLET", "PRODUCT_KEY")

# (testo, [(label, valore), ...]) - lista vuota = nessun rilevamento atteso
CASES = [('il server ha indirizzo 192.168.1.15', [('IP_ADDRESS', '192.168.1.15')]),
 ('Host: 10.0.0.1:5432', [('IP_ADDRESS', '10.0.0.1')]),
 ('la subnet è 10.20.0.0/16 e il gateway 10.20.0.1',
  [('IP_ADDRESS', '10.20.0.0/16'), ('IP_ADDRESS', '10.20.0.1')]),
 ('ping 8.8.8.8 non risponde', [('IP_ADDRESS', '8.8.8.8')]),
 ('connessione da 172.16.254.3.', [('IP_ADDRESS', '172.16.254.3')]),
 ('http://10.0.0.1/admin', [('IP_ADDRESS', '10.0.0.1')]),
 ('allow from 213.45.67.89;', [('IP_ADDRESS', '213.45.67.89')]),
 ('IPv6: 2001:db8:85a3::8a2e:370:7334', [('IP_ADDRESS', '2001:db8:85a3::8a2e:370:7334')]),
 ('[2001:db8::1]:8080', [('IP_ADDRESS', '2001:db8::1')]),
 ('fe80::1c2a:3b4c:5d6e:7f80%eth0', [('IP_ADDRESS', 'fe80::1c2a:3b4c:5d6e:7f80%eth0')]),
 ('rete 2001:db8:abcd:12::/64', [('IP_ADDRESS', '2001:db8:abcd:12::/64')]),
 ('::ffff:10.1.2.3', [('IP_ADDRESS', '::ffff:10.1.2.3')]),
 ('ip 2001:0db8:0000:0000:0000:ff00:0042:8329.',
  [('IP_ADDRESS', '2001:0db8:0000:0000:0000:ff00:0042:8329')]),
 ("l'ip è 2001:db8::1: poi altro", [('IP_ADDRESS', '2001:db8::1')]),
 ('versione 2.4.1.0 di Apache', []),
 ('Chrome 128.0.6613.84', []),
 ('kernel 6.12.0.211', []),
 ('v1.2.3.4', []),
 ('release 10.0.19045.1', []),
 ('bind 0.0.0.0:8000', []),
 ('localhost 127.0.0.1', []),
 ('netmask 255.255.255.0', []),
 ('multicast 239.255.255.250', []),
 ('::1 è il loopback', []),
 ('alle 12:30:45 del 02.09.2026', []),
 ('1.2.3.4.5 non è un ip', []),
 ('ip: 192.168.1.256', []),
 ('id 3.14.15.9265', []),
 ('costa 1.000.000,00 euro', []),
 ('MAC 00:1A:2B:3C:4D:5E', [('MAC_ADDRESS', '00:1A:2B:3C:4D:5E')]),
 ('scheda 00-1a-2b-3c-4d-5e', [('MAC_ADDRESS', '00-1a-2b-3c-4d-5e')]),
 ('mac cisco 001a.2b3c.4d5e', [('MAC_ADDRESS', '001a.2b3c.4d5e')]),
 ('dhcp lease aa:bb:cc:dd:ee:ff -> 192.168.0.7',
  [('MAC_ADDRESS', 'aa:bb:cc:dd:ee:ff'), ('IP_ADDRESS', '192.168.0.7')]),
 ('broadcast ff:ff:ff:ff:ff:ff', []),
 ('vuoto 00:00:00:00:00:00', []),
 ('00:1A:2B:3C:4D:5E:6F sette gruppi', []),
 ('orario 10:20:30', []),
 ('codice 12-34-56', []),
 ('root:$6$saltsalt$GnTpeMNLLZvfvguLvjq5GGJ6xAph.k9jThqjjbb9d4wlFmAEghTvesJKZPihnKOTIvsz8WpW3QlERkMQvu0yG/:19000:0:99999:7:::',
  [('PASSWORD_HASH',
    '$6$saltsalt$GnTpeMNLLZvfvguLvjq5GGJ6xAph.k9jThqjjbb9d4wlFmAEghTvesJKZPihnKOTIvsz8WpW3QlERkMQvu0yG/')]),
 ('hash: $5$saltsalt$xH9cDDU0CkTLN/6AZzWk7tnPkVbI7rOG8XxgkMEOLF7',
  [('PASSWORD_HASH', '$5$saltsalt$xH9cDDU0CkTLN/6AZzWk7tnPkVbI7rOG8XxgkMEOLF7')]),
 ('md5crypt $1$saltsalt$2uyoWpT/fKiCuY7H2ZeQT/',
  [('PASSWORD_HASH', '$1$saltsalt$2uyoWpT/fKiCuY7H2ZeQT/')]),
 ('htpasswd: admin:$apr1$saltsalt$0lOtBxuiNJa3k4T3WU9691',
  [('PASSWORD_HASH', '$apr1$saltsalt$0lOtBxuiNJa3k4T3WU9691')]),
 ("password_hash = '$2b$12$R9h/cIPz0gi.URNNX3kh2OPST9/PgBkqquzi.Ss7KIUgO2t0jWMUW'",
  [('PASSWORD_HASH', '$2b$12$R9h/cIPz0gi.URNNX3kh2OPST9/PgBkqquzi.Ss7KIUgO2t0jWMUW')]),
 ("l'hash è $2b$12$R9h/cIPz0gi.URNNX3kh2OPST9/PgBkqquzi.Ss7KIUgO2t0jWMUW.",
  [('PASSWORD_HASH', '$2b$12$R9h/cIPz0gi.URNNX3kh2OPST9/PgBkqquzi.Ss7KIUgO2t0jWMUW')]),
 ('argon: $argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0$RdescudvJCsgt3ub+b+dWRWJTmaaJObG',
  [('PASSWORD_HASH',
    '$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0$RdescudvJCsgt3ub+b+dWRWJTmaaJObG')]),
 ('argon: $argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0$RdescudvJCsgt3ub+b+dWRWJTmaaJObG.',
  [('PASSWORD_HASH',
    '$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0$RdescudvJCsgt3ub+b+dWRWJTmaaJObG')]),
 ('django: pbkdf2_sha256$600000$saltsaltsalt$Yx3fbRt9V6mGx2c9h0Z1QXw1mI3QY6f2nVQ2QdKpJ4A=',
  [('PASSWORD_HASH',
    'pbkdf2_sha256$600000$saltsaltsalt$Yx3fbRt9V6mGx2c9h0Z1QXw1mI3QY6f2nVQ2QdKpJ4A=')]),
 ('mysql: *94BDCEBE19083CE2A1F959FD02F964C7AF4CFC29',
  [('PASSWORD_HASH', '*94BDCEBE19083CE2A1F959FD02F964C7AF4CFC29')]),
 ('Administrator:500:aad3b435b51404eeaad3b435b51404ee:31d6cfe0d16ae931b73c59d7e0c089c0:::',
  [('PASSWORD_HASH', 'aad3b435b51404eeaad3b435b51404ee:31d6cfe0d16ae931b73c59d7e0c089c0')]),
 ('NTLM hash: 31d6cfe0d16ae931b73c59d7e0c089c0',
  [('PASSWORD_HASH', '31d6cfe0d16ae931b73c59d7e0c089c0')]),
 ("l'hash della password è 5f4dcc3b5aa765d61d8327deb882cf99",
  [('PASSWORD_HASH', '5f4dcc3b5aa765d61d8327deb882cf99')]),
 ('sha256 hash = 5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8',
  [('PASSWORD_HASH', '5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8')]),
 ('ldap: {SSHA}MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkw',
  [('PASSWORD_HASH', '{SSHA}MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkw')]),
 ('userPassword: {CRYPT}$6$abc$defghijklmnopqrstuvwxyz0123456789',
  [('PASSWORD_HASH', '{CRYPT}$6$abc$defghijklmnopqrstuvwxyz0123456789')]),
 ('$P$B0nzM4yG3qb3ZRoHQ3E0T8A1xpJ0YS/', [('PASSWORD_HASH', '$P$B0nzM4yG3qb3ZRoHQ3E0T8A1xpJ0YS/')]),
 ('il costo è $5$ e non $6$', []),
 ('formula $x^2$ ok', []),
 ('commit 5f4dcc3b5aa765d61d8327deb882cf99 su main', []),
 ('sha256sum: 5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8 file.iso', []),
 ('${DB_PASSWORD}', []),
 ('$HOME/bin', []),
 ('otpauth://totp/ACME:mrossi@acme.it?secret=JBSWY3DPEHPK3PXP&issuer=ACME',
  [('TOTP_SECRET', 'otpauth://totp/ACME:mrossi@acme.it?secret=JBSWY3DPEHPK3PXP&issuer=ACME')]),
 ('secret 2FA: JBSWY3DPEHPK3PXP', [('TOTP_SECRET', 'JBSWY3DPEHPK3PXP')]),
 ("la chiave dell'authenticator è jbsw y3dp ehpk 3pxp", [('TOTP_SECRET', 'jbsw y3dp ehpk 3pxp')]),
 ('TOTP seed: JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP',
  [('TOTP_SECRET', 'JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP')]),
 ('codice di verifica: 482913', []),
 ('MFA setup key = GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ',
  [('TOTP_SECRET', 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ')]),
 ('JBSWY3DPEHPK3PXP senza contesto', []),
 ('AUTENTICAZIONE A DUE FATTORI: DISATTIVATA', []),
 ('2FA attiva, come vado alla casa mia', []),
 ("l'autenticazione a due fattori usa Google Authenticator", []),
 ('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFe3Zdj8UYVJz6iMbpHXOUBRlu1/aR0NWwdCQ5/XNV4g '
  'mrossi@laptop-acme',
  [('SSH_KEY',
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFe3Zdj8UYVJz6iMbpHXOUBRlu1/aR0NWwdCQ5/XNV4g '
    'mrossi@laptop-acme')]),
 ('ssh-rsa '
  'AAAAB3NzaC1yc2EAAAADAQABAAABAQCQ+sp3S+cwM8vQXm2iu6+RZoSB15I6dditZm65wgWPvETJSqbqKVDIYQXRdI/iioizOIv2j6EK5IpIqyTofjBefrL3NgCIjRWgODsgNnMVfkxphiGA0/OOqFYG6gsvZnEahqyzV9szAUgIC7EYVtyUFlgnD79BtvGiHIOIZr5ssxRN+hHXwC5X/k+sQAiQjQu+FjgMLnX7iAcKej4EiGY1kJayc42qDSiutzrf2xpnWzrw9txhQBXcvcXnrD3z8I2ycEPalPwibWGuwr+gQ8LizYnBORvE2LAabUd1aJfXptWltg4MHzsSAUeV31TAS0vNUvwTLED67z0rDcdSt1F/ '
  'deploy@srv01',
  [('SSH_KEY',
    'ssh-rsa '
    'AAAAB3NzaC1yc2EAAAADAQABAAABAQCQ+sp3S+cwM8vQXm2iu6+RZoSB15I6dditZm65wgWPvETJSqbqKVDIYQXRdI/iioizOIv2j6EK5IpIqyTofjBefrL3NgCIjRWgODsgNnMVfkxphiGA0/OOqFYG6gsvZnEahqyzV9szAUgIC7EYVtyUFlgnD79BtvGiHIOIZr5ssxRN+hHXwC5X/k+sQAiQjQu+FjgMLnX7iAcKej4EiGY1kJayc42qDSiutzrf2xpnWzrw9txhQBXcvcXnrD3z8I2ycEPalPwibWGuwr+gQ8LizYnBORvE2LAabUd1aJfXptWltg4MHzsSAUeV31TAS0vNUvwTLED67z0rDcdSt1F/ '
    'deploy@srv01')]),
 ('ecdsa-sha2-nistp256 '
  'AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBPBXLTi9MbZvqKFFsTysEBFhR9Pv2/OWRI6Ke8GDTFbxWTq15ZjLsfKplISDdCZp3rCK3e9VQojD6tMJvq2Sem0= ',
  [('SSH_KEY',
    'ecdsa-sha2-nistp256 '
    'AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBPBXLTi9MbZvqKFFsTysEBFhR9Pv2/OWRI6Ke8GDTFbxWTq15ZjLsfKplISDdCZp3rCK3e9VQojD6tMJvq2Sem0=')]),
 ('authorized_keys: ssh-ed25519 '
  'AAAAC3NzaC1lZDI1NTE5AAAAIFe3Zdj8UYVJz6iMbpHXOUBRlu1/aR0NWwdCQ5/XNV4g',
  [('SSH_KEY',
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFe3Zdj8UYVJz6iMbpHXOUBRlu1/aR0NWwdCQ5/XNV4g')]),
 ('ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFe3Zdj8UYVJz6iMbpHXOUBRlu1/aR0NWwdCQ5/XNV4g è la mia '
  'chiave',
  [('SSH_KEY',
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFe3Zdj8UYVJz6iMbpHXOUBRlu1/aR0NWwdCQ5/XNV4g')]),
 ('chiave: AAAAC3NzaC1lZDI1NTE5AAAAIFe3Zdj8UYVJz6iMbpHXOUBRlu1/aR0NWwdCQ5/XNV4g',
  [('SSH_KEY', 'AAAAC3NzaC1lZDI1NTE5AAAAIFe3Zdj8UYVJz6iMbpHXOUBRlu1/aR0NWwdCQ5/XNV4g')]),
 ('fingerprint SHA256:Vr871a2kTe+vCtlgt/pvKXN+fR9BBWDsKc4UfwKNpsc',
  [('SSH_FINGERPRINT', 'SHA256:Vr871a2kTe+vCtlgt/pvKXN+fR9BBWDsKc4UfwKNpsc')]),
 ('MD5:6f:7f:62:3a:22:7d:39:af:f1:32:15:1d:71:69:bf:c3',
  [('SSH_FINGERPRINT', 'MD5:6f:7f:62:3a:22:7d:39:af:f1:32:15:1d:71:69:bf:c3')]),
 ('fp 6f:7f:62:3a:22:7d:39:af:f1:32:15:1d:71:69:bf:c3.',
  [('SSH_FINGERPRINT', '6f:7f:62:3a:22:7d:39:af:f1:32:15:1d:71:69:bf:c3')]),
 ('ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAA', []),
 ('SHA256:troppocorto', []),
 ('cert:\n'
  '-----BEGIN CERTIFICATE-----\n'
  'MIIDLTCCAhWgAwIBAgIUU++Rhrn2mRX68VQVVGCyCcP0rt8wDQYJKoZIhvcNAQEL\n'
  'BQAwJjEVMBMGA1UEAwwMdGVzdC5hY21lLml0MQ0wCwYDVQQKDARBQ01FMB4XDTI2\n'
  'MDkwMjE2MDQwOVoXDTI2MDkwMzE2MDQwOVowJjEVMBMGA1UEAwwMdGVzdC5hY21l\n'
  'Lml0MQ0wCwYDVQQKDARBQ01FMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKC\n'
  'AQEAjQE0zFZ3c6CL0gg7nD9QeM0ZO/NJdiZoPFsLhTs9hc6yU087H9qyV+O3rvA4\n'
  'x8/HlVsXUzfOOxEfhpzx65kC5lV4qxgtF3PgUe5zzMbkEyxqmBpFBV/9QFPMUVDM\n'
  'REaLiDj0OyG9W8aMmEL1IjHiKjypOqYKRB1R6p7smbgbcwyNHHYYjN7NoVLGxGkj\n'
  'VbmrwCStCbUq91nXzHpPxtyEBGmqtAzVvEV79levbQo/AIm6ufdZ9dSpWr2tsFkR\n'
  'oilxyS/+LoKGKQSJlBZfI1W0GRYUFK8z1MXKgt2LKO/eF5+OZh0H8b629iZuc3Wx\n'
  '1yLp8kKjGmf7y5wTWlwbaf3kAwIDAQABo1MwUTAdBgNVHQ4EFgQUi0LBzw3TvgPU\n'
  'sIRA4O0ZDMZqD8kwHwYDVR0jBBgwFoAUi0LBzw3TvgPUsIRA4O0ZDMZqD8kwDwYD\n'
  'VR0TAQH/BAUwAwEB/zANBgkqhkiG9w0BAQsFAAOCAQEAU1XebRu1jBu65RMEW65y\n'
  'QbXrnk48xE6fpvqbiypF9HfDPaDJkrFckUNRPIoeLY3sPgZw8VeuoU3Og0NL2b39\n'
  'iqdWGn7kJplm9QsWW5LzH0Nu0Zs0pkSMvF+oXzoTBfroSbuzWY6bnck01ancsn2W\n'
  'a65fH980uSa9cEmrpM0GLLpd4ZurIqDklpHm2Bd1JfabdnAUqVVysWu7YCV9ySsQ\n'
  'XnZrAcJuImvwDnKfWbjb0qxBdZsyfIMQIW3ZHVBiueJkUex1YcE6SfUeMpkPcXam\n'
  'oPNoEVy9EZg5AeM0d7Nj9ChHaP1ELcCHdFMk8YObQbbdzG3BPcBllzRSbVrbvEU9\n'
  '9w==\n'
  '-----END CERTIFICATE-----\n'
  'fine',
  [('CERTIFICATE',
    '-----BEGIN CERTIFICATE-----\n'
    'MIIDLTCCAhWgAwIBAgIUU++Rhrn2mRX68VQVVGCyCcP0rt8wDQYJKoZIhvcNAQEL\n'
    'BQAwJjEVMBMGA1UEAwwMdGVzdC5hY21lLml0MQ0wCwYDVQQKDARBQ01FMB4XDTI2\n'
    'MDkwMjE2MDQwOVoXDTI2MDkwMzE2MDQwOVowJjEVMBMGA1UEAwwMdGVzdC5hY21l\n'
    'Lml0MQ0wCwYDVQQKDARBQ01FMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKC\n'
    'AQEAjQE0zFZ3c6CL0gg7nD9QeM0ZO/NJdiZoPFsLhTs9hc6yU087H9qyV+O3rvA4\n'
    'x8/HlVsXUzfOOxEfhpzx65kC5lV4qxgtF3PgUe5zzMbkEyxqmBpFBV/9QFPMUVDM\n'
    'REaLiDj0OyG9W8aMmEL1IjHiKjypOqYKRB1R6p7smbgbcwyNHHYYjN7NoVLGxGkj\n'
    'VbmrwCStCbUq91nXzHpPxtyEBGmqtAzVvEV79levbQo/AIm6ufdZ9dSpWr2tsFkR\n'
    'oilxyS/+LoKGKQSJlBZfI1W0GRYUFK8z1MXKgt2LKO/eF5+OZh0H8b629iZuc3Wx\n'
    '1yLp8kKjGmf7y5wTWlwbaf3kAwIDAQABo1MwUTAdBgNVHQ4EFgQUi0LBzw3TvgPU\n'
    'sIRA4O0ZDMZqD8kwHwYDVR0jBBgwFoAUi0LBzw3TvgPUsIRA4O0ZDMZqD8kwDwYD\n'
    'VR0TAQH/BAUwAwEB/zANBgkqhkiG9w0BAQsFAAOCAQEAU1XebRu1jBu65RMEW65y\n'
    'QbXrnk48xE6fpvqbiypF9HfDPaDJkrFckUNRPIoeLY3sPgZw8VeuoU3Og0NL2b39\n'
    'iqdWGn7kJplm9QsWW5LzH0Nu0Zs0pkSMvF+oXzoTBfroSbuzWY6bnck01ancsn2W\n'
    'a65fH980uSa9cEmrpM0GLLpd4ZurIqDklpHm2Bd1JfabdnAUqVVysWu7YCV9ySsQ\n'
    'XnZrAcJuImvwDnKfWbjb0qxBdZsyfIMQIW3ZHVBiueJkUex1YcE6SfUeMpkPcXam\n'
    'oPNoEVy9EZg5AeM0d7Nj9ChHaP1ELcCHdFMk8YObQbbdzG3BPcBllzRSbVrbvEU9\n'
    '9w==\n'
    '-----END CERTIFICATE-----')]),
 ('-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----', []),
 ('SID S-1-5-21-3623811015-3361044348-30300820-1013',
  [('WINDOWS_SID', 'S-1-5-21-3623811015-3361044348-30300820-1013')]),
 ('domain S-1-5-21-3623811015-3361044348-30300820',
  [('WINDOWS_SID', 'S-1-5-21-3623811015-3361044348-30300820')]),
 ('azure S-1-12-1-1234567890-1234567890-1234567890-1234567890',
  [('WINDOWS_SID', 'S-1-12-1-1234567890-1234567890-1234567890-1234567890')]),
 ('SYSTEM S-1-5-18, Everyone S-1-1-0, Administrators S-1-5-32-544', []),
 ('btc 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa',
  [('CRYPTO_WALLET', '1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa')]),
 ('p2sh 3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy',
  [('CRYPTO_WALLET', '3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy')]),
 ('bech32 bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq',
  [('CRYPTO_WALLET', 'bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq')]),
 ('taproot bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0',
  [('CRYPTO_WALLET', 'bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0')]),
 ('ltc LQTpS3VaYTjCr4s9Y1t5zbeY26zevf7Fb3',
  [('CRYPTO_WALLET', 'LQTpS3VaYTjCr4s9Y1t5zbeY26zevf7Fb3')]),
 ('eth 0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed',
  [('CRYPTO_WALLET', '0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed')]),
 ('eth lower 0xde0b295669a9fd93d5f28d9ec85e40f4cb697bae',
  [('CRYPTO_WALLET', '0xde0b295669a9fd93d5f28d9ec85e40f4cb697bae')]),
 ('checksum errato 0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD', []),
 ('btc errato 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb', []),
 ('bech32 errato bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdx', []),
 ('hash 0x0000000000000000000000000000000000000000', []),
 ('id 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa2', []),
 ('Product Key: W269N-WFGWX-YVC9B-4J6C9-T83GX', [('PRODUCT_KEY', 'W269N-WFGWX-YVC9B-4J6C9-T83GX')]),
 ('chiave office NKGG6-WBPCC-HXWMY-6DQGJ-CPQVG',
  [('PRODUCT_KEY', 'NKGG6-WBPCC-HXWMY-6DQGJ-CPQVG')]),
 ('XXXXX-XXXXX-XXXXX-XXXXX-XXXXX', []),
 ('AAAAA-BBBBB-CCCCC-DDDDD-EEEEE', []),
 ('codice 12345-67890-12345-67890-12345', []),
 ('W269N-WFGWX-YVC9B-4J6C9', []),
 ('server 10.1.1.5 mac 00:1a:2b:3c:4d:5e sid S-1-5-21-1-2-3-500',
  [('IP_ADDRESS', '10.1.1.5'),
   ('MAC_ADDRESS', '00:1a:2b:3c:4d:5e'),
   ('WINDOWS_SID', 'S-1-5-21-1-2-3-500')]),
 ('tel 333 123 4567, iban IT60X0542811101000000123456, targa AB123CD', []),
 ('Mario Rossi, nato il 12/03/1980 a Roma, CF RSSMRA80C12H501U', []),
 ('uuid 123e4567-e89b-12d3-a456-426614174000', []),
 ('CVE-2024-12345 su porta 443', []),
 ('Sep  2 10:15:32 srv01 sshd[1234]: Accepted publickey for mrossi from 192.168.10.44 port 51022 '
  'ssh2: ED25519 SHA256:Vr871a2kTe+vCtlgt/pvKXN+fR9BBWDsKc4UfwKNpsc',
  [('IP_ADDRESS', '192.168.10.44'),
   ('SSH_FINGERPRINT', 'SHA256:Vr871a2kTe+vCtlgt/pvKXN+fR9BBWDsKc4UfwKNpsc')]),
 ('nginx 1.24.0.1 su 10.0.5.2, 10.0.5.3 e 10.0.5.4',
  [('IP_ADDRESS', '10.0.5.2'), ('IP_ADDRESS', '10.0.5.3'), ('IP_ADDRESS', '10.0.5.4')]),
 ('{"cert": "-----BEGIN '
  'CERTIFICATE-----\\nMIIDLTCCAhWgAwIBAgIUU++Rhrn2mRX68VQVVGCyCcP0rt8wDQYJKoZIhvcNAQEL\\nBQAwJjEVMBMGA1UEAwwMdGVzdC5hY21lLml0MQ0wCwYDVQQKDARBQ01FMB4XDTI2\\n-----END '
  'CERTIFICATE-----"}',
  [('CERTIFICATE',
    '-----BEGIN '
    'CERTIFICATE-----\\nMIIDLTCCAhWgAwIBAgIUU++Rhrn2mRX68VQVVGCyCcP0rt8wDQYJKoZIhvcNAQEL\\nBQAwJjEVMBMGA1UEAwwMdGVzdC5hY21lLml0MQ0wCwYDVQQKDARBQ01FMB4XDTI2\\n-----END '
    'CERTIFICATE-----')]),
 ('-----BEGIN PUBLIC KEY-----\n'
  'MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE8FctOL0xtm+ooUWxPKwQEWFH0+/b\n'
  '85ZEjop7wYNMVvFZOrXlmMux8qmUhIN0JmnesIrd71VCiMPq0wm+rZJ6bQ==\n'
  '-----END PUBLIC KEY-----',
  [('CERTIFICATE',
    '-----BEGIN PUBLIC KEY-----\n'
    'MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE8FctOL0xtm+ooUWxPKwQEWFH0+/b\n'
    '85ZEjop7wYNMVvFZOrXlmMux8qmUhIN0JmnesIrd71VCiMPq0wm+rZJ6bQ==\n'
    '-----END PUBLIC KEY-----')]),
 ('interface GigabitEthernet0/1\n ip address 10.10.10.1 255.255.255.0\n mac-address 0011.2233.4455',
  [('IP_ADDRESS', '10.10.10.1'), ('MAC_ADDRESS', '0011.2233.4455')]),
 ('route add -net 192.168.50.0/24 gw 192.168.1.254',
  [('IP_ADDRESS', '192.168.50.0/24'), ('IP_ADDRESS', '192.168.1.254')]),
 ('DNS 1.1.1.1, 9.9.9.9', [('IP_ADDRESS', '1.1.1.1'), ('IP_ADDRESS', '9.9.9.9')]),
 ('ipv6 compresso 2001:db8::', [('IP_ADDRESS', '2001:db8::')]),
 ('otto gruppi 1:2:3:4:5:6:7:8', [('IP_ADDRESS', '1:2:3:4:5:6:7:8')]),
 ('porta [::]:443 in ascolto', []),
 ('ore 09:41:00 del 2026-09-02', []),
 ('rapporto 3:2:1', []),
 ('PSQL: postgresql://app:Segr3t0@10.0.0.9:5432/db', [('IP_ADDRESS', '10.0.0.9')]),
 ('SSID Casa, MAC del router 3C-84-6A-11-22-33.', [('MAC_ADDRESS', '3C-84-6A-11-22-33')]),
 ('MAC misto 00:1a-2b:3c-4d:5e', []),
 ("hash bcrypt '$2y$10$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUVWXYZ01234'",
  [('PASSWORD_HASH', '$2y$10$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUVWXYZ01234')]),
 ('bcrypt troncato $2b$12$R9h/cIPz0gi.URNNX3kh2O', []),
 ('$argon2i$v=19$m=4096,t=3,p=1$c29tZXNhbHQ$iWh06vD8Fy27wf9npn6FXWiCX4K6pW6Ue1Bnzz07Z8A',
  [('PASSWORD_HASH',
    '$argon2i$v=19$m=4096,t=3,p=1$c29tZXNhbHQ$iWh06vD8Fy27wf9npn6FXWiCX4K6pW6Ue1Bnzz07Z8A')]),
 ('$scrypt$ln=16,r=8,p=1$aM15713r3Xsvxbi31lqr1Q$nFNh2CVHVjNldFVKDHDlm4CbdRSCdEBsjjJxD+iCs5E',
  [('PASSWORD_HASH',
    '$scrypt$ln=16,r=8,p=1$aM15713r3Xsvxbi31lqr1Q$nFNh2CVHVjNldFVKDHDlm4CbdRSCdEBsjjJxD+iCs5E')]),
 ('ntlm: aad3b435b51404eeaad3b435b51404ee:31d6cfe0d16ae931b73c59d7e0c089c0, altro',
  [('PASSWORD_HASH', 'aad3b435b51404eeaad3b435b51404ee:31d6cfe0d16ae931b73c59d7e0c089c0')]),
 ('NT hash 31D6CFE0D16AE931B73C59D7E0C089C0',
  [('PASSWORD_HASH', '31D6CFE0D16AE931B73C59D7E0C089C0')]),
 ('hash del file: 31d6cfe0d16ae931b73c59d7e0c089c0', []),
 ('password hash troppo corto: abcdef', []),
 ('pbkdf2_sha256$600000$saltsaltsalt$Yx3fbRt9V6mGx2c9h0Z1QXw1mI3QY6f2nVQ2QdKpJ4A= in fondo',
  [('PASSWORD_HASH',
    'pbkdf2_sha256$600000$saltsaltsalt$Yx3fbRt9V6mGx2c9h0Z1QXw1mI3QY6f2nVQ2QdKpJ4A=')]),
 ('otpauth://hotp/Acme:m.rossi?secret=GEZDGNBVGY3TQOJQ&counter=1.',
  [('TOTP_SECRET', 'otpauth://hotp/Acme:m.rossi?secret=GEZDGNBVGY3TQOJQ&counter=1')]),
 ('2FA: il codice attuale è 123456', []),
 ('Setup MFA, chiave manuale: GEZD GNBV GY3T QOJQ GEZD GNBV.',
  [('TOTP_SECRET', 'GEZD GNBV GY3T QOJQ GEZD GNBV')]),
 ('il TOTP secret è JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP',
  [('TOTP_SECRET', 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP')]),
 ('MFA ENABLED FOR ALL ADMINISTRATORS ACCOUNTS', []),
 ('chiave con virgolette "ssh-ed25519 '
  'AAAAC3NzaC1lZDI1NTE5AAAAIFe3Zdj8UYVJz6iMbpHXOUBRlu1/aR0NWwdCQ5/XNV4g mrossi@laptop-acme"',
  [('SSH_KEY',
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFe3Zdj8UYVJz6iMbpHXOUBRlu1/aR0NWwdCQ5/XNV4g '
    'mrossi@laptop-acme')]),
 ('key = "AAAAC3NzaC1lZDI1NTE5AAAAIFe3Zdj8UYVJz6iMbpHXOUBRlu1/aR0NWwdCQ5/XNV4g"',
  [('SSH_KEY', 'AAAAC3NzaC1lZDI1NTE5AAAAIFe3Zdj8UYVJz6iMbpHXOUBRlu1/aR0NWwdCQ5/XNV4g')]),
 ('---- BEGIN SSH2 PUBLIC KEY ----\n'
  'Comment: "2048-bit RSA"\n'
  'AAAAB3NzaC1yc2EAAAADAQABAAABAQCQ+sp3S+cwM8vQXm2iu6+RZoSB15I6dditZm65\n'
  'wgWPvETJSqbqKVDIYQXRdI/iioizOIv2j6EK5IpIqyTofjBefrL3NgCIjRWgODsgNnMV\n'
  '---- END SSH2 PUBLIC KEY ----',
  [('SSH_KEY',
    '---- BEGIN SSH2 PUBLIC KEY ----\n'
    'Comment: "2048-bit RSA"\n'
    'AAAAB3NzaC1yc2EAAAADAQABAAABAQCQ+sp3S+cwM8vQXm2iu6+RZoSB15I6dditZm65\n'
    'wgWPvETJSqbqKVDIYQXRdI/iioizOIv2j6EK5IpIqyTofjBefrL3NgCIjRWgODsgNnMV\n'
    '---- END SSH2 PUBLIC KEY ----')]),
 ('base64 generico AAAAB3NzaC1yc2EAAAADAQABAAABAQCQ', []),
 ('utente S-1-5-21-3623811015-3361044348-30300820-1013 in gruppo S-1-5-32-545',
  [('WINDOWS_SID', 'S-1-5-21-3623811015-3361044348-30300820-1013')]),
 ('sid parziale S-1-5-21-3623811015', []),
 ('eth in json {"to":"0xde0b295669a9fd93d5f28d9ec85e40f4cb697bae"}',
  [('CRYPTO_WALLET', '0xde0b295669a9fd93d5f28d9ec85e40f4cb697bae')]),
 ('tx hash 0xde0b295669a9fd93d5f28d9ec85e40f4cb697bae1234567890abcdef1234567890abcdef', []),
 ('BC1QAR0SRRR7XFKVY5L643LYDNW9RE59GTZZWF5MDQ',
  [('CRYPTO_WALLET', 'BC1QAR0SRRR7XFKVY5L643LYDNW9RE59GTZZWF5MDQ')]),
 ('doge DH5yaieqoZN36fDVciNyRueRGvGLR3mr7L',
  [('CRYPTO_WALLET', 'DH5yaieqoZN36fDVciNyRueRGvGLR3mr7L')]),
 ('tron TJRabPrwbZy45sbavfcjinPJC18kjpRTv8',
  [('CRYPTO_WALLET', 'TJRabPrwbZy45sbavfcjinPJC18kjpRTv8')]),
 ('licenza: w269n-wfgwx-yvc9b-4j6c9-t83gx minuscola', []),
 ('chiave in tabella | W269N-WFGWX-YVC9B-4J6C9-T83GX |',
  [('PRODUCT_KEY', 'W269N-WFGWX-YVC9B-4J6C9-T83GX')]),
 ('W269N-WFGWX-YVC9B-4J6C9-T83GX-EXTRA', []),
 ('il kernel al 6.12.0.211 va bene', []),
 ('Nginx è alla versione 1.24.0.1 e ascolta su 10.4.4.4', [('IP_ADDRESS', '10.4.4.4')]),
 ('kernel aggiornato, host 10.5.5.5', [('IP_ADDRESS', '10.5.5.5')])]

CASES += [
    ("la seed Base32 JBSWY3DPEHPK3PXP era visibile sotto il QR", [(TOTP, "JBSWY3DPEHPK3PXP")]),
    ("il secondo fattore usa la chiave GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", [(TOTP, "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")]),
    # chiave privata WIF (esempi pubblici del wiki Bitcoin): materiale segreto -> SECRET
    ("un WIF bitcoin 5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ. Qualcuno", [("SECRET", "5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ")]),
    ("WIF compresso KwdMAjGmerYanjeui5SHS7JkmpZvVipYvB2LJGU1ZxJwYvP98617", [("SECRET", "KwdMAjGmerYanjeui5SHS7JkmpZvVipYvB2LJGU1ZxJwYvP98617")]),
    ("WIF errato 5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTK", []),
    ("sid S-1-5-21-1876949484-1292801991-3180804235-500, quindi Administrator", [(SID, "S-1-5-21-1876949484-1292801991-3180804235-500")]),
]

# --- contesto 2FA in francese, tedesco, spagnolo, olandese (lexicon.py) ---
CASES += [
    ("authentification à deux facteurs, clé secrète : JBSWY3DPEHPK3PXP", [("TOTP_SECRET", "JBSWY3DPEHPK3PXP")]),
    ("Zwei-Faktor-Authentifizierung: JBSWY3DPEHPK3PXP", [("TOTP_SECRET", "JBSWY3DPEHPK3PXP")]),
    ("verificación en dos pasos JBSWY3DPEHPK3PXP", [("TOTP_SECRET", "JBSWY3DPEHPK3PXP")]),
    ("tweestapsverificatie JBSWY3DPEHPK3PXP", [("TOTP_SECRET", "JBSWY3DPEHPK3PXP")]),   # la parola davanti resta fuori
    ("Einrichtungsschlüssel: JBSW Y3DP EHPK 3PXP", [("TOTP_SECRET", "JBSW Y3DP EHPK 3PXP")]),
    ("niente contesto JBSWY3DPEHPK3PXP", []),
]


def main():
    print("[A] set sintetico")
    for text, expect in CASES:
        got = [(e["label"], text[e["start"]:e["end"]]) for e in detect_cyber(text)]
        check(got == list(expect), f"{text[:70]!r}\n        atteso {expect}\n        trovato {got}")

    print("[B] checksum e validated")
    check(cyber._keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
          "keccak-256 della stringa vuota")
    for addr in ("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
                 "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB", "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb"):
        check(cyber._eip55_ok(addr), f"EIP-55 {addr}")
    check(not cyber._eip55_ok("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD"), "EIP-55 sbagliato")
    check(cyber._b58check_ok("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"), "base58check")
    check(not cyber._b58check_ok("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb"), "base58check sbagliato")
    check(cyber._bech32_ok("bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"), "bech32")
    check(cyber._bech32_ok("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0"), "bech32m")
    check(not cyber._bech32_ok("bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdx"), "bech32 sbagliato")

    def validated(text, label):
        return [e["validated"] for e in detect_cyber(text) if e["label"] == label]
    check(validated("btc 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", WAL) == [True], "wallet base58check validated")
    check(validated("eth 0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", WAL) == [True], "wallet EIP-55 validated")
    check(validated("eth 0xde0b295669a9fd93d5f28d9ec85e40f4cb697bae", WAL) == [False], "wallet minuscolo non validato")
    ed = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFe3Zdj8UYVJz6iMbpHXOUBRlu1/aR0NWwdCQ5/XNV4g"
    check(validated(ed, SSH) == [True], "blob SSH decodificato: validated")
    cert = next(t for t, exp in CASES if exp and exp[0][0] == CERT)
    check(validated(cert, CERT) == [True], "DER del certificato: validated")
    check(validated("fingerprint SHA256:Vr871a2kTe+vCtlgt/pvKXN+fR9BBWDsKc4UfwKNpsc", FP) == [True],
          "fingerprint SHA256 decodificata: validated")
    check(validated("ip 10.0.0.1 mac 00:1A:2B:3C:4D:5E", IP) == [False], "IP: nessun checksum, non validato")
    check(validated("sid S-1-5-21-1-2-3-500", SID) == [True], "SID: la forma vale come validazione")

    print("[C] rete regex, label esatte, gruppo")
    ents = detect_regex("server 10.0.0.1, mail a@acme.it, hash $2b$12$R9h/cIPz0gi.URNNX3kh2OPST9/PgBkqquzi.Ss7KIUgO2t0jWMUW")
    labels = {e["label"] for e in ents}
    check({IP, HASH, "EMAIL"} <= labels, f"detect_regex include il gruppo cyber: {labels}")
    for e in ents:
        check(set(e) >= {"label", "start", "end", "score", "validated", "source"}, f"campi entità {e}")
    check(not (CYBER_LABELS & SOFT_REGEX_LABELS), "nessuna label cyber è soft")
    check(EXACT_SPAN_LABELS == CREDENTIAL_LABELS | CYBER_LABELS, "EXACT_SPAN_LABELS = credenziali + cyber")
    check(TAG_GROUPS["cyber"] == sorted(EXACT_SPAN_LABELS | DEVICE_LABELS), f"gruppo cyber: {TAG_GROUPS}")
    check("PASSWORD" in TAG_GROUPS["cyber"] and IP in TAG_GROUPS["cyber"], "il gruppo contiene credenziali e cyber")

    print("[D] fusione: span esatte, priorità")
    def merged(text, model=()):
        kept = core._merge(list(model) + detect_regex(text), text)
        return {(e["label"], text[e["start"]:e["end"]]) for e in kept}

    vals = merged("rete 10.20.0.0/16 e host 10.20.0.1:5432.")
    check((IP, "10.20.0.0/16") in vals and (IP, "10.20.0.1") in vals, f"CIDR intero, porta fuori: {vals}")
    vals = merged("http://10.0.0.1/admin")
    check((IP, "10.0.0.1") in vals and not any(l == "URL" for l, _ in vals), f"IP batte URL: {vals}")
    vals = merged("Product Key: W269N-WFGWX-YVC9B-4J6C9-T83GX")
    check(vals == {(PK, "W269N-WFGWX-YVC9B-4J6C9-T83GX")}, f"PRODUCT_KEY batte TARGA/SECRET: {vals}")
    uri = "otpauth://totp/ACME:mrossi@acme.it?secret=JBSWY3DPEHPK3PXP&issuer=ACME"
    vals = merged(uri)
    check(vals == {(TOTP, uri)}, f"TOTP_SECRET batte EMAIL e SECRET: {vals}")
    h = "$2b$12$R9h/cIPz0gi.URNNX3kh2OPST9/PgBkqquzi.Ss7KIUgO2t0jWMUW"
    vals = merged(f"password: {h}")
    check(vals == {(HASH, h)}, f"PASSWORD_HASH batte PASSWORD: {vals}")
    dj = "pbkdf2_sha256$600000$saltsaltsalt$Yx3fbRt9V6mGx2c9h0Z1QXw1mI3QY6f2nVQ2QdKpJ4A="
    vals = merged(f"hash {dj} fine")
    check((HASH, dj) in vals, f"'=' finale resta nella span: {vals}")
    text = "SID S-1-5-21-3623811015-3361044348-30300820-1013"
    model = [{"label": "TELEPHONENUM", "start": text.index("3623811015"), "end": text.index("3623811015") + 10,
              "score": 0.95, "validated": False, "source": "modello"}]
    vals = merged(text, model)
    check(vals == {(SID, "S-1-5-21-3623811015-3361044348-30300820-1013")}, f"SID intero batte il frammento del modello: {vals}")
    # "1-5-21-1876949484" passa il Luhn per caso: la carta validata non deve rubare il SID
    vals = merged("sid S-1-5-21-1876949484-1292801991-3180804235-500, quindi")
    check(vals == {(SID, "S-1-5-21-1876949484-1292801991-3180804235-500")}, f"SID batte la carta Luhn casuale: {vals}")

    class NoModel(core.PiiEngine):
        def detect_model(self, text, ctl=None):
            return []
    eng = NoModel(str(HERE / "models" / "none"))
    res = eng.analyze("MAC 00:1A:2B:3C:4D:5E poi 00:1a:2b:3c:4d:5e poi 00:1A:2B:3C:4D:5E")
    phs = [e["ph"] for e in res["entities"]]
    check(len(set(phs)) == 2 and phs[0] == phs[2] and phs[0].startswith("[MAC_ADDRESS_"), f"MAC case-sensitive: {phs}")
    res = eng.analyze(f"host 10.0.0.1, hash {h}, di nuovo 10.0.0.1")
    check(res["anonymized_text"] == "host [IP_ADDRESS_1], hash [PASSWORD_HASH_1], di nuovo [IP_ADDRESS_1]",
          res["anonymized_text"])
    check(res["mapping"] == {"[IP_ADDRESS_1]": "10.0.0.1", "[PASSWORD_HASH_1]": h}, res["mapping"])
    check(core.decode_text(res["anonymized_text"], res["mapping"])[0] == f"host 10.0.0.1, hash {h}, di nuovo 10.0.0.1",
          "decodifica con '$' nel valore")

    cfg = HERE / "models"
    model_dirs = [d for d in cfg.glob("*") if (d / "config.json").exists()] if cfg.exists() else []
    if model_dirs:
        tags = core.PiiEngine(str(model_dirs[0])).tags()
        check(CYBER_LABELS <= set(tags["all"]) and CYBER_LABELS <= set(tags["regex_only"]),
              f"tags(): {tags['regex_only']}")
        check(tags["groups"] == {"cyber": sorted(EXACT_SPAN_LABELS | DEVICE_LABELS)},
              f"tags()['groups']: {tags['groups']}")
    else:
        print("  (nessun modello scaricato: tags() non verificato)")

    print("[E] chat_anonymization: superfici esatte")
    try:
        from app import chat_anonymization as ca
    except Exception as exc:                        # dipendenze pesanti assenti
        print(f"  (chat_anonymization non importabile qui: {exc})")
    else:
        check(ca._surface_core(IP, "10.0.0.1") == "10.0.0.1", "surface_core esatta")
        check(ca._surface_core(MAC, "00:1A:2B:3C:4D:5E") != ca._surface_core(MAC, "00:1a:2b:3c:4d:5e"),
              "surface_core case-sensitive")
        pat = ca._pattern_for("[IP_ADDRESS_1]", "10.0.0.1")
        check(pat.search("host 10.0.0.1:5432") is not None, "trova l'IP seguito dalla porta")
        check(pat.search("host 10.0.0.10") is None, "10.0.0.1 non trova 10.0.0.10")
        check(pat.search("host 110.0.0.1") is None, "10.0.0.1 non trova 110.0.0.1")
        pat = ca._pattern_for("[PASSWORD_HASH_1]", h)
        check(pat.search(f"x {h} y") is not None, "hash con '$' e '.' trovato letteralmente")
        text = f"hash {h} e ip 10.0.0.1 e 10.0.0.10"
        out = ca.apply_known_surfaces(text, {"[PASSWORD_HASH_1]": h, "[IP_ADDRESS_1]": "10.0.0.1"})
        check(out == "hash [PASSWORD_HASH_1] e ip [IP_ADDRESS_1] e 10.0.0.10", out)

    print(f"\nPASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
