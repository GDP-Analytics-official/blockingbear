"""HOSTNAME e DEVICE_ID: nomi di macchina e identificativi di dispositivo, senza modello.

`engine/devices.py` aggiunge due label alla rete regex, nel gruppo «Cybersecurity»:
  HOSTNAME   solo nelle forme ancorate (FQDN con suffisso interno, UNC, nomi di
             default Windows, riga di comando e prompt, syslog, CN= di un DN,
             parola chiave + identificativo);
  DEVICE_ID  IMEI e ICCID (Luhn + prefisso, o parola chiave), IMSI, numeri di
             serie / service tag / asset tag, UDID/MEID/hardware id con parola chiave.
Qui si verifica:
  - il set sintetico: per ogni testo le entità attese, e nessun'altra; i
    negativi (nomi nudi, "server web", modelli di hardware, ticket, versioni,
    domini pubblici, IP, porte seriali, carte di credito vere) restano in chiaro;
  - `detectors.card_ok`: un IMEI o un ICCID che passano il Luhn non sono più una
    carta di credito, né per la rete regex né per il post-filtro sul modello,
    mentre le carte di prova pubbliche (Visa/Mastercard/Amex) passano ancora;
  - l'integrazione con `core`: le due label NON sono a span esatta (SRV01 e srv01
    hanno lo stesso placeholder, il punto finale resta fuori), l'IP batte la
    parola chiave, la chiave 5x5 resta PRODUCT_KEY, tags() le elenca nel gruppo;
  - `chat_anonymization`: superficie e ricerca tolleranti al case.

Nomi di macchina inventati, IMEI/ICCID costruiti ad hoc con la cifra di
controllo calcolata, numeri di serie di fantasia. Nessun dato reale.

Uso:  python backend/tests/devices_test.py
"""
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.engine import core                                       # noqa: E402
from app.engine.devices import (DEVICE_LABELS, IMEI_RBI, detect_devices,  # noqa: E402
                                is_device_number)
from app.engine.detectors import (DETECTORS, EXACT_SPAN_LABELS,   # noqa: E402
                                  SOFT_REGEX_LABELS, TAG_GROUPS, card_ok,
                                  detect_regex, luhn_ok, scan_card)

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {msg}")


H, D = "HOSTNAME", "DEVICE_ID"

# (testo, [(label, valore), ...]); vuoto = nessun rilevamento atteso
CASES = [('il DC è star.corp.lan e risponde', [(H, 'star.corp.lan')]),
 ('connessione a fileserver01.acme.local fallita', [(H, 'fileserver01.acme.local')]),
 ('SRV-APP01.INTRA', [(H, 'SRV-APP01.INTRA')]),
 ('nodo k8s-w3.cluster.internal down', [(H, 'k8s-w3.cluster.internal')]),
 ('host printer.localdomain e nas01.priv', [(H, 'printer.localdomain'), (H, 'nas01.priv')]),
 ('router.home.arpa', [(H, 'router.home.arpa')]),
 ('sito www.acme.it e portale acme.it', []),
 ('file settings.local.php e config.lan.json', []),
 ('mail a admin@srv01.corp.lan', []),
 ('proprietà user.home e local.host', []),
 ('intranet.acme.lan.', [(H, 'intranet.acme.lan')]),
 ('copiato in \\\\forensics\\INC-88421\\img', [(H, 'forensics')]),
 ('share \\\\srv-file01.corp.lan\\dati$', [(H, 'srv-file01.corp.lan')]),
 ('path \\\\?\\C:\\Windows e \\\\.\\pipe\\x', []),
 ('json \\"\\\\\\\\backup02\\\\share\\"', [(H, 'backup02')]),
 ('\\\\10.0.0.5\\share', []),
 ('\\\\localhost\\c$', []),
 ('il portatile DESKTOP-NBI04 e LAPTOP-7Q2XK1P',
  [(H, 'DESKTOP-NBI04'), (H, 'LAPTOP-7Q2XK1P')]),
 ('WIN-A1B2C3D4E5F', [(H, 'WIN-A1B2C3D4E5F')]),
 ('build win-amd64 e desktop-nbi04 minuscolo', []),
 ('DESKTOP-DELL senza cifre', []),
 ('ssh admin@bastion-01', [(H, 'bastion-01')]),
 ('$ ssh -p 2222 -i ~/.ssh/id_ed25519 root@jumphost', [(H, 'jumphost')]),
 ("l'accesso ssh al bastion è chiuso", []),
 ('ssh key rotation', []),
 ('ping srv-db02 fallisce', [(H, 'srv-db02')]),
 ('# ping nas01\nPING nas01 ...', [(H, 'nas01'), (H, 'nas01')]),
 ('scp -P 2222 dump.sql admin@srv-db02:/tmp/', [(H, 'srv-db02')]),
 ('rsync -av ./dir backup02:/data', [(H, 'backup02')]),
 ('scp: connection error: timeout', []),
 ('mstsc /v:WKS-042:3389 /admin', [(H, 'WKS-042')]),
 ('psexec \\\\wks-017 cmd', [(H, 'wks-017')]),
 ('Enter-PSSession -ComputerName SRV-EXCH01', [(H, 'SRV-EXCH01')]),
 ('Test-NetConnection srv-web01 -Port 443', [(H, 'srv-web01')]),
 ('nslookup dc1.corp.lan 10.0.0.1', [(H, 'dc1.corp.lan')]),
 ('nmap -sV -p 22 fw-01', [(H, 'fw-01')]),
 ('ping google.com', []),
 ('ping 10.0.0.1', []),
 ('telnet smtp-relay01 25', [(H, 'smtp-relay01')]),
 ('dig deeper into the logs', []),
 ('ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ', []),
 ('ssh://admin@srv01/', []),
 ('root@srv-app01:~$ cat /etc/passwd', [(H, 'srv-app01')]),
 ('[admin@wks-042 ~]$ ls', [(H, 'wks-042')]),
 ('user@localhost:~$ ls', []),
 ('mario.rossi@acme.it $ 100', []),
 ('Sep  2 10:15:32 srv-app01 sshd[1234]: Accepted publickey', [(H, 'srv-app01')]),
 ('<34>1 2026-09-02T10:15:32Z fw-01 fortigate - - - traffic', [(H, 'fw-01')]),
 ('Sep  2 10:15:32 10.0.0.5 sshd[1]: x', []),
 ('CN=WKS-042,OU=Workstations,DC=corp,DC=lan', [(H, 'WKS-042')]),
 ('CN=Mario Rossi,OU=Users,DC=corp,DC=lan', []),
 ('CN=Administrator,CN=Users,DC=corp,DC=lan', []),
 ('CN=SRV-EXCH01,CN=Computers,DC=corp,DC=lan', [(H, 'SRV-EXCH01')]),
 ('hostname: srv-app01', [(H, 'srv-app01')]),
 ('host srv01, srv02 e srv03 spenti', [(H, 'srv01'), (H, 'srv02'), (H, 'srv03')]),
 ('il server di posta mail01 è compromesso', [(H, 'mail01')]),
 ('la macchina virtuale di test vm-42', [(H, 'vm-42')]),
 ('nodo k8s-worker-3 in NotReady', [(H, 'k8s-worker-3')]),
 ('workstation WKS-042 isolata', [(H, 'WKS-042')]),
 ('il jumphost è stato isolato', []),
 ('il server web risponde', []),
 ('Windows Server 2019 e SQL Server 2019', []),
 ('server HP DL380 e server R740', []),
 ('host europeo di Azure', []),
 ('server Apache 2.4', []),
 ('PC 2019-2020', []),
 ('il computer di Mario', []),
 ('nodo 2 del cluster', []),
 ('server DNS e server DHCP', []),
 ('macchina Windows10 aggiornata', []),
 ('host 10.0.0.1:22 raggiungibile', []),
 ('VLAN10 sul router rtr-core-01', [(H, 'rtr-core-01')]),
 ('il firewall FW-MI-01 e lo switch SW-CORE-02', [(H, 'FW-MI-01'), (H, 'SW-CORE-02')]),
 ('"hostname": "srv-app01"', [(H, 'srv-app01')]),
 ('host=srv-app01 level=error', [(H, 'srv-app01')]),
 ('ticket INC-88421 sul server', []),
 ('il server INC-88421', []),
 ('computer name: PC-ROSSI-01', [(H, 'PC-ROSSI-01')]),
 ('nome host DESKTOP-NBI04', [(H, 'DESKTOP-NBI04')]),
 ('server KB5001234 patch', []),
 ("l'endpoint EDR-042 ha segnalato", [(H, 'EDR-042')]),
 ('Postazione 12', []),
 ('il server i7-8700 vecchio', []),
 ('host CVE-2024-3094', []),
 ('vm ubuntu22 di test', []),
 ('srv WKS042', [(H, 'WKS042')]),
 ('sensor e agent sono aggiornati', []),
 ('IMEI 353918107123453 del telefono', [(D, '353918107123453')]),
 ('il numero 353918107123453 nudo', [(D, '353918107123453')]),
 ('IMEI: 35-391810-712345-3', [(D, '35-391810-712345-3')]),
 ('IMEI 35391810 712345 3', [(D, '35391810 712345 3')]),
 ('IMEI 353918107123450 (errato)', [(D, '353918107123450')]),
 ('numero 353918107123450 nudo errato', []),
 ('codice 440123456789013 nudo', []),
 ('IMEI 440123456789013', [(D, '440123456789013')]),
 ('IMEISV 3539181071234501', [(D, '3539181071234501')]),
 ('ordine 3539181071234501 di 16 cifre', []),
 ('IMEI 860123456789014 e 990001123456782',
  [(D, '860123456789014'), (D, '990001123456782')]),
 ('protocollo 012345678901237', [(D, '012345678901237')]),
 ('carta 4111111111111111', []),
 ('amex 378282246310005', []),
 ('tel 3391234567', []),
 ('ICCID 8939100000000000018', [(D, '8939100000000000018')]),
 ('SIM 89391000000000001237', [(D, '89391000000000001237')]),
 ('codice 8944100012345678903 nudo', [(D, '8944100012345678903')]),
 ('codice 8944100012345678900 nudo errato', []),
 ('ICCID 8944100012345678900 errato', [(D, '8944100012345678900')]),
 ('ICCID 8939 1000 0000 0000 018', [(D, '8939 1000 0000 0000 018')]),
 ('EID 89049032004008882600012345678901', []),
 ('IMSI 222011234567890', [(D, '222011234567890')]),
 ('imsi: 22201 1234567890', []),
 ('serial number C02XG1FHJGH5', [(D, 'C02XG1FHJGH5')]),
 ('S/N: 5CG1234ABC', [(D, '5CG1234ABC')]),
 ('SN 7B2J4M3', [(D, '7B2J4M3')]),
 ('Service Tag 7B2J4M3, Express Service Code 15910123456',
  [(D, '7B2J4M3'), (D, '15910123456')]),
 ('numero di serie del portatile: PF2ABCDE', [(D, 'PF2ABCDE')]),
 ('il seriale è R9WN70ABCDE', [(D, 'R9WN70ABCDE')]),
 ('matricola del dispositivo FTX1234A5BC', [(D, 'FTX1234A5BC')]),
 ('matricola 12345 del dipendente', []),
 ('porta seriale RS232 e console serial', []),
 ('serial port COM3', []),
 ('Serial ATA', []),
 ('asset tag IT-004521', [(D, 'IT-004521')]),
 ('serial 1 of 3', []),
 ('seriale 1234567890', [(D, '1234567890')]),
 ('sn minuscolo 5CG1234ABC', []),
 ('serial: XXXXXXXXXXXX', []),
 ('numero di serie 8', []),
 ('UDID 00008030-001A2B3C4D5E802E', [(D, '00008030-001A2B3C4D5E802E')]),
 ('MEID A0000012345678', [(D, 'A0000012345678')]),
 ('Intune device ID 3f2504e0-4f89-11d3-9a0c-0305e82c3301',
  [(D, '3f2504e0-4f89-11d3-9a0c-0305e82c3301')]),
 ('hwid: 8f3a9c2e1b4d', [(D, '8f3a9c2e1b4d')]),
 ('Android ID a1b2c3d4e5f60718', [(D, 'a1b2c3d4e5f60718')]),
 ('device id sconosciuto', []),
 ('id dispositivo: DEV-0042-MI', [(D, 'DEV-0042-MI')]),
 ('host srv-app01 con IMEI 353918107123453 e S/N 5CG1234ABC',
  [(H, 'srv-app01'), (D, '353918107123453'), (D, '5CG1234ABC')]),
 ('', []),
 ('un secondo serial VK7JG-NPHTM-C97JM-9MPGT-3V66T compariva', []),
 ('serial number: ABCDE-12345-FGHIJ-67890-KLMNO', []),
 ('hostsrv01 attaccato', []),
 ('il server di posta mail01 e il server di backup bkp-01', [(H, 'mail01'), (H, 'bkp-01')]),
 ('root@bastion:~$ id', [(H, 'bastion')]),
 ('ssh bastion', [(H, 'bastion')]),
 ('via ssh a bastion', []),
 ('Sep  2 10:15:32 localhost sshd[1]: x', []),
 ('device id: 12345678', [(D, '12345678')]),
 ('IMEI 2: 860123456789014', [(D, '860123456789014')]),
 ('workstation WKS-042 isolata; WKS-042 riavviata alle 10',
  [(H, 'WKS-042'), (H, 'WKS-042')]),
 ('ssh bastion\nil bastion è stato isolato', [(H, 'bastion'), (H, 'bastion')]),
 ('ssh mail\nla mail è arrivata', [(H, 'mail')]),
 ('host srv01.corp.lan; poi srv01 da solo e SRV01 maiuscolo',
  [(H, 'srv01.corp.lan'), (H, 'srv01'), (H, 'SRV01')]),
 ('il DC è star.corp.lan; la star è alta', [(H, 'star.corp.lan')]),
 ('S/N 5CG1234ABC e ancora 5cg1234abc', [(D, '5CG1234ABC'), (D, '5cg1234abc')]),
 ('server di posta mail01; mail01 ripristinato', [(H, 'mail01'), (H, 'mail01')]),
 ('hostname: srv-app01 mentre srv-app012 è diverso', [(H, 'srv-app01')]),
 ('IMEI 353918107123453. Poi', [(D, '353918107123453')]),
 ('ICCID 8939100000000000018, poi 2.353918107123453 e 353918107123453-1',
  [(D, '8939100000000000018')]),
 ('host srv01 e srv02, host srv01 di nuovo',
  [(H, 'srv01'), (H, 'srv02'), (H, 'srv01')])]


# --- francese, tedesco, spagnolo, olandese (lexicon.py) ---
CASES += [
    ("Rechner srv-42 ist infiziert", [(H, "srv-42")]),
    ("l'ordinateur pc-paris-07 a été isolé", [(H, "pc-paris-07")]),
    ("el servidor srv-madrid-01 no responde", [(H, "srv-madrid-01")]),
    ("de server mail01.intern is besmet", [(H, "mail01.intern")]),
    ("serveurs srv01, srv02 et srv03", [(H, "srv01"), (H, "srv02"), (H, "srv03")]),
    ("Domänencontroller dc-01 und dc-02", [(H, "dc-01"), (H, "dc-02")]),
    ("nom d'hôte : SRV-APP01", [(H, "SRV-APP01")]),
    ("hostnaam: wks-042", [(H, "wks-042")]),
    ("nombre de host: srv-01.acme.local", [(H, "srv-01.acme.local")]),
    ("Windows Server 2019 installiert", []),
    ("el equipo debe reiniciarse", []),
    ("Le serveur de production est tombé", []),
    ("servidor web www.acme.es", []),                                   # URL, non HOSTNAME
    ("Seriennummer: 5CD1234XYZ", [(D, "5CD1234XYZ")]),
    ("numéro de série ABC123456", [(D, "ABC123456")]),
    ("número de serie XYZ987654", [(D, "XYZ987654")]),
    ("serienummer: NL12345AB", [(D, "NL12345AB")]),
    ("Geräte-ID: ABCD1234EFGH", [(D, "ABCD1234EFGH")]),
    ("numéro de série du contrat 12345", []),
    ("Seriennummer siehe Rechnung", []),
]


def main():
    print("[A] set sintetico")
    for text, expect in CASES:
        got = [(e["label"], text[e["start"]:e["end"]]) for e in detect_devices(text)]
        check(got == list(expect), f"{text[:70]!r}\n        atteso {expect}\n        trovato {got}")

    print("[B] carte di credito vs IMEI/ICCID")
    imei, iccid = "353918107123453", "8939100000000000018"
    check(luhn_ok(imei) and luhn_ok(iccid), "IMEI e ICCID passano il Luhn (per questo servono le esclusioni)")
    check(is_device_number(imei) and is_device_number(iccid), "is_device_number: IMEI e ICCID")
    check(not card_ok(imei) and not card_ok(iccid), "card_ok li scarta")
    check(not card_ok("35-391810-712345-3"), "card_ok scarta l'IMEI anche a gruppi")
    for card in ("4111111111111111", "5555555555554444", "378282246310005", "4111 1111 1111 1111"):
        check(card_ok(card), f"carta di prova ancora valida: {card}")
    check(not is_device_number("378282246310005"), "Amex a 15 cifre non è un IMEI")
    check(not is_device_number("440123456789013"), "prefisso 44 non è di un ente IMEI")
    check(IMEI_RBI == {"00", "01", "35", "86", "99"}, "prefissi RBI")
    check(scan_card(f"IMEI {imei}") == [(5, 20, False)], f"scan_card: candidato ma non valido: {scan_card(f'IMEI {imei}')}")
    entry = next(d for d in DETECTORS if d[0] == "CREDITCARDNUMBER")
    check(entry[2] is card_ok, "la voce CREDITCARDNUMBER usa card_ok")

    def validated(text, label=D):
        return [e["validated"] for e in detect_devices(text) if e["label"] == label]
    check(validated(f"IMEI {imei}") == [True], "IMEI Luhn + prefisso: validated")
    check(validated("IMEI 353918107123450") == [False], "IMEI con Luhn rotto ma parola chiave: non validato")
    check(validated(f"ICCID {iccid}") == [True], "ICCID Luhn: validated")
    check(validated("ICCID 8944100012345678900") == [False], "ICCID senza Luhn ma parola chiave: non validato")
    check(validated("S/N 5CG1234ABC") == [False], "numero di serie: nessun checksum")
    check(validated("host srv-app01", H) == [False], "hostname: nessun checksum")

    print("[C] rete regex, label, gruppo")
    ents = detect_regex(f"host srv-app01 con IMEI {imei} e mail a@acme.it")
    labels = {e["label"] for e in ents}
    check({H, D, "EMAIL"} <= labels and "CREDITCARDNUMBER" not in labels,
          f"detect_regex include HOSTNAME e DEVICE_ID, non la carta: {labels}")
    for e in ents:
        check(set(e) >= {"label", "start", "end", "score", "validated", "source"}, f"campi entità {e}")
    check(DEVICE_LABELS == {H, D}, "DEVICE_LABELS")
    check(not (DEVICE_LABELS & EXACT_SPAN_LABELS), "HOSTNAME e DEVICE_ID NON sono a span esatta")
    check(not (DEVICE_LABELS & SOFT_REGEX_LABELS), "HOSTNAME e DEVICE_ID non sono soft")
    check(TAG_GROUPS["cyber"] == sorted(EXACT_SPAN_LABELS | DEVICE_LABELS), f"gruppo cyber: {TAG_GROUPS}")

    print("[D] fusione e post-check")
    def merged(text, model=()):
        kept = core._merge(list(model) + detect_regex(text), text)
        return {(e["label"], text[e["start"]:e["end"]]) for e in kept}

    vals = merged(f"IMEI {imei}.")
    check(vals == {(D, imei)}, f"IMEI: DEVICE_ID e nessuna carta: {vals}")
    vals = merged(f"il numero {imei} nudo")
    check(vals == {(D, imei)}, f"IMEI nudo (Luhn + prefisso) batte la carta: {vals}")
    vals = merged(f"SIM {iccid}")
    check(vals == {(D, iccid)}, f"ICCID: DEVICE_ID: {vals}")
    vals = merged("carta 4111111111111111")
    check(vals == {("CREDITCARDNUMBER", "4111111111111111")}, f"la carta vera resta carta: {vals}")
    vals = merged("host 10.0.0.1:22 e host srv01.")
    check(vals == {("IP_ADDRESS", "10.0.0.1"), (H, "srv01")}, f"IP batte la parola chiave, punto fuori: {vals}")
    vals = merged("sito www.acme.it e share \\\\forensics\\img")
    check(vals == {("URL", "www.acme.it"), (H, "forensics")}, f"URL resta URL, UNC -> HOSTNAME: {vals}")
    vals = merged("serial VK7JG-NPHTM-C97JM-9MPGT-3V66T")
    check(vals == {("PRODUCT_KEY", "VK7JG-NPHTM-C97JM-9MPGT-3V66T")}, f"la chiave 5x5 resta PRODUCT_KEY: {vals}")
    vals = merged("login administrator@vsphere.local / VMware1!")
    check(not any(l == H for l, _ in vals), f"la parte dopo @ non è un hostname a sé: {vals}")
    # il modello marca l'IMEI come carta: il post-check lo scarta (Luhn ok ma forma esclusa)
    text = f"telefono IMEI {imei} rubato"
    ent = {"label": "CREDITCARDNUMBER", "start": text.index(imei), "end": text.index(imei) + 15,
           "score": 0.9, "validated": False, "source": "modello"}
    check(core.post_check([dict(ent)], text) == [], "post_check scarta la carta del modello sull'IMEI")
    vals = merged(text, [dict(ent)])
    check(vals == {(D, imei)}, f"con il modello che dice carta vince DEVICE_ID: {vals}")

    class NoModel(core.PiiEngine):
        def detect_model(self, text, ctl=None):
            return []
    eng = NoModel(str(HERE / "models" / "none"))
    res = eng.analyze("host SRV-APP01, poi srv-app01 e infine Srv-App01.")
    phs = [e["ph"] for e in res["entities"]]
    check(len(phs) == 3 and len(set(phs)) == 1 and phs[0].startswith("[HOSTNAME_"),
          f"hostname case-insensitive: stesso placeholder: {phs}")
    check(res["anonymized_text"] == "host [HOSTNAME_1], poi [HOSTNAME_1] e infine [HOSTNAME_1].",
          res["anonymized_text"])
    res = eng.analyze(f"S/N 5CG1234ABC, IMEI {imei}, di nuovo 5cg1234abc")
    check(res["anonymized_text"] == "S/N [DEVICE_ID_1], IMEI [DEVICE_ID_2], di nuovo [DEVICE_ID_1]",
          res["anonymized_text"])
    check(core.decode_text(res["anonymized_text"], res["mapping"])[0] == f"S/N 5CG1234ABC, IMEI {imei}, di nuovo 5CG1234ABC",
          "decodifica (il valore memorizzato è il primo visto)")

    cfg = HERE / "models"
    model_dirs = [d for d in cfg.glob("*") if (d / "config.json").exists()] if cfg.exists() else []
    if model_dirs:
        tags = core.PiiEngine(str(model_dirs[0])).tags()
        check(DEVICE_LABELS <= set(tags["all"]) and DEVICE_LABELS <= set(tags["regex_only"]),
              f"tags(): {tags['regex_only']}")
        check(set(DEVICE_LABELS) <= set(tags["groups"]["cyber"]), f"tags()['groups']: {tags['groups']}")
    else:
        print("  (nessun modello scaricato: tags() non verificato)")

    print("[E] chat_anonymization: superfici tolleranti")
    try:
        from app import chat_anonymization as ca
    except Exception as exc:                        # dipendenze pesanti assenti
        print(f"  (chat_anonymization non importabile qui: {exc})")
    else:
        check(ca._surface_core(H, "SRV-APP01") == ca._surface_core(H, "srv-app01"), "surface_core casefold")
        check(ca._surface_core(D, "5CG1234ABC") == ca._surface_core(D, "5cg1234abc"), "surface_core casefold (serial)")
        pat = ca._pattern_for("[HOSTNAME_1]", "srv-app01")
        check(pat.search("il nodo SRV-APP01 risponde") is not None, "trova l'hostname in maiuscolo")
        check(pat.search("il nodo srv-app012 risponde") is None, "srv-app01 non trova srv-app012")
        out = ca.apply_known_surfaces("host SRV-APP01 e imei " + imei, {"[HOSTNAME_1]": "srv-app01", "[DEVICE_ID_1]": imei})
        check(out == "host [HOSTNAME_1] e imei [DEVICE_ID_1]", out)

    print(f"\nPASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
