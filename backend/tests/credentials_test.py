"""Credenziali (USERNAME / PASSWORD / SECRET): rete regex di contesto.

Una password non ha formato: `engine/credentials.py` la riconosce dalla frase
che la introduce (parola chiave + separatore + valore), dai formati noti
(chiavi API con prefisso, JWT, PEM, header Authorization, URI con credenziali)
e dalle coppie ("credenziali: mrossi / Abc123!"). Qui si verifica:
  - il set sintetico di frasi: italiano/inglese, configurazione, riga di
    comando, formati; per ognuna le entità attese, e nessun'altra;
  - i negativi: parole di servizio ("la password è scaduta"), segnaposto
    ("${DB_PASSWORD}", "********"), codice ("password_hash = ...");
  - l'integrazione con `core`: la fusione NON allarga la span ai confini di
    parola né toglie il simbolo finale ("Estate2024!"), il placeholder è
    case-sensitive, USERNAME cede al modello (soft), EMAIL vince su USERNAME
    sulla stessa span, tags() elenca le tre label;
  - `chat_anonymization`: superficie esatta e ricerca esatta per le credenziali.

Non serve il modello: le entità "del modello" si costruiscono a mano.
Tutte le credenziali sono INVENTATE.

Uso:  python backend/tests/credentials_test.py
"""
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]        # backend/
sys.path.insert(0, str(HERE))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.engine import core                                       # noqa: E402
from app.engine.credentials import CREDENTIAL_LABELS, detect_credentials  # noqa: E402
from app.engine.detectors import SOFT_REGEX_LABELS, detect_regex  # noqa: E402

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {msg}")


P, U, S = "PASSWORD", "USERNAME", "SECRET"

# (testo, [(label, valore), ...]) - lista vuota = nessun rilevamento atteso
CASES = [('La password del portale è Estate2024!', [('PASSWORD', 'Estate2024!')]),
 ("la mia password è 'ciao mondo 1'", [('PASSWORD', 'ciao mondo 1')]),
 ('pwd: x7#Qz', [('PASSWORD', 'x7#Qz')]),
 ('utente mrossi, password Abc123!', [('USERNAME', 'mrossi'), ('PASSWORD', 'Abc123!')]),
 ('user: mrossi pass: Abc123', [('USERNAME', 'mrossi'), ('PASSWORD', 'Abc123')]),
 ('le credenziali sono mrossi / Abc123!', [('USERNAME', 'mrossi'), ('PASSWORD', 'Abc123!')]),
 ('accedi con mrossi e Estate2024!', [('USERNAME', 'mrossi'), ('PASSWORD', 'Estate2024!')]),
 ('il mio username è m.rossi92', [('USERNAME', 'm.rossi92')]),
 ('nome utente: mario.rossi', [('USERNAME', 'mario.rossi')]),
 ('PIN: 1234', [('PASSWORD', '1234')]),
 ('il pin del bancomat è 5 4 3 2', [('PASSWORD', '5 4 3 2')]),
 ('codice OTP 482913', [('PASSWORD', '482913')]),
 ('Password: Vecchia1!', [('PASSWORD', 'Vecchia1!')]),
 ('password = pippo123', [('PASSWORD', 'pippo123')]),
 ('la password è pippolandia.', [('PASSWORD', 'pippolandia')]),
 ("la parola d'ordine è Sesamo#1", [('PASSWORD', 'Sesamo#1')]),
 ("chiave d'accesso: K3y-Acc3ss", [('PASSWORD', 'K3y-Acc3ss')]),
 ('Password WiFi: CasaRossi2024', [('PASSWORD', 'CasaRossi2024')]),
 ('la password del wifi di casa è rossi1234', [('PASSWORD', 'rossi1234')]),
 ("la password l'ho impostata a Nuova!2025", [('PASSWORD', 'Nuova!2025')]),
 ('user e password sono mrossi e Abc123!', [('USERNAME', 'mrossi'), ('PASSWORD', 'Abc123!')]),
 ('Login: mrossi\nPassword: Abc123!', [('USERNAME', 'mrossi'), ('PASSWORD', 'Abc123!')]),
 ('Username: MROSSI', [('USERNAME', 'MROSSI')]),
 ('il codice di verifica è 123 456', [('PASSWORD', '123 456')]),
 ('CVV: 123', [('PASSWORD', '123')]),
 ('Passwort: Geheim#1', [('PASSWORD', 'Geheim#1')]),
 ('contraseña: Clave!23', [('PASSWORD', 'Clave!23')]),
 ('chiave API: abcd1234efgh5678', [('SECRET', 'abcd1234efgh5678')]),
 ('user_id=12345', [('USERNAME', '12345')]),
 ('uid: mrossi', [('USERNAME', 'mrossi')]),
 ('nick: darkrider77', [('USERNAME', 'darkrider77')]),
 ("l'utente mrossi ha segnalato un problema", [('USERNAME', 'mrossi')]),
 ('utente admin, pw Adm!n', [('USERNAME', 'admin'), ('PASSWORD', 'Adm!n')]),
 ('psw: Abc12345', [('PASSWORD', 'Abc12345')]),
 ('Utente: 4471; Password: Casa!2024', [('USERNAME', '4471'), ('PASSWORD', 'Casa!2024')]),
 ('password: 12345678', [('PASSWORD', '12345678')]),
 ('la nuova password è Primavera2025!', [('PASSWORD', 'Primavera2025!')]),
 ('ti mando le credenziali: utente gverdi, password Verd3!',
  [('USERNAME', 'gverdi'), ('PASSWORD', 'Verd3!')]),
 ('Le credenziali di accesso al gestionale sono: user gverdi pwd Gest10n@le',
  [('USERNAME', 'gverdi'), ('PASSWORD', 'Gest10n@le')]),
 ("per entrare usa l'utente backup e la password Bck#2024",
  [('USERNAME', 'backup'), ('PASSWORD', 'Bck#2024')]),
 ('la password è la seguente: Segu3nte!', [('PASSWORD', 'Segu3nte!')]),
 ('password: "con spazi 12"', [('PASSWORD', 'con spazi 12')]),
 ('the password is Winter2024!', [('PASSWORD', 'Winter2024!')]),
 ('my username is jdoe and my password is Hunter2!',
  [('USERNAME', 'jdoe'), ('PASSWORD', 'Hunter2!')]),
 ('logged in as jdoe', [('USERNAME', 'jdoe')]),
 ('password (nuova): Nu0va!', [('PASSWORD', 'Nu0va!')]),
 ('il token di accesso è 9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c',
  [('SECRET', '9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c')]),
 ('token: a1b2c3d4e5f6', [('SECRET', 'a1b2c3d4e5f6')]),
 ('il PIN è 0000', [('PASSWORD', '0000')]),
 ('codice di sblocco: 987654', [('PASSWORD', '987654')]),
 ('DB_PASSWORD=S3cret!\nDB_USER=admin', [('PASSWORD', 'S3cret!'), ('USERNAME', 'admin')]),
 ('export PGPASSWORD="pg#2024"', [('PASSWORD', 'pg#2024')]),
 ('"password": "abc123", "username": "jdoe"', [('PASSWORD', 'abc123'), ('USERNAME', 'jdoe')]),
 ("password: 'abc123',", [('PASSWORD', 'abc123')]),
 ('spring.datasource.password=Pa55word', [('PASSWORD', 'Pa55word')]),
 ('Server=db;User Id=sa;Password=Sql!2024;', [('USERNAME', 'sa'), ('PASSWORD', 'Sql!2024')]),
 ('<password>abc123</password><username>jdoe</username>',
  [('PASSWORD', 'abc123'), ('USERNAME', 'jdoe')]),
 ("$db_pass = 'secret1';", [('PASSWORD', 'secret1')]),
 ("'password' => 'Wp#Pass1',", [('PASSWORD', 'Wp#Pass1')]),
 ('dbPassword: Camel#1', [('PASSWORD', 'Camel#1')]),
 ('MYSQL_PWD=mysql!pw', [('PASSWORD', 'mysql!pw')]),
 ('Pwd=Sql!2024;', [('PASSWORD', 'Sql!2024')]),
 ('smtp_user = noreply@acme.it\nsmtp_pass = Smtp#2024',
  [('USERNAME', 'noreply@acme.it'), ('PASSWORD', 'Smtp#2024')]),
 ("SECRET_KEY = 'django-insecure-x!8k2#pq9z@lm4n7v0'",
  [('SECRET', 'django-insecure-x!8k2#pq9z@lm4n7v0')]),
 ('aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
  [('SECRET', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY')]),
 ('client_secret: GOCSPX-abcdefghijklmnopqrstuvwxyz12',
  [('SECRET', 'GOCSPX-abcdefghijklmnopqrstuvwxyz12')]),
 ('OPENAI_API_KEY=sk-proj-abc123DEF456ghi789JKL012mno345PQR',
  [('SECRET', 'sk-proj-abc123DEF456ghi789JKL012mno345PQR')]),
 ('x-api-key: 3f9a8b7c6d5e4f3a2b1c', [('SECRET', '3f9a8b7c6d5e4f3a2b1c')]),
 ('api_key: AIzaSyA-1234567890abcdefghijklmnopqrstuv',
  [('SECRET', 'AIzaSyA-1234567890abcdefghijklmnopqrstuv')]),
 ('key: 8f14e45fceea167a5a36dedd4bea2543', [('SECRET', '8f14e45fceea167a5a36dedd4bea2543')]),
 # Le tre chiavi FINTE che seguono (OpenRouter, Slack, Hugging Face) sono
 # ricomposte a runtime con un +. Scritte per intero facevano scattare il
 # push protection di GitHub, che riconosce il formato e non puo' sapere
 # che qui e' il fixture del rilevatore: il push veniva rifiutato con
 # GH013. Il valore che il test vede e' identico: la verifica non cambia.
 ('la mia chiave OpenRouter è '
  'sk-or-' + 'v1-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
  [('SECRET', 'sk-or-' + 'v1-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef')]),
 ('ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef012345',
  [('SECRET', 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef012345')]),
 ('AKIAIOSFODNN7EXAMPLE', [('SECRET', 'AKIAIOSFODNN7EXAMPLE')]),
 ('Authorization: Bearer '
  'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U',
  [('SECRET',
    'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U')]),
 ('Authorization: Basic YWRtaW46QWRtMW4h', [('SECRET', 'YWRtaW46QWRtMW4h')]),
 ('-----BEGIN RSA PRIVATE KEY-----\n'
  'MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGy0AH\n'
  '-----END RSA PRIVATE KEY-----',
  [('SECRET',
    '-----BEGIN RSA PRIVATE KEY-----\n'
    'MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGy0AH\n'
    '-----END RSA PRIVATE KEY-----')]),
 ('postgres://app:Sup3r!@db.local:5432/app', [('USERNAME', 'app'), ('PASSWORD', 'Sup3r!')]),
 ('mongodb+srv://user1:P%40ss@cluster0.x.mongodb.net',
  [('USERNAME', 'user1'), ('PASSWORD', 'P%40ss')]),
 ('xoxb-' + '1234567890-abcdefghijklmnop', [('SECRET', 'xoxb-' + '1234567890-abcdefghijklmnop')]),
 ('hf_' + 'abcdefghijklmnopqrstuvwxyzABCDEFGH', [('SECRET', 'hf_' + 'abcdefghijklmnopqrstuvwxyzABCDEFGH')]),
 ('seed phrase: abandon ability able about above absent absorb abstract absurd abuse access '
  'accident',
  [('SECRET',
    'abandon ability able about above absent absorb abstract absurd abuse access accident')]),
 ('codici di recupero: ABCD-EFGH-IJKL', [('SECRET', 'ABCD-EFGH-IJKL')]),
 ('mysql -u root -pRoot!234 mydb', [('USERNAME', 'root'), ('PASSWORD', 'Root!234')]),
 ('curl -u admin:Adm1n! https://api.example.com', [('USERNAME', 'admin'), ('PASSWORD', 'Adm1n!')]),
 ('--password=Cli#Pass1 --user admin', [('PASSWORD', 'Cli#Pass1'), ('USERNAME', 'admin')]),
 ("sshpass -p 'Ssh#123' ssh root@host", [('PASSWORD', 'Ssh#123')]),
 ('net use Z: \\\\srv\\share /user:DOM\\mrossi Pa$$w0rd',
  [('USERNAME', 'DOM\\mrossi'), ('PASSWORD', 'Pa$$w0rd')]),
 ('smbclient //srv/share -U mrossi%Smb!2024', [('USERNAME', 'mrossi'), ('PASSWORD', 'Smb!2024')]),
 ('psql -U postgres -p 5432 -h localhost', [('USERNAME', 'postgres')]),
 ('ho dimenticato la password', []),
 ('la password è scaduta, devo cambiarla', []),
 ('la password deve avere almeno 8 caratteri', []),
 ('Password: ********', []),
 ('password: ${DB_PASSWORD}', []),
 ('password: <inserisci qui>', []),
 ('type="password" name="pwd"', []),
 ("l'utente finale non vede nulla", []),
 ('ogni utente deve cambiare la password al primo accesso', []),
 ('il token è scaduto', []),
 ('password_min_length = 8', []),
 ('password_hash = bcrypt(pw)', []),
 ('Utente: Mario Rossi', []),
 ('la password è sicura?', []),
 ('user experience è importante', []),
 ('password: campo obbligatorio', []),
 ('il login non funziona', []),
 ('PIN: 12', []),
 ('il cvv è sul retro della carta', []),
 ('compass=north', []),
 ('author = Mario', []),
 ('key: value', []),
 ('primary key: id_utente', []),
 ('password: (vuota)', []),
 ('monkey=banana123', []),
 ('The user can reset the password from settings', []),
 ('passa la password al metodo login(user, password)', []),
 ('Password', []),
 ('username e password non corrispondono', []),
 ('il campo utente è obbligatorio', []),
 ('le password vanno cambiate ogni 90 giorni', []),
 ('Set-Cookie: theme=light', []),
 ('user_count = 42', []),
 ('users: 1200', []),
 ('utente premium', []),
 ('token JWT non valido', []),
 ('la chiave di casa è sotto lo zerbino', []),
 ('api key mancante', []),
 ('user: 5 tentativi', []),
 ('accedi con Google', []),
 ('accedi con le credenziali aziendali', []),
 ('use with caution', []),
 ('la password è quella di sempre', []),
 ('password: vedi mail', []),
 ('Username: come sopra', []),
 ('la password è stata inviata via SMS', []),
 ('inserisci la password e premi invio', []),
 ("l'utente ha inserito la password sbagliata tre volte", []),
 ('| username | password |', []),
 ('password: la stessa di prima', []),
 ('il pin è a 6 cifre', []),
 ('password: min 8 caratteri', []),
 ('user agent: Mozilla/5.0', []),
 ('secret santa', []),
 ('token: obbligatorio', []),
 ('chiave: casa', []),
 ('Il login avviene con SPID', []),
 ('la password è la data di nascita', []),
 ('nome utente e password sono obbligatori', []),
 ('mysql -u root -p mydb', [('USERNAME', 'root')]),
 ('user story: come utente voglio', []),
 ('password reset: https://example.com/reset', []),
 ('utente: N/A', []),
 ('credenziali: non funzionano', []),
 ('accesso: negato', []),
 ('account: bloccato', []),
 ('la tua password', []),
 ('Password dimenticata? Clicca qui', []),
 ('Password: [PASSWORD_1]', []),
 ('la password è composta da 8 caratteri', []),
 ('password: 8 caratteri', []),
 ('utente: ***', []),
 ('la password è case sensitive', []),
 ('il pin è a 4 cifre', []),
 ('Accesso con SPID o CIE', []),
 ("l'utente root ha tutti i permessi", [('USERNAME', 'root')]),
 ('Utente e Password', []),
 ('username: ', []),
 ('password:', []),
 ("La password è: Abc!2024, l'utente è: mrossi",
  [('PASSWORD', 'Abc!2024'), ('USERNAME', 'mrossi')]),
 ('token=abc', []),
 ('secret: yes', []),
 ("api_key = os.environ.get('API_KEY')", []),
 ('password = getpass()', []),
 ("password=request.form['password']", []),
 ('PASSWORD = config.PASSWORD', []),
 ('user = User.objects.get(id=1)', []),
 ('chiave primaria: id', []),
 ('La chiave del successo è la costanza', []),
 ('ho perso le chiavi della macchina', []),
 ('Il tuo PIN è stato bloccato', []),
 ('OTP non ricevuto', []),
 ("key = 'value'", []),
 ('token = jwt.encode(payload, key)', []),
 ('Cookie: sessionid=k3j4h5g6f7d8s9a0q1w2e3r4; csrftoken=abc',
  [('SECRET', 'sessionid=k3j4h5g6f7d8s9a0q1w2e3r4; csrftoken=abc')]),
 ('il mio utente è il codice fiscale', []),
 ('username: RSSMRA80A01H501U', [('USERNAME', 'RSSMRA80A01H501U')]),
 ('la password è uguale allo username', []),
 ('Password scaduta il 12/03/2024', []),
 ('user: admin\npass: admin', [('USERNAME', 'admin'), ('PASSWORD', 'admin')]),
 ('connessione: postgresql://readonly:R3ad0nly@10.0.0.5/analytics',
  [('USERNAME', 'readonly'), ('PASSWORD', 'R3ad0nly')]),
 ('jdbc:mysql://db:3306/app?user=app&password=Jdbc!1',
  [('USERNAME', 'app'), ('PASSWORD', 'Jdbc!1')]),
 ('token di verifica: 55 44 33', [('PASSWORD', '55 44 33')]),
 ('--token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef012345',
  [('SECRET', 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef012345')]),
 ('Chiave di licenza: XXXXX-XXXXX-XXXXX', []),
 ('Chiave di licenza: A1B2C-D3E4F-G5H6I-J7K8L', [('SECRET', 'A1B2C-D3E4F-G5H6I-J7K8L')]),
 ('passphrase: correct horse battery staple', [('PASSWORD', 'correct horse battery staple')]),
 ("passphrase: 'correct horse battery staple'", [('PASSWORD', 'correct horse battery staple')])]


def found(text):
    return [(e["label"], text[e["start"]:e["end"]]) for e in detect_credentials(text)]

CASES += [
    # utente in forma di email dentro una coppia; "here" dentro "vsphere" non è un segnaposto
    ("le credenziali del vCenter sono administrator@vsphere.local / VMware1! ma non", [(U, "administrator@vsphere.local"), (P, "VMware1!")]),
    ("il login del vCenter è administrator@vsphere.local / VMware1!", [(U, "administrator@vsphere.local"), (P, "VMware1!")]),
    ("user: sphere_ops", [(U, "sphere_ops")]),
    # participio senza preposizione: solo con forma da segreto
    ("password locale ruotata gH7$kP2mN9#xQ4wL, collegata al PC", [(P, "gH7$kP2mN9#xQ4wL")]),
    ("password rotated Xk9#pL2m", [(P, "Xk9#pL2m")]),
    ("la password è stata ruotata stamattina.", []),
    ("la password è stata ruotata ieri sera", []),
    ("il vecchio valore era vault:v1:8SDd3ZHDS9A8I5tyvHMW6+KRaTjkN/KTWeapT3CwfxI=", [(S, "vault:v1:8SDd3ZHDS9A8I5tyvHMW6+KRaTjkN/KTWeapT3CwfxI=")]),
]

# --- francese, tedesco, spagnolo, olandese (lexicon.py) ---
CASES += [
    ("mot de passe : Ete2024!", [(P, "Ete2024!")]),
    ("identifiant : jdupont", [(U, "jdupont")]),
    ("nom d'utilisateur : j.dupont", [(U, "j.dupont")]),
    ("le mot de passe est Ete2024!", [(P, "Ete2024!")]),
    ("le mot de passe est expiré.", []),
    ("le mot de passe doit contenir 8 caractères", []),
    ("l'utilisateur jdupont a signalé un problème", [(U, "jdupont")]),
    ("utilisateur : Jean Dupont", []),
    ("identifiants : jdupont / Ete2024!", [(U, "jdupont"), (P, "Ete2024!")]),
    ("connecté avec jdupont et Ete2024!", [(U, "jdupont"), (P, "Ete2024!")]),
    ("mdp: Xk9#pL2m", [(P, "Xk9#pL2m")]),
    ("code de vérification : 482913", [(P, "482913")]),
    ("clé API : abcd1234efgh5678ijkl", [(S, "abcd1234efgh5678ijkl")]),
    ("clé secrète : abcd1234efgh5678ijkl", [(S, "abcd1234efgh5678ijkl")]),
    ("un utilisateur interne ouvre le ticket", []),
    ("chaque utilisateur externe reçoit la notice", []),
    ("le mot de passe est compromis", []),
    ("la politique de mot de passe reste inchangée", []),
    ("Passwort: Sommer2024!", [(P, "Sommer2024!")]),
    ("Benutzername: mmueller", [(U, "mmueller")]),
    ("das Kennwort lautet Xk9#pL2m", [(P, "Xk9#pL2m")]),
    ("Passwort vergessen: klicken Sie hier", []),
    ("der Benutzer muss das Passwort ändern", []),
    ("Das Passwort muss mindestens 8 Zeichen haben", []),
    ("Ihr Passwort ist geändert worden.", []),
    ("Benutzer mmueller kann sich nicht anmelden", [(U, "mmueller")]),
    ("Benutzer Müller hat angerufen", []),
    ("Benutzer: Max Mustermann", []),
    ("WLAN-Passwort: Wlan!2024", [(P, "Wlan!2024")]),
    ("Zugangspasswort ist Zug@ng24", [(P, "Zug@ng24")]),
    ("Zugangsdaten: mmueller / Sommer24!", [(U, "mmueller"), (P, "Sommer24!")]),
    ("angemeldet als mmueller", [(U, "mmueller")]),
    ("Bestätigungscode: 123456", [(P, "123456")]),
    ("TAN: 482913", [(P, "482913")]),
    ("API-Schlüssel: abcd1234efgh5678ijkl", [(S, "abcd1234efgh5678ijkl")]),
    ("Benutzername ist obligatorisch", []),
    ("Das Passwort ist kompromittiert", []),
    ("Ein eingeladener Benutzer liest den Leitfaden", []),
    ("contraseña: Verano2024!", [(P, "Verano2024!")]),
    ("usuario: jgarcia", [(U, "jgarcia")]),
    ("nombre de usuario: j.garcia", [(U, "j.garcia")]),
    ("la contraseña es incorrecta.", []),
    ("el usuario debe cambiar la contraseña", []),
    ("el usuario jgarcia no puede acceder", [(U, "jgarcia")]),
    ("usuario: Juan García", []),
    ("la clave es Verano2024!", [(P, "Verano2024!")]),
    ("palabra clave: seguridad", []),
    ("credenciales: jgarcia / Verano24!", [(U, "jgarcia"), (P, "Verano24!")]),
    ("iniciar sesión con jgarcia y Verano24!", [(U, "jgarcia"), (P, "Verano24!")]),
    ("código de verificación 482 913", [(P, "482 913")]),
    ("clave API: abcd1234efgh5678ijkl", [(S, "abcd1234efgh5678ijkl")]),
    ("cada usuario externo recibe el aviso", []),
    ("la contraseña es comprometida", []),
    ("wachtwoord: Zomer2024!", [(P, "Zomer2024!")]),
    ("gebruikersnaam: jdevries", [(U, "jdevries")]),
    ("het wachtwoord is verlopen.", []),
    ("het wachtwoord moet minimaal 8 tekens hebben", []),
    ("de gebruiker jdevries kan niet inloggen", [(U, "jdevries")]),
    ("gebruiker: Jan de Vries", []),
    ("het wachtwoord is Zomer2024!", [(P, "Zomer2024!")]),
    ("beheerderswachtwoord: Adm!n24", [(P, "Adm!n24")]),
    ("inloggegevens: jdevries / Zomer24!", [(U, "jdevries"), (P, "Zomer24!")]),
    ("ingelogd als jdevries", [(U, "jdevries")]),
    ("verificatiecode: 123456", [(P, "123456")]),
    ("API-sleutel: abcd1234efgh5678ijkl", [(S, "abcd1234efgh5678ijkl")]),
    ("de gebruiker beheerder beheert de rollen", []),
    ("het wachtwoord is gelekt", []),
    ("een uitgenodigde gebruiker leest de handleiding", []),
    ("Server=db;User Id=sa;Password=Sql!2024;", [(U, "sa"), (P, "Sql!2024")]),   # "sa" non è una parola di servizio
]


def main():
    print("[A] set sintetico")
    for text, expect in CASES:
        got = found(text)
        miss = [x for x in expect if x not in got]
        extra = [x for x in got if x not in expect]
        check(not miss and not extra,
              f"{text!r}: manca {miss} / di troppo {extra}" if (miss or extra) else "")

    print("[B] forma delle entità e rete regex")
    ents = detect_regex("user: mrossi pass: Abc123! mail mario@acme.it")
    labels = {e["label"] for e in ents}
    check({"USERNAME", "PASSWORD", "EMAIL"} <= labels, f"detect_regex include le credenziali: {labels}")
    for e in ents:
        check(set(e) >= {"label", "start", "end", "score", "validated", "source"}, f"campi entità {e}")
        check(e["source"] == "regex", "source=regex")
    check("USERNAME" in SOFT_REGEX_LABELS and "PASSWORD" not in SOFT_REGEX_LABELS
          and "SECRET" not in SOFT_REGEX_LABELS, "solo USERNAME è soft")

    print("[C] fusione: span esatte, case-sensitive, priorità")
    text = "la password è Estate2024! e l'utente è mrossi"
    kept = core._merge(detect_regex(text), text)
    vals = {(e["label"], text[e["start"]:e["end"]]) for e in kept}
    check((P, "Estate2024!") in vals, f"il simbolo finale resta nella span: {vals}")
    check((U, "mrossi") in vals, f"utente: {vals}")

    text = "mysql -u root -pRoot!234 mydb"
    kept = core._merge(detect_regex(text), text)
    vals = {(e["label"], text[e["start"]:e["end"]]) for e in kept}
    check((P, "Root!234") in vals, f"-pRoot!234: la span non si allarga al flag: {vals}")

    # il modello vede "Mario Rossi" come nome: su "Utente: Mario Rossi" vince lui
    text = "Utente: Mario Rossi, password: Abc!2024"
    model = [{"label": "FULLNAME", "start": text.index("Mario"), "end": text.index("Rossi") + 5,
              "score": 0.99, "validated": False, "source": "modello"}]
    kept = core._merge(model + detect_regex(text), text)
    vals = {(e["label"], text[e["start"]:e["end"]]) for e in kept}
    check(("FULLNAME", "Mario Rossi") in vals and not any(l == U for l, _ in vals),
          f"USERNAME soft cede a FULLNAME: {vals}")
    check((P, "Abc!2024") in vals, f"la password resta: {vals}")

    # il modello taglia una chiave a metà: la regex SECRET (hard) vince
    key = "sk-proj-abc123DEF456ghi789JKL012mno345PQR"
    text = f"OPENAI_API_KEY={key}"
    model = [{"label": "ID_DOC", "start": text.index("abc123"), "end": text.index("abc123") + 12,
              "score": 0.9, "validated": False, "source": "modello"}]
    kept = core._merge(model + detect_regex(text), text)
    vals = {(e["label"], text[e["start"]:e["end"]]) for e in kept}
    check((S, key) in vals, f"SECRET intero batte il frammento del modello: {vals}")

    # EMAIL vince su USERNAME sulla stessa span
    text = "username: mario.rossi@acme.it"
    kept = core._merge(detect_regex(text), text)
    vals = {(e["label"], text[e["start"]:e["end"]]) for e in kept}
    check(("EMAIL", "mario.rossi@acme.it") in vals and not any(l == U for l, _ in vals),
          f"EMAIL su USERNAME: {vals}")

    # analyze() senza modello: placeholder distinti per "Abc123" e "abc123"
    class NoModel(core.PiiEngine):
        def detect_model(self, text, ctl=None):
            return []
    eng = NoModel(str(HERE / "models" / "none"))
    res = eng.analyze("password: Abc123\npassword: abc123\npassword: Abc123")
    phs = [e["ph"] for e in res["entities"]]
    check(len(set(phs)) == 2 and phs[0] == phs[2], f"case-sensitive: {phs}")
    check(all(ph.startswith("[PASSWORD_") for ph in phs), f"placeholder PASSWORD: {phs}")
    res = eng.analyze("la password è Estate2024!")
    check(res["anonymized_text"] == "la password è [PASSWORD_1]", res["anonymized_text"])
    check(res["mapping"] == {"[PASSWORD_1]": "Estate2024!"}, res["mapping"])
    check(core.decode_text(res["anonymized_text"], res["mapping"])[0] == "la password è Estate2024!",
          "decodifica")

    cfg = HERE / "models"
    model_dirs = [d for d in cfg.glob("*") if (d / "config.json").exists()] if cfg.exists() else []
    if model_dirs:
        tags = core.PiiEngine(str(model_dirs[0])).tags()
        check(CREDENTIAL_LABELS <= set(tags["all"]) and CREDENTIAL_LABELS <= set(tags["regex_only"]),
              f"tags(): {tags['regex_only']}")
    else:
        print("  (nessun modello scaricato: tags() non verificato)")

    print("[D] chat_anonymization: superfici esatte")
    try:
        from app import chat_anonymization as ca
    except Exception as exc:                        # dipendenze pesanti assenti
        print(f"  (chat_anonymization non importabile qui: {exc})")
    else:
        check(ca._surface_core(P, "Estate2024!") == "Estate2024!", "surface_core esatta")
        check(ca._surface_core(P, "Abc") != ca._surface_core(P, "abc"), "surface_core case-sensitive")
        check(ca._surface_core("FULLNAME", "Mario Rossi") == ca._surface_core("FULLNAME", "rossi mario"),
              "i nomi restano tolleranti")
        pat = ca._pattern_for("[PASSWORD_1]", "Estate2024!")
        check(pat.search("pw Estate2024! ok") is not None, "ricerca esatta trova il valore")
        check(pat.search("pw estate2024! ok") is None, "ricerca esatta è case-sensitive")
        check(pat.search("Estate2024!x") is not None, "simbolo finale: nessun confine di parola richiesto")
        check(pat.search("xEstate2024!") is None, "inizio: confine di parola richiesto")
        pat = ca._pattern_for("[FULLNAME_1]", "Mario Rossi")
        check(pat.search("MARIO ROSSI") is not None, "gli altri restano case-insensitive")
        text = "ripeto: Estate2024! e estate2024!"
        out = ca.apply_known_surfaces(text, {"[PASSWORD_1]": "Estate2024!"})
        check(out == "ripeto: [PASSWORD_1] e estate2024!", out)

    print(f"\nPASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
