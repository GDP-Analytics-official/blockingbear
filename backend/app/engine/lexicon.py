"""
Lessico multilingue della rete regex: francese, tedesco, spagnolo, olandese.

Le regex di contesto (credentials.py, devices.py, cyber.py) riconoscono un
dato dalla PAROLA che lo introduce: "password:", "server di posta", "codice
di verifica". Italiano e inglese vivono nei moduli storici, insieme ai
commenti che ne spiegano le scelte; le altre quattro lingue stanno qui, per
concetto e per lingua, e i moduli le concatenano alle proprie liste con
`alt()`.

Ogni voce è un FRAMMENTO di regex (non una parola da escapare): le forme
composte usano `[ _\\-]` fra le parole come i moduli storici, le lettere
accentate sono scritte come classi ("cl[ée]") perché i PDF e l'OCR le
perdono, i plurali sono espliciti. Le liste si compilano con re.IGNORECASE,
che in Python copre anche le maiuscole accentate (Ä/ä).

Regola d'ordine: le forme composte PRIMA di quelle brevi che ne sono
prefisso ("nom d'utilisateur" prima di "utilisateur", "Benutzername" prima
di "Benutzer"), altrimenti la regex si ferma alla forma corta e legge la
coda come valore. `alt()` ordina per lunghezza decrescente, che per
frammenti di questa forma è la stessa cosa.

Il modulo non importa nulla dal resto del motore: lo importano detectors.py e
devices.py (per i TLD pubblici) senza cicli.
"""

# ---------------------------------------------------------------------------
# TLD della lista chiusa del detector URL (detectors.DETECTORS, voce URL).
# Un dominio nudo si tagga URL solo con uno di questi suffissi, e HOSTNAME
# (devices.py) NON contende un nome che finisce così. Una sola lista per i
# due usi: se divergono, un dominio resta in chiaro fra le due label.
# ---------------------------------------------------------------------------
# Regola LARGA (com'era): qualsiasi etichetta davanti, maiuscole ammesse.
# "it" ed "eu" restano qui perché la lista storica li trattava così.
PUBLIC_TLD_LOOSE = ("it eu com net org info io dev app gov edu cloud online site blog biz tech shop "
                    "store").split()
# Regola STRETTA: ccTLD dei paesi delle lingue supportate e dei vicini, più i
# generici di due lettere. Due lettere sono anche un'abbreviazione ("p.es.",
# "i.e.", "u.a."): il detector URL pretende un'etichetta di almeno tre
# caratteri davanti e il suffisso minuscolo.
PUBLIC_TLD_STRICT = "fr de es nl uk be ch at lu ie pt co me tv ai".split()
# secondi livelli d'uso corrente ("shop.co.uk", "empresa.com.es")
PUBLIC_TLD_2ND = "co.uk org.uk ac.uk gov.uk nhs.uk me.uk net.uk ltd.uk plc.uk com.es org.es nom.es co.at or.at".split()
PUBLIC_TLD = frozenset(PUBLIC_TLD_LOOSE + PUBLIC_TLD_STRICT + PUBLIC_TLD_2ND)


def alt(*groups):
    """Alternativa regex dai frammenti dei gruppi dati (stringhe o iterabili di
    stringhe), senza doppioni, più lunghi prima."""
    frags = []
    for g in groups:
        frags.extend([g] if isinstance(g, str) else g)
    seen, out = set(), []
    for f in sorted(frags, key=len, reverse=True):
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return "|".join(out)


# ---------------------------------------------------------------------------
# Credenziali (credentials.py)
# ---------------------------------------------------------------------------
# PASSWORD: la parola basta, il valore deve solo avere una forma da segreto.
# In tedesco e olandese la parola si compone ("Zugangspasswort",
# "beheerderswachtwoord"): il frammento con prefisso libero copre i composti,
# che altrimenti non passerebbero il confine sinistro di parola.
KW_PASSWORD = {
    "fr": [r"mots?[ _\-]de[ _\-]passe", r"mdp", r"code[ _\-]secret", r"code[ _\-]d[’']acc[eè]s",
           r"code[ _\-]confidentiel", r"phrase[ _\-]secr[eè]te", r"phrase[ _\-]de[ _\-]passe"],
    "de": [r"passw(?:ort|örter|oerter)", r"kennw(?:ort|örter|oerter)",
           r"[A-Za-zÄÖÜäöüß\-]{2,24}passw(?:ort|örter|oerter)",
           r"[A-Za-zÄÖÜäöüß\-]{2,24}kennw(?:ort|örter|oerter)",
           r"zugangscodes?", r"geheimwort", r"losungswort"],
    "es": [r"contrase[ñn]as?", r"clave[ _\-]de[ _\-]acceso", r"claves[ _\-]de[ _\-]acceso",
           r"c[óo]digo[ _\-]de[ _\-]acceso", r"c[óo]digo[ _\-]secreto", r"palabra[ _\-]de[ _\-]paso",
           r"frase[ _\-]de[ _\-](?:paso|contrase[ñn]a)", r"clave"],
    "nl": [r"wachtwoord(?:en)?", r"[a-zë\-]{2,24}wachtwoord(?:en)?", r"toegangscodes?",
           r"geheime[ _\-]zin", r"wachtwoordzin"],
}
# PIN/OTP/codici numerici corti: forma controllata a parte (credentials._accept).
KW_PIN = {
    "fr": [r"code[ _\-]de[ _\-]v[ée]rification", r"code[ _\-](?:à|a)[ _\-]usage[ _\-]unique",
           r"mot[ _\-]de[ _\-]passe[ _\-](?:à|a)[ _\-]usage[ _\-]unique", r"code[ _\-]pin",
           r"code[ _\-]de[ _\-]confirmation", r"code[ _\-]de[ _\-]s[ée]curit[ée]",
           r"code[ _\-](?:temporaire|provisoire|otp|2fa|mfa|sms)"],
    "de": [r"einmalpassw(?:ort|örter)", r"einmalkennwort", r"einmalcode", r"best[äa]tigungscode",
           r"verifizierungscode", r"verifikationscode", r"sicherheitscode", r"pin-?code",
           r"sms-?code", r"(?-i:(?:m|sms|push|chip|i|photo|app)?TANs?)"],
    "es": [r"c[óo]digo[ _\-]de[ _\-]verificaci[óo]n", r"c[óo]digo[ _\-]de[ _\-]un[ _\-]solo[ _\-]uso",
           r"contrase[ñn]a[ _\-]de[ _\-]un[ _\-]solo[ _\-]uso", r"c[óo]digo[ _\-]de[ _\-]seguridad",
           r"c[óo]digo[ _\-]de[ _\-]confirmaci[óo]n", r"c[óo]digo[ _\-](?:pin|otp|2fa|mfa|temporal|sms)",
           r"clave[ _\-]din[áa]mica"],
    "nl": [r"verificatiecode", r"bevestigingscode", r"beveiligingscode",
           r"eenmalige[ _\-](?:code|wachtwoord)", r"eenmalig[ _\-]wachtwoord", r"pincode", r"pin-?code",
           r"sms-?code", r"tan-?code"],
}
# parole della classe PIN dopo le quali il codice può essere alfanumerico
# (OTP/2FA: "codice di verifica A7K9Q2"), oltre a otp/verif/2fa/mfa/one
PIN_ALNUM_HINTS = ("unique", "solo uso", "einmal", "eenmal", "sms", "tan", "bestätig", "bestaetig",
                   "bevestig", "confirm", "verifi", "verifiz", "verifica")

KW_USERNAME = {
    "fr": [r"noms?[ _\-]d[’']utilisateur", r"identifiants?[ _\-]de[ _\-](?:connexion|compte)",
           r"identifiant[ _\-]utilisateur", r"nom[ _\-]de[ _\-]connexion", r"nom[ _\-]de[ _\-]compte",
           r"compte[ _\-]utilisateur", r"identifiant", r"utilisateur"],
    "de": [r"benutzernamen?", r"nutzernamen?", r"anmeldenamen?", r"benutzerkennung(?:en)?",
           r"nutzerkennung", r"anmeldekennung", r"login-?namen?", r"benutzer-?id", r"nutzer-?id",
           r"benutzerkonto", r"kontonamen?", r"benutzer", r"nutzer", r"kennung"],
    "es": [r"nombres?[ _\-]de[ _\-]usuario", r"identificador[ _\-]de[ _\-]usuario", r"id[ _\-]de[ _\-]usuario",
           r"cuenta[ _\-]de[ _\-]usuario", r"nombre[ _\-]de[ _\-]cuenta",
           r"nombre[ _\-]de[ _\-]inicio[ _\-]de[ _\-]sesi[óo]n", r"usuario"],
    "nl": [r"gebruikersna(?:a)?m(?:en)?", r"inlognaam", r"loginnaam", r"aanmeldnaam", r"accountnaam",
           r"gebruikers-?id", r"gebruikersaccount", r"gebruiker"],
}
# parole chiave USERNAME che valgono anche col solo spazio come separatore
# ("utilisateur jdupont", "Benutzer mmueller"): le composte e le nude più
# frequenti, non "kennung" o "compte utilisateur" che senza due punti sono prosa
SPACE_USER_KW = [r"nom[ _\-]d[’']utilisateur", r"identifiant", r"utilisateur",
                 r"benutzername", r"nutzername", r"anmeldename", r"benutzer", r"nutzer",
                 r"nombre[ _\-]de[ _\-]usuario", r"usuario",
                 r"gebruikersnaam", r"gebruiker", r"inlognaam"]

# Segreti "forti": la parola basta (chiave API, token, chiave privata...).
KW_SECRET_STRONG = {
    "fr": [r"cl[ée]s?[ _\-](?:api|secr[èe]tes?|priv[ée]es?|de[ _\-]chiffrement|de[ _\-]cryptage|ma[îi]tre"
           r"|de[ _\-]licence|d[’']activation|ssh|pgp|gpg|de[ _\-]signature|de[ _\-]session|d[’']application"
           r"|de[ _\-]service|de[ _\-]compte)",
           r"jetons?[ _\-](?:d[’']acc[èe]s|d[’']authentification|api|secrets?|de[ _\-]session|d[’']actualisation"
           r"|porteur|oauth)",
           r"code[ _\-]de[ _\-]licence", r"code[ _\-]d[’']activation",
           r"secrets?[ _\-](?:client|partag[ée]s?|d[’']application|de[ _\-]signature)", r"jetons?"],
    "de": [r"api-?schl[üu]ssel", r"geheim(?:er|e|es)?[ _\-]?schl[üu]ssel", r"privat(?:er|e)?[ _\-]?schl[üu]ssel",
           r"(?:haupt|master|lizenz|produkt|signatur|signier|verschl[üu]sselungs|zugriffs|zugangs|dienst|konto"
           r"|anwendungs|sitzungs|ssh|pgp|gpg|app)-?schl[üu]ssel",
           r"aktivierungs(?:schl[üu]ssel|code)", r"lizenzcode",
           r"(?:zugriffs|zugangs|aktualisierungs|sitzungs|api|auth|tr[äa]ger)-?tokens?",
           r"client-?geheimnis", r"gemeinsames[ _\-]geheimnis", r"geheimnis(?:se)?"],
    "es": [r"claves?[ _\-](?:api|secretas?|privadas?|de[ _\-]cifrado|de[ _\-]encriptaci[óo]n|maestra"
           r"|de[ _\-]licencia|de[ _\-]activaci[óo]n|ssh|pgp|gpg|de[ _\-]firma|de[ _\-]aplicaci[óo]n"
           r"|de[ _\-]servicio|de[ _\-]cuenta|de[ _\-]producto|de[ _\-]sesi[óo]n)",
           r"c[óo]digo[ _\-]de[ _\-](?:licencia|activaci[óo]n)",
           r"tokens?[ _\-]de[ _\-](?:acceso|autenticaci[óo]n|sesi[óo]n|actualizaci[óo]n|api|portador)",
           r"secretos?[ _\-](?:de[ _\-])?(?:cliente|compartidos?|de[ _\-]aplicaci[óo]n)", r"secretos?"],
    "nl": [r"api-?sleutels?", r"geheime[ _\-]sleutels?", r"priv[ée]-?sleutels?", r"private[ _\-]sleutels?",
           r"(?:hoofd|master|licentie|product|activerings|ondertekenings|handtekening|versleutelings|encryptie"
           r"|toegangs|dienst|service|account|app|applicatie|sessie|ssh|pgp|gpg)-?sleutels?",
           r"licentiecode", r"activeringscode",
           r"(?:toegangs|vernieuwings|sessie|api|auth)-?tokens?",
           r"client-?geheim", r"gedeeld[ _\-]geheim", r"geheimen"],
}
# Segreti "deboli": parola comune, serve un valore lungo con entropia.
KW_SECRET_WEAK = {
    "fr": [r"cl[ée]s?", r"clefs?", r"identifiants", r"informations[ _\-]d[’']identification",
           r"signature"],
    "de": [r"schl[üu]ssel", r"zugangsdaten", r"anmeldedaten", r"anmeldeinformationen", r"signatur"],
    "es": [r"credenciales", r"datos[ _\-]de[ _\-]acceso", r"firma[ _\-]digital"],
    "nl": [r"sleutels?", r"geheim", r"inloggegevens", r"toegangsgegevens", r"aanmeldgegevens",
           r"handtekening"],
}
# Parola prima della chiave "debole" che dice che NON è un segreto ("clé
# primaire", "Fremdschlüssel", "clave foránea", "publieke sleutel"), come
# credentials._WEAK_KEY_PREV per key/chiave.
WEAK_KEY_PREV = [r"primaire", r"[ée]trang[èe]re", r"publique", r"de[ \t]+tri", r"composite", r"candidate",
                 r"prim[äa]r", r"fremd", r"[öo]ffentlicher?", r"eindeutiger?", r"zusammengesetzter?",
                 r"primaria", r"for[áa]nea", r"p[úu]blica", r"[úu]nica", r"compuesta", r"candidata",
                 r"vreemde", r"publieke", r"unieke", r"samengestelde",
                 r"le", r"la", r"une", r"der", r"die", r"den", r"des", r"ein", r"el", r"una", r"de", r"het", r"een"]
# Parola prima di una parola chiave che ne cambia il senso: "palabra clave" è
# una keyword, non una password.
KW_PREV_BLOCK = [(r"clave", r"palabras?[ \t]*$")]

# Dopo la parola chiave: parole che dicono "non è il valore" (Passwortlänge,
# mot de passe oublié, contraseña caducada, wachtwoord vergeten).
SUFFIX_NOT_VALUE = {
    "fr": [r"oubli[ée]e?s?", r"expir[ée]e?s?", r"incorrecte?s?", r"invalides?", r"obligatoires?", r"requise?s?",
           r"modifi[ée]e?s?", r"chang[ée]e?s?", r"r[ée]initialis[ée]e?s?", r"r[ée]initialisation", r"changement",
           r"modification", r"longueur", r"minimale?", r"maximale?", r"politique", r"r[èe]gles?", r"confirmation",
           r"confirmer", r"erreur", r"champ", r"saisie", r"masqu[ée]e?", r"visible", r"perdue?s?", r"faible",
           r"forte", r"s[ée]curis[ée]e?", r"temporaire", r"hach[ée]e?", r"hachage", r"chiffr[ée]e?", r"fiscal",
           r"de[ _]session", r"de[ _]transaction", r"client", r"uniques?", r"finale?", r"g[ée]n[ée]rique",
           r"anonyme", r"connect[ée]e?s?", r"invit[ée]e?", r"par[ _]d[ée]faut", r"rendu", r"bancaire",
           r"courant"],
    "de": [r"vergessen", r"abgelaufen", r"falsch", r"ung[üu]ltig", r"erforderlich", r"ge[äa]ndert",
           r"zur[üu]cksetzen", r"zur[üu]ckgesetzt", r"[äa]nderung(?:en)?", r"[äa]ndern", r"l[äa]nge",
           r"richtlinien?", r"regeln?", r"best[äa]tigung", r"best[äa]tigen", r"fehler", r"feld", r"eingabe",
           r"maske", r"anzeigen", r"verbergen", r"verwaltung", r"verwalten", r"generator", r"schutz",
           r"gesch[üu]tzt", r"sicher", r"schwach", r"stark", r"tempor[äa]r", r"verschl[üu]sselt",
           r"verschl[üu]sselung", r"wiederherstellung", r"wiederherstellen", r"pr[üu]fung", r"pr[üu]fen",
           r"hinweis", r"anforderungen?", r"komplexit[äa]t", r"verlauf", r"alter", r"eingeben",
           r"speichern", r"gespeichert", r"abfrage", r"konto", r"konten", r"rechte", r"rollen?", r"gruppen?",
           r"liste", r"anzahl", r"typ", r"datei", r"pfad", r"einstellungen?", r"variable", r"profil",
           r"tabelle", r"verzeichnis", r"handbuch", r"anleitung", r"zugriff", r"sperre", r"gesperrt",
           r"vergabe", r"wechsel", r"ablauf", r"qualit[äa]t", r"st[äa]rke", r"tresor", r"safe"],
    "es": [r"olvidad[ao]s?", r"caducad[ao]s?", r"expirad[ao]s?", r"vencid[ao]s?", r"incorrect[ao]s?",
           r"inv[áa]lid[ao]s?", r"obligatori[ao]s?", r"requerid[ao]s?", r"cambiad[ao]s?", r"modificad[ao]s?",
           r"restablecid[ao]s?", r"restablecer", r"restablecimiento", r"cambio", r"cambiar", r"longitud",
           r"m[íi]nim[ao]", r"m[áa]xim[ao]", r"pol[íi]ticas?", r"reglas?", r"confirmaci[óo]n", r"confirmar",
           r"error", r"campo", r"entrada", r"ocult[ao]", r"visible", r"d[ée]bil", r"fuerte", r"segur[ao]",
           r"temporal", r"cifrad[ao]", r"hash", r"recuperaci[óo]n", r"recuperar", r"requisitos?",
           r"complejidad", r"historial", r"gestor", r"administraci[óo]n", r"generador", r"primaria",
           r"for[áa]nea", r"externa", r"p[úu]blica", r"[úu]nica", r"compuesta", r"candidata",
           r"de[ _]sesi[óo]n", r"fiscal", r"final(?:es)?", r"gen[ée]ric[ao]", r"an[óo]nim[ao]",
           r"conectad[ao]s?", r"invitad[ao]", r"por[ _]defecto", r"predeterminad[ao]", r"bancari[ao]",
           r"corriente", r"registrad[ao]s?"],
    "nl": [r"vergeten", r"verlopen", r"onjuist", r"ongeldig", r"verplicht", r"vereist", r"gewijzigd",
           r"veranderd", r"wijzigen", r"wijziging", r"resetten", r"gereset", r"herstellen", r"herstel",
           r"lengte", r"minimale?", r"maximale?", r"beleid", r"regels?", r"bevestiging", r"bevestigen",
           r"fout", r"veld", r"invoer", r"verborgen", r"zichtbaar", r"zwak", r"sterk", r"veilig",
           r"tijdelijk", r"versleuteld", r"versleuteling", r"hash", r"beheer", r"beheren", r"generator",
           r"vereisten", r"complexiteit", r"geschiedenis", r"instellingen?", r"optie", r"profiel",
           r"tabel", r"lijst", r"aantal", r"type", r"bestand", r"pad", r"opslag", r"kluis", r"sterkte",
           r"kwaliteit", r"toegang", r"rechten", r"rollen?", r"groepen?", r"handleiding", r"uniek",
           r"anoniem", r"ingelogd", r"aangemeld", r"gast", r"standaard"],
}

# Parole ammesse tra la parola chiave e il separatore ("mot de passe du
# portail :", "Passwort für das WLAN:", "contraseña del correo:").
FILL_WORDS = {
    "fr": [r"du", r"de[ _]la", r"de[ _]l[’']", r"des", r"de", r"pour", r"le", r"la", r"les", r"mon", r"ma", r"mes",
           r"votre", r"vos", r"notre", r"nos", r"son", r"sa", r"ses", r"actuel(?:le)?", r"nouve(?:au|lle)",
           r"ancien(?:ne)?", r"temporaire", r"provisoire", r"initial(?:e)?", r"par[ _]d[ée]faut",
           r"administrateur", r"r[ée]seau", r"portail", r"site", r"compte", r"acc[èe]s", r"connexion",
           r"messagerie", r"courriel", r"banque", r"bancaire", r"domaine", r"entreprise", r"bureau", r"service",
           r"serveur", r"base", r"donn[ée]es", r"partag[ée]e?", r"local(?:e)?", r"distant(?:e)?",
           r"principal(?:e)?", r"secondaire", r"utilisateur", r"professionnel(?:le)?", r"personnel(?:le)?",
           r"syst[èe]me", r"poste", r"ordinateur"],
    "de": [r"des", r"der", r"die", r"das", r"dem", r"den", r"f[üu]rs?", r"zum", r"zur", r"vom", r"von", r"am", r"im",
           r"mein(?:e|es|em|en)?", r"ihr(?:e|es|em|en)?", r"dein(?:e|es|em|en)?", r"unser(?:e|es|em|en)?",
           r"aktuell(?:e|es|en)?", r"neu(?:e|es|en)?", r"alt(?:e|es|en)?", r"tempor[äa]r(?:e|es|en)?",
           r"vorl[äa]ufig(?:e|es|en)?", r"initial(?:e|es|en)?", r"standard", r"administrator", r"netzwerk",
           r"portal", r"konto", r"zugang", r"anmeldung", r"e-?mail", r"bank", r"dom[äa]ne", r"firma", r"b[üu]ro",
           r"dienst", r"datenbank", r"wlan", r"gemeinsam(?:e|es|en)?", r"lokal(?:e|es|en)?",
           r"entfernt(?:e|es|en)?", r"haupt", r"zweit", r"benutzer", r"nutzer", r"kunden?", r"system", r"rechner",
           r"gesch[äa]ftlich(?:e|es|en)?", r"privat(?:e|es|en)?", r"pers[öo]nlich(?:e|es|en)?",
           r"folgend(?:e|es|en)?", r"jeweilig(?:e|es|en)?"],
    "es": [r"del", r"de[ _]la", r"de[ _]los", r"de[ _]las", r"de", r"para", r"el", r"la", r"los", r"las", r"mi", r"mis",
           r"tu", r"tus", r"su", r"sus", r"nuestr[ao]", r"actual", r"nuev[ao]", r"antigu[ao]", r"viej[ao]",
           r"temporal", r"provisional", r"inicial", r"por[ _]defecto", r"predeterminad[ao]", r"administrador",
           r"red", r"portal", r"sitio", r"cuenta", r"acceso", r"correo", r"banco", r"bancari[ao]", r"dominio",
           r"empresa", r"oficina", r"servicio", r"servidor", r"base", r"datos", r"compartid[ao]", r"local",
           r"remot[ao]", r"principal", r"secundari[ao]", r"usuario", r"sistema", r"equipo", r"personal",
           r"corporativ[ao]", r"siguiente"],
    "nl": [r"van", r"van[ _]de", r"van[ _]het", r"voor", r"de", r"het", r"een", r"mijn", r"jouw", r"uw", r"onze",
           r"zijn", r"haar", r"hun", r"huidige?", r"nieuwe?", r"oude?", r"tijdelijke?", r"voorlopige?",
           r"initi[eë]le?", r"standaard", r"beheerders?", r"netwerk", r"portaal", r"site", r"account",
           r"toegang", r"inlog", r"e-?mail", r"bank", r"domein", r"bedrijfs?", r"kantoor", r"dienst",
           r"gedeelde?", r"lokale?", r"lokaal", r"externe?", r"hoofd", r"tweede", r"gebruiker", r"systeem",
           r"zakelijke?", r"priv[ée]", r"persoonlijke?", r"volgende"],
}

# Copule e verbi che introducono il valore nella prosa ("le mot de passe est",
# "das Passwort lautet", "la contraseña es", "het wachtwoord is").
NATURAL_COPULA = [r"est", r"[ée]tait", r"sera", r"serait", r"reste", r"devient", r"vaut", r"correspond[ \t]+à",
                  r"ist", r"war", r"wird", r"bleibt", r"lautet", r"lautete", r"entspricht",
                  r"es", r"era", r"ser[áa]", r"ser[íi]a", r"sigue[ \t]+siendo", r"equivale[ \t]+a",
                  r"wordt", r"blijft", r"luidt"]
# ausiliare + participio + preposizione ("a été défini à", "wurde gesetzt auf",
# "ha sido establecida en", "is ingesteld op")
NATURAL_AUX = [r"a[ \t]+[ée]t[ée]", r"est", r"wurde", r"wird", r"ist", r"ha[ \t]+sido", r"fue", r"est[áa]",
               r"es", r"werd", r"wordt", r"is"]
NATURAL_PARTICIPLE = [r"d[ée]finie?s?", r"chang[ée]e?s?", r"modifi[ée]e?s?", r"r[ée]initialis[ée]e?s?",
                      r"configur[ée]e?s?", r"mise?s?", r"remise?s?", r"fix[ée]e?s?",
                      r"gesetzt", r"ge[äa]ndert", r"zur[üu]ckgesetzt", r"konfiguriert", r"festgelegt",
                      r"eingestellt", r"vergeben",
                      r"establecid[ao]s?", r"cambiad[ao]s?", r"modificad[ao]s?", r"restablecid[ao]s?",
                      r"configurad[ao]s?", r"fijad[ao]s?", r"puest[ao]s?", r"definid[ao]s?",
                      r"ingesteld", r"gewijzigd", r"veranderd", r"gereset", r"geconfigureerd", r"gezet"]
NATURAL_PREP = [r"à", r"a", r"sur", r"en", r"auf", r"zu", r"als", r"in", r"op", r"naar", r"como", r"comme"]
# articoli e avverbi ammessi fra la copula e il valore ("est toujours", "ist
# einfach", "es la siguiente", "is gewoon")
NATURAL_ARTICLES = [r"le", r"la", r"les", r"un", r"une", r"toujours", r"encore", r"maintenant", r"d[ée]sormais",
                    r"actuellement", r"simplement", r"celle-ci", r"cette", r"ce", r"la[ \t]+suivante",
                    r"le[ \t]+suivant", r"rest[ée]e?",
                    r"der", r"die", r"das", r"ein", r"eine", r"einfach", r"immer", r"noch", r"jetzt", r"nun",
                    r"derzeit", r"aktuell", r"folgende", r"folgendes", r"wie[ \t]+folgt", r"geblieben",
                    r"el", r"los", r"las", r"una", r"simplemente", r"siempre", r"todav[íi]a", r"a[úu]n", r"ahora",
                    r"actualmente", r"la[ \t]+siguiente", r"el[ \t]+siguiente", r"esta", r"este",
                    r"de", r"het", r"een", r"gewoon", r"altijd", r"nog", r"nu", r"momenteel", r"de[ \t]+volgende",
                    r"het[ \t]+volgende", r"deze", r"dit", r"gebleven"]
# copula plurale: "les mots de passe sont obligatoires" non introduce un valore
NATURAL_PLURAL = [r"sont", r"sind", r"son", r"zijn", r"waren", r"[ée]taient", r"eran"]
# participio senza preposizione dopo cui il valore deve avere forma da segreto
PARTICIPLE_SEP = [r"rotiert", r"rotad", r"geroteerd", r"r[ée]g[ée]n[ée]r", r"regenerier", r"regenerad",
                  r"gereset", r"r[ée]initialis", r"zur[üu]ckgesetzt", r"restablecid"]

# Parole guida dentro un segnaposto ("votre_mot_de_passe", "<hier eingeben>").
PLACEHOLDER_WORDS = [r"votre", r"vos", r"ton", r"ta", r"tes", r"ihr", r"ihre", r"dein", r"deine", r"euer",
                     r"su", r"sus", r"tu", r"tus", r"vuestr[ao]", r"uw", r"jouw", r"je", r"ici", r"hier", r"aqu[íi]",
                     r"exemple", r"beispiel", r"ejemplo", r"voorbeeld", r"saisir", r"saisissez", r"eingeben",
                     r"introduzca", r"introducir", r"invoeren", r"vul[ _]in", r"remplacer", r"ersetzen",
                     r"reemplazar", r"vervangen", r"mot_de_passe_ici", r"passwort_hier", r"contraseña_aquí",
                     r"wachtwoord_hier"]

# Valori di servizio: parole che seguono la parola chiave ma non sono
# credenziali ("le mot de passe est expiré", "der Benutzer muss", "el usuario
# debe", "de gebruiker kan"). Articoli, pronomi, ausiliari, verbi frequenti,
# aggettivi di stato, sostantivi della schermata di login.
STOP_VALUES_FR = """
le la les l un une des du de d au aux et ou mais donc or ni car que qui quoi dont où ce cet cette ces
celui celle ceux celles mon ma mes ton ta tes son ses notre nos votre vos leur leurs je tu il elle on
nous vous ils elles me te se lui y en ne pas plus jamais rien aussi encore déjà toujours très trop peu
beaucoup bien mal ici là est sont était étaient sera seront été être avoir a ont avait avaient aura
auront eu peut peuvent pouvait doit doivent devait faut fait font faire va vont allait vient viennent
veut veulent voulait sait savent voit voient même mêmes
nouveau nouvelle nouveaux nouvelles ancien ancienne anciens anciennes actuel actuelle actuels actuelles
courant courante temporaire temporaires provisoire provisoires initial initiale initiaux défaut standard
correct correcte corrects correctes incorrect incorrecte incorrects incorrectes invalide invalides valide
valides expiré expirée expirés expirées oublié oubliée oubliés oubliées perdu perdue perdus perdues bloqué
bloquée bloqués bloquées verrouillé verrouillée désactivé désactivée activé activée changé changée changés
changées modifié modifiée modifiés modifiées réinitialisé réinitialisée réinitialisés réinitialisées défini
définie définis définies enregistré enregistrée enregistrés enregistrées sauvegardé sauvegardée saisi
saisie saisis saisies visible visibles masqué masquée masqués masquées caché cachée chiffré chiffrée
crypté cryptée haché hachée obligatoire obligatoires requis requise requises nécessaire nécessaires
facultatif facultative optionnel optionnelle sécurisé sécurisée sûr sûre faible faibles fort forte forts
fortes long longue longs longues court courte courts courtes différent différente différents différentes
identique identiques unique uniques vide vides manquant manquante aléatoire aléatoires généré générée
personnel personnelle final finale finaux finales moyen moyenne connecté connectée connectés connectées
déconnecté déconnectée autorisé autorisée anonyme anonymes invité invitée administrateur administrateurs
suivant suivante suivants suivantes précédent précédente ci-dessus ci-dessous ci-joint joint jointe envoyé
envoyée reçu reçue fourni fournie fournis fournies indiqué indiquée spécifié spécifiée demandé demandée
communiqué communiquée transmis transmise habituel habituelle habituels habituelles inconnu inconnue
existant existante professionnel professionnelle
interne internes externe externes actif active actifs actives inactif inactive inactifs inactives principal
principale principaux principales secondaire secondaires concerné concernée concernés concernées responsable
responsables titulaire titulaires local locale locaux locales distant distante distants distantes compromis
compromise compromises inchangé inchangée inchangés inchangées exposé exposée exposés exposées divulgué
divulguée divulgués divulguées révoqué révoquée révoqués révoquées
caractères chiffres lettres symboles majuscules minuscules minimum maximum moins exemple modèle type nom
description valeur champ champs colonne compte comptes utilisateur utilisateurs mot passe identifiant
identifiants connexion session
reçoit obtient clique saisit ouvre sélectionne choisit entre accède connecte souhaite demande indique
confirme signale rencontre utilise essaie tente travaille travaillent consulte consultent lit lisent
crée créent modifie modifient supprime suppriment envoie envoient
""".split()
STOP_VALUES_DE = """
der die das des dem den ein eine einer eines einem einen und oder aber sondern denn dass ob wenn als wie
wo wer was welche welcher welches dieser diese dieses jener mein meine meiner dein deine sein seine ihr
ihre unser unsere euer eure ich du er es wir sie mich dich sich uns euch mir dir ihm ihn ihnen nicht kein
keine keiner nie mehr noch auch schon immer sehr zu wenig viel gut schlecht hier dort da ist sind war
waren wird werden wurde wurden worden haben hat hatte hatten kann können konnte muss müssen musste soll
sollen sollte darf dürfen will wollen möchte möchten mag geht gibt macht machen tut steht bleibt kommt
liegt
neu neue neuer neues neuen alt alte alter altes alten aktuell aktuelle aktueller aktuelles aktuellen
bisherig bisherige temporär temporäre vorläufig vorläufige initial initiale standard korrekt korrekte
richtig richtige falsch falsche ungültig ungültige gültig gültige abgelaufen abgelaufene vergessen
vergessene verloren verlorene gesperrt gesperrte blockiert blockierte deaktiviert deaktivierte aktiviert
aktivierte geändert geänderte zurückgesetzt zurückgesetzte gesetzt gesetzte festgelegt gespeichert
gespeicherte eingegeben eingegebene sichtbar sichtbare verborgen versteckt verschlüsselt verschlüsselte
gehasht erforderlich erforderliche notwendig notwendige optional optionale pflicht sicher sichere unsicher
unsichere schwach schwache stark starke lang lange kurz kurze gleich gleiche identisch identische
unterschiedlich unterschiedliche verschieden verschiedene eindeutig eindeutige leer leere fehlend fehlende
zufällig zufällige generiert generierte persönlich persönliche endgültig endgültige angemeldet angemeldete
abgemeldet eingeloggt ausgeloggt berechtigt berechtigte anonym anonyme gast folgend folgende folgender
folgendes vorherig vorherige oben unten anbei beigefügt gesendet geschickt erhalten bereitgestellt
angegeben genannt gewünscht angefordert bekannt unbekannt vorhanden üblich übliche bestehend bestehende
dienstlich dienstliche geschäftlich geschäftliche
intern interne interner internes internen extern externe externer externes externen aktiv aktive aktiver
aktives aktiven inaktiv inaktive inaktiver inaktives inaktiven eingeladen eingeladene eingeladener betroffen
betroffene betroffener zuständig zuständige zuständiger lokal lokale lokaler entfernt entfernte entfernter
kompromittiert kompromittierte unverändert unveränderte offengelegt geleakt widerrufen widerrufene obligatorisch
zeichen ziffern buchstaben symbole sonderzeichen großbuchstaben kleinbuchstaben mindestens maximal
minimum maximum beispiel muster typ name bezeichnung beschreibung wert feld felder spalte konto konten
benutzer nutzer passwort kennwort kennung anmeldung sitzung
klickt öffnet wählt erhält bekommt meldet loggt bestätigt ändert vergisst benötigt braucht verwendet
nutzt bittet fragt sagt zeigt sieht versucht liest lesen arbeitet arbeiten prüft prüfen bearbeitet bearbeiten
erstellt erstellen löscht löschen sendet senden
""".split()
STOP_VALUES_ES = """
el la los las lo un una unos unas y o u pero ni que quien quién cual cuál cuyo cuya donde dónde cuando
cuándo como cómo este esta estos estas ese esa esos esas aquel aquella mi mis tu tus su sus nuestro
nuestra nuestros nuestras vuestro vuestra yo tú él ella usted nosotros ellos ellas me te se le les nos no
sí ya más muy poco mucho bien mal también tampoco siempre nunca aquí ahí allí es son era eran fue fueron
será serán sido ser estar está están estaba estaban ha han había habían hay tiene tienen tenía puede
pueden podía debe deben debía hace hacen va van viene vienen quiere quieren sabe saben
nuevo nueva nuevos nuevas viejo vieja antiguo antigua actual actuales corriente temporal temporales
provisional provisionales inicial iniciales defecto predeterminado predeterminada estándar correcto
correcta correctos correctas incorrecto incorrecta incorrectos incorrectas inválido inválida válido válida
caducado caducada caducados caducadas expirado expirada vencido vencida olvidado olvidada olvidados
olvidadas perdido perdida bloqueado bloqueada bloqueados bloqueadas desactivado desactivada activado
activada cambiado cambiada cambiados cambiadas modificado modificada modificados modificadas restablecido
restablecida reiniciado reiniciada establecido establecida guardado guardada guardados guardadas
almacenado almacenada introducido introducida ingresado ingresada visible visibles oculto oculta ocultos
ocultas cifrado cifrada encriptado encriptada hasheado obligatorio obligatoria obligatorios obligatorias
requerido requerida necesario necesaria opcional opcionales seguro segura inseguro insegura débil débiles
fuerte fuertes largo larga corto corta mismo misma mismos mismas igual iguales distinto distinta distintos
distintas diferente diferentes idéntico idéntica único única vacío vacía faltante aleatorio aleatoria
generado generada personal personales final finales medio media conectado conectada conectados conectadas
desconectado desconectada autorizado autorizada anónimo anónima invitado invitada administrador
administradores siguiente siguientes anterior anteriores arriba abajo adjunto adjunta enviado enviada
recibido recibida proporcionado proporcionada indicado indicada especificado especificada solicitado
solicitada facilitado facilitada habitual habituales desconocido desconocida existente corporativo
corporativa registrado registrada
interno interna internos internas externo externa externos externas activo activa activos activas inactivo
inactiva inactivos inactivas principal principales secundario secundaria secundarios secundarias afectado
afectada afectados afectadas responsable responsables local locales remoto remota remotos remotas comprometido
comprometida comprometidos comprometidas expuesto expuesta expuestos expuestas divulgado divulgada divulgados
divulgadas revocado revocada revocados revocadas
caracteres cifras dígitos letras símbolos mayúsculas minúsculas mínimo máximo menos ejemplo modelo tipo
nombre descripción valor campo campos columna cuenta cuentas usuario usuarios contraseña clave claves
acceso sesión inicio
ve recibe obtiene clic pulsa introduce ingresa abre selecciona elige entra accede desea solicita indica
confirma informa reporta encuentra necesita usa utiliza pide dice muestra intenta lee leen trabaja trabajan
consulta consultan crea crean modifica modifican elimina eliminan envía envían
""".split()
STOP_VALUES_NL = """
de het een en of maar want dus dat die dit deze wat wie welke waar wanneer hoe mijn jouw je uw zijn haar
ons onze hun ik jij u hij zij wij we jullie ze me mij jou hem hen zich er niet geen nooit meer nog ook al
altijd zeer erg te weinig veel goed slecht hier daar is was waren wordt worden werd werden geweest hebben
heeft had hadden kan kunnen kon moet moeten moest mag mogen wil willen wou zou zouden gaat gaan komt doet
staat blijft ligt
nieuw nieuwe oud oude huidig huidige tijdelijk tijdelijke voorlopig voorlopige initieel initiële
standaard correct correcte juist juiste onjuist onjuiste ongeldig ongeldige geldig geldige verlopen
vergeten verloren geblokkeerd geblokkeerde vergrendeld uitgeschakeld ingeschakeld gewijzigd gewijzigde
veranderd gereset ingesteld opgeslagen bewaard ingevoerd zichtbaar zichtbare verborgen versleuteld
versleutelde gehasht verplicht verplichte vereist vereiste nodig noodzakelijk optioneel optionele veilig
veilige onveilig zwak zwakke sterk sterke lang lange kort korte zelfde dezelfde hetzelfde gelijk gelijke
verschillend verschillende anders identiek identieke uniek unieke leeg lege ontbrekend ontbrekende
willekeurig willekeurige gegenereerd gegenereerde persoonlijk persoonlijke definitief definitieve eind
ingelogd aangemeld uitgelogd afgemeld bevoegd anoniem anonieme gast volgend volgende vorig vorige
hierboven hieronder bijgevoegd verstuurd verzonden ontvangen verstrekt opgegeven aangegeven gevraagd
aangevraagd bekend onbekend aanwezig gebruikelijk gebruikelijke bestaand bestaande zakelijk zakelijke
geregistreerd geregistreerde
intern interne extern externe actief actieve inactief inactieve beheerder beheerders hoofd secundair secundaire
betrokken verantwoordelijke verantwoordelijk lokaal lokale gecompromitteerd ongewijzigd ongewijzigde
blootgesteld blootgestelde gelekt ingetrokken
tekens cijfers letters symbolen hoofdletters minimaal maximaal minimum maximum voorbeeld sjabloon type
naam omschrijving beschrijving waarde veld velden kolom account accounts gebruiker gebruikers wachtwoord
toegang sessie
klikt geeft opent kiest selecteert ontvangt krijgt meldt logt bevestigt wijzigt vergeet gebruikt vraagt
zegt toont ziet probeert leest lezen werkt werken bekijkt bekijken bewerkt bewerken maakt maken verwijdert
verwijderen stuurt sturen
""".split()
STOP_VALUES = frozenset(w.casefold() for w in STOP_VALUES_FR + STOP_VALUES_DE + STOP_VALUES_ES + STOP_VALUES_NL)

# Coppie ("identifiants : jdupont / Ete2024!", "Zugangsdaten: mmueller /
# Sommer24!") e verbi di accesso ("connecté avec jdupont", "angemeldet als").
PAIR_KW = [r"identifiants(?:[ \t]de[ \t]connexion)?", r"informations[ \t]de[ \t]connexion", r"acc[èe]s", r"compte",
           r"zugangsdaten", r"anmeldedaten", r"anmeldeinformationen", r"zugang", r"konto", r"benutzerkonto",
           r"credenciales", r"datos[ \t]de[ \t]acceso", r"acceso", r"cuenta",
           r"inloggegevens", r"toegangsgegevens", r"aanmeldgegevens", r"inlog", r"toegang",
           r"(?:utilisateur|identifiant|benutzer(?:name)?|nutzer(?:name)?|usuario|gebruiker(?:snaam)?)"
           r"[ \t]*(?:/|et|und|y|en|&|-|,)[ \t]*"
           r"(?:mot[ \t]de[ \t]passe|mdp|passwort|kennwort|contrase[ñn]a|clave|wachtwoord|pwd|pw)"]
PAIR_CONJ = [r"et", r"avec", r"und", r"mit", r"y", r"con", r"en", r"met",
             r"mot[ \t]de[ \t]passe", r"mdp", r"passwort", r"kennwort", r"contrase[ñn]a", r"clave", r"wachtwoord"]
LOGIN_VERBS = [r"connect(?:[ée]e?s?|er|ez|e|ons|ent)(?:-vous)?", r"se[ \t]connecter", r"s[’']authentifier",
               r"authentifi(?:[ée]e?s?|er|ez)", r"identifi(?:[ée]e?s?|er|ez)(?:-vous)?",
               r"anmeld(?:en|et|e)", r"angemeldet", r"einlogg(?:en|t)", r"eingeloggt", r"authentifizier(?:en|t)",
               r"iniciar?[ \t]sesi[óo]n", r"inici[óo][ \t]sesi[óo]n", r"inicia[ \t]sesi[óo]n",
               r"acced(?:er|e|a|i[óo]|iendo)", r"conect(?:arse|ad[ao]|ar|a)", r"autentic(?:arse|ad[ao]|ar|a)",
               r"ingres(?:ar|a|[óo]|ado)",
               r"inlogg(?:en|t)", r"ingelogd", r"aanmeld(?:en|t)", r"aangemeld", r"authenticeren",
               r"geauthenticeerd", r"meld[ \t](?:je|u)[ \t]aan"]
USE_VERBS = [r"utilis(?:er|ez|e|ant)", r"verwend(?:en|et|e)", r"benutz(?:en|t|e)", r"nutz(?:en|t|e)",
             r"us(?:ar|ando)", r"utiliz(?:ar|a|e|ando)", r"gebruik(?:en|t)"]
LOGIN_PREP = [r"avec", r"comme", r"en[ \t]tant[ \t]que", r"mit", r"als", r"con", r"como", r"met",
              r"l[’']utilisateur", r"utilisateur", r"l[’']identifiant", r"identifiant", r"les[ \t]identifiants",
              r"identifiants", r"benutzer", r"nutzer", r"zugangsdaten", r"anmeldedaten",
              r"el[ \t]usuario", r"usuario", r"credenciales", r"de[ \t]gebruiker", r"gebruiker", r"inloggegevens"]
LOGIN_FILL = [r"l[’']utilisateur", r"utilisateur", r"l[’']identifiant", r"identifiant", r"le", r"la", r"les",
              r"du", r"de", r"der", r"die", r"den", r"dem", r"des", r"benutzer", r"nutzer",
              r"el", r"los", r"usuario", r"het", r"gebruiker"]

# ---------------------------------------------------------------------------
# Dispositivi (devices.py)
# ---------------------------------------------------------------------------
HOST_KW = {
    "fr": [r"noms?[ \-]d[’']h[ôo]te", r"nom[ \-]de[ \-](?:la[ \-])?machine", r"nom[ \-]d[’']ordinateur",
           r"nom[ \-]du[ \-](?:serveur|poste)", r"serveurs?", r"machines?", r"postes?(?:[ \-]de[ \-]travail)?",
           r"ordinateurs?", r"portables?", r"n[œoe]uds?", r"imprimantes?", r"pare-?feux?", r"routeurs?",
           r"commutateurs?", r"passerelles?", r"contr[ôo]leurs?[ \-]de[ \-]domaine", r"h[ôo]tes?",
           r"machines?[ \-]virtuelles?", r"hyperviseur", r"baie", r"stockage", r"serveur[ \-]de[ \-]rebond",
           r"[ée]quipements?", r"terminaux", r"terminal", r"stations?"],
    "de": [r"hostnamen?", r"rechnernamen?", r"computernamen?", r"servernamen?", r"ger[äa]tenamen?",
           r"maschinennamen?", r"knotennamen?", r"rechner", r"arbeitsplatz(?:rechner)?", r"arbeitspl[äa]tze",
           r"arbeitsstation(?:en)?", r"maschinen?", r"knoten", r"drucker", r"firewalls?", r"router",
           r"switch(?:es)?", r"gateways?", r"dom[äa]nencontroller", r"domaincontroller",
           r"virtuelle[ \-]maschinen?", r"hypervisor", r"ger[äa]te?", r"endger[äa]te?", r"clients?",
           r"speicher", r"netzlaufwerk", r"terminal(?:s|server)?", r"(?:datenbank|mail|file|datei|web|print"
           r"|druck|backup|anwendungs|applikations|dom[äa]nen|proxy)-?server", r"jumphost", r"sprungserver"],
    "es": [r"nombres?[ \-]de[ \-](?:host|equipo|m[áa]quina|servidor|nodo|dispositivo|ordenador|pc)",
           r"servidor(?:es)?", r"equipos?", r"m[áa]quinas?", r"ordenador(?:es)?", r"computador(?:as?|es)?",
           r"port[áa]til(?:es)?", r"nodos?", r"impresoras?", r"cortafuegos", r"enrutador(?:es)?",
           r"conmutador(?:es)?", r"pasarelas?", r"puerta[ \-]de[ \-]enlace", r"controlador(?:es)?[ \-]de[ \-]dominio",
           r"m[áa]quinas?[ \-]virtual(?:es)?", r"hipervisor", r"puestos?(?:[ \-]de[ \-]trabajo)?",
           r"estaci[óo]n(?:es)?[ \-]de[ \-]trabajo", r"terminal(?:es)?", r"dispositivos?", r"almacenamiento",
           r"servidor[ \-]de[ \-]salto", r"basti[óo]n"],
    "nl": [r"hostna(?:a)?m(?:en)?", r"computernaam", r"servernaam", r"machinenaam", r"apparaatnaam",
           r"werkstations?", r"werkplek(?:ken)?", r"knooppunt(?:en)?", r"domeincontrollers?",
           r"virtuele[ \-]machines?", r"apparaat", r"apparaten", r"opslag", r"toestel(?:len)?", r"springplank",
           r"(?:mail|bestands|file|web|database|print|backup|applicatie|domein|proxy)-?server"],
}
HOST_FILLER = {
    "fr": [r"de", r"du", r"des", r"de[ \t]la", r"de[ \t]l[’']", r"le", r"la", r"les", r"l[’']", r"pour", r"sur", r"dans",
           r"principal(?:e)?", r"secondaire", r"nouve(?:au|lle)", r"ancien(?:ne)?", r"messagerie", r"mail",
           r"sauvegarde", r"fichiers?", r"web", r"base[ \t]de[ \t]donn[ée]es", r"impression", r"domaine",
           r"production", r"prod", r"test", r"recette", r"pr[ée]production", r"d[ée]veloppement", r"dev",
           r"virtuel(?:le)?", r"physique", r"linux", r"windows", r"distant(?:e)?", r"local(?:e)?", r"interne",
           r"externe", r"compromise?", r"infect[ée]e?", r"touch[ée]e?", r"affect[ée]e?", r"concern[ée]e?",
           r"impliqu[ée]e?", r"nomm[ée]e?", r"appel[ée]e?", r"d[ée]nomm[ée]e?", r"est", r"[ée]tait", r"isol[ée]e?",
           r"central(?:e)?"],
    "de": [r"der", r"die", r"das", r"des", r"dem", r"den", r"vom", r"zum", r"zur", r"f[üu]r", r"auf", r"im", r"in",
           r"prim[äa]r(?:e|er|es)?", r"sekund[äa]r(?:e|er|es)?", r"neu(?:e|er|es)?", r"alt(?:e|er|es)?", r"mail",
           r"e-mail", r"backup", r"datei", r"web", r"datenbank", r"db", r"druck", r"dom[äa]nen", r"produktions?",
           r"prod", r"test", r"staging", r"entwicklungs?", r"dev", r"virtuell(?:e|er|es)?",
           r"physisch(?:e|er|es)?", r"physikalisch(?:e|er|es)?", r"linux", r"windows", r"entfernt(?:e|er|es)?",
           r"lokal(?:e|er|es)?", r"intern(?:e|er|es)?", r"extern(?:e|er|es)?", r"kompromittiert(?:e|er|es)?",
           r"infiziert(?:e|er|es)?", r"betroffen(?:e|er|es)?", r"beteiligt(?:e|er|es)?", r"genannt(?:e|er|es)?",
           r"namens", r"mit[ \t]dem[ \t]namen", r"ist", r"war", r"hei[ßs]t", r"isoliert(?:e|er|es)?",
           r"zentral(?:e|er|es)?"],
    "es": [r"de", r"del", r"de[ \t]la", r"de[ \t]los", r"de[ \t]las", r"el", r"la", r"los", r"las", r"para", r"en",
           r"principal", r"secundari[oa]", r"nuev[oa]", r"antigu[oa]", r"viej[oa]", r"correo", r"mail",
           r"copia[ \t]de[ \t]seguridad", r"respaldo", r"backup", r"archivos", r"ficheros", r"web",
           r"base[ \t]de[ \t]datos", r"bbdd", r"db", r"impresi[óo]n", r"dominio", r"producci[óo]n", r"prod",
           r"pruebas", r"test", r"preproducci[óo]n", r"desarrollo", r"dev", r"virtual", r"f[íi]sic[oa]", r"linux",
           r"windows", r"remot[oa]", r"local", r"intern[oa]", r"extern[oa]", r"comprometid[oa]", r"infectad[oa]",
           r"afectad[oa]", r"implicad[oa]", r"involucrad[oa]", r"llamad[oa]", r"denominad[oa]",
           r"con[ \t]nombre", r"es", r"era", r"aislad[oa]", r"central"],
    "nl": [r"van", r"van[ \t]de", r"van[ \t]het", r"de", r"het", r"een", r"voor", r"op", r"in", r"primaire",
           r"secundaire", r"nieuwe", r"oude", r"mail", r"e-mail", r"backup", r"bestands", r"bestanden", r"web",
           r"database", r"db", r"print", r"domein", r"productie", r"prod", r"test", r"acceptatie", r"ontwikkel",
           r"dev", r"virtuele", r"fysieke", r"linux", r"windows", r"externe", r"lokale", r"interne",
           r"gecompromitteerde?", r"ge[ïi]nfecteerde?", r"getroffen", r"betrokken", r"genaamd", r"genoemd",
           r"met[ \t]de[ \t]naam", r"is", r"was", r"heet", r"ge[ïi]soleerde?", r"centrale"],
}
# continuazione di un elenco: "serveurs srv01, srv02 et srv03"
HOST_LIST_CONJ = [r"\bet\b", r"\bou\b", r"\bund\b", r"\boder\b", r"\by\b", r"\ben\b", r"\bof\b"]
# suffissi DNS interni ricorrenti nelle quattro lingue
INTERNAL_TLD = [r"intern", r"interne", r"lokal", r"lokaal"]

SERIAL_KW = {
    "fr": [r"num[ée]ros?[ \t]+de[ \t]+s[ée]rie", r"n[°º]?[ \t]*de[ \t]+s[ée]rie", r"no\.?[ \t]+de[ \t]+s[ée]rie",
           r"s[ée]rie[ \t]+n[°º]", r"num[ée]ro[ \t]+d[’']inventaire", r"n[°º][ \t]*d[’']inventaire",
           r"code[ \t]+d[’']inventaire", r"[ée]tiquette[ \t]+de[ \t]+service"],
    "de": [r"seriennummern?", r"serien-?nr\.?", r"serien-?nummer", r"seriennr\.?", r"ger[äa]tenummer",
           r"ger[äa]te-?nr\.?", r"inventarnummer", r"inventar-?nr\.?", r"anlagennummer"],
    "es": [r"n[úu]meros?[ \t]+de[ \t]+serie", r"n[°ºo]\.?[ \t]*de[ \t]+serie", r"num\.?[ \t]*de[ \t]+serie",
           r"serie[ \t]+n[°ºo]\.?", r"n[úu]mero[ \t]+de[ \t]+inventario", r"c[óo]digo[ \t]+de[ \t]+inventario",
           r"etiqueta[ \t]+de[ \t]+servicio", r"n[úu]mero[ \t]+de[ \t]+activo"],
    "nl": [r"serienummers?", r"serie-?nr\.?", r"serienr\.?", r"apparaatnummer", r"inventarisnummer",
           r"inventaris-?nr\.?", r"activanummer"],
}
SERIAL_FILLER = [r"du", r"de", r"des", r"de[ \t]la", r"de[ \t]l[’']", r"le", r"la", r"est", r"[ée]tait", r"suivant",
                 r"dispositif", r"appareil", r"ordinateur", r"portable", r"serveur", r"t[ée]l[ée]phone",
                 r"imprimante", r"routeur", r"machine", r"produit", r"unit[ée]", r"carte", r"ch[âa]ssis",
                 r"der", r"die", r"das", r"vom", r"ist", r"war", r"folgende", r"lautet", r"ger[äa]t(?:s|es)?",
                 r"rechner", r"laptop", r"notebook", r"servers?", r"telefons?", r"drucker", r"router",
                 r"maschine", r"produkts?", r"einheit", r"karte", r"geh[äa]use",
                 r"del", r"el", r"es", r"era", r"siguiente", r"dispositivo", r"equipo", r"ordenador",
                 r"port[áa]til", r"servidor", r"tel[ée]fono", r"impresora", r"m[áa]quina", r"producto",
                 r"unidad", r"tarjeta", r"chasis",
                 r"van", r"van[ \t]de", r"van[ \t]het", r"het", r"is", r"was", r"volgende", r"apparaat",
                 r"toestel", r"computer", r"telefoon", r"printer", r"product", r"eenheid", r"kaart",
                 r"behuizing"]
DEVID_KW = [r"identifiant[ \t]+(?:de[ \t]+l[’']|du[ \t]+)?(?:appareil|p[ée]riph[ée]rique|terminal|dispositif)",
            r"id[ \t]+(?:de[ \t]+l[’']|du[ \t]+)?(?:appareil|p[ée]riph[ée]rique|dispositif)",
            r"ger[äa]te-?id", r"ger[äa]te-?kennung", r"ger[äa]tekennung", r"ger[äa]tebezeichner", r"hardware-?kennung",
            r"id[ \t]+(?:del[ \t]+)?(?:dispositivo|equipo)", r"identificador[ \t]+(?:del[ \t]+)?(?:dispositivo|equipo)",
            r"apparaat-?id", r"apparaatidentificatie", r"toestel-?id"]
# parola che precede un ICCID senza checksum ("carte SIM", "SIM-Karte", "simkaart")
ICCID_CUE = [r"carte", r"tarjeta", r"karte", r"kaart", r"simkaart", r"puce"]

# ---------------------------------------------------------------------------
# Cybersecurity (cyber.py): contesto 2FA per i seed base32
# ---------------------------------------------------------------------------
TFA_CUE = [r"authentification[ \-](?:à|a)[ \-]deux[ \-]facteurs", r"double[ \-]authentification",
           r"validation[ \-]en[ \-]deux[ \-][ée]tapes", r"v[ée]rification[ \-]en[ \-]deux[ \-][ée]tapes",
           r"deux[ \-]facteurs", r"deuxi[èe]me[ \-]facteur", r"code[ \-]de[ \-]v[ée]rification",
           r"cl[ée][ \-]de[ \-]configuration", r"cl[ée][ \-]secr[èe]te", r"authentificateur",
           r"zwei-?faktor(?:-?authentifizierung)?", r"2-faktor", r"zweiter[ \-]faktor",
           r"best[äa]tigung[ \-]in[ \-]zwei[ \-]schritten",
           r"zweistufige[ \-](?:best[äa]tigung|verifizierung|authentifizierung)", r"best[äa]tigungscode",
           r"verifizierungscode", r"einrichtungsschl[üu]ssel", r"geheimer[ \-]schl[üu]ssel",
           r"autenticaci[óo]n[ \-](?:de|en)[ \-]dos[ \-](?:factores|pasos)",
           r"verificaci[óo]n[ \-]en[ \-]dos[ \-]pasos", r"dos[ \-]factores", r"segundo[ \-]factor",
           r"c[óo]digo[ \-]de[ \-]verificaci[óo]n", r"clave[ \-]de[ \-]configuraci[óo]n", r"clave[ \-]secreta",
           r"autenticador",
           r"tweestapsverificatie", r"twee-?staps-?verificatie", r"tweefactorauthenticatie",
           r"twee-?factor-?authenticatie", r"tweede[ \-]factor", r"verificatiecode", r"instellingssleutel",
           r"geheime[ \-]sleutel", r"verificatie[ \-]in[ \-]twee[ \-]stappen"]

# ---------------------------------------------------------------------------
# Telefono (phones.py): etichetta che precede un numero nazionale senza
# prefisso internazionale né zero iniziale
# ---------------------------------------------------------------------------
PHONE_CUE = [r"tel", r"t[ée]l", r"telefono", r"tel[ée]fono", r"telefon", r"telefoon", r"phone", r"mobile",
             r"mobil", r"m[óo]vil", r"handy", r"portable", r"cell(?:ulare|phone)?", r"gsm", r"fax", r"whatsapp",
             r"sms", r"numero", r"num[ée]ro", r"n[úu]mero", r"nummer", r"rufnummer", r"festnetz", r"fisso",
             r"fixe", r"fijo", r"vast", r"call", r"chiam\w*", r"appel\w*", r"anruf\w*", r"llam\w*", r"bel",
             r"bellen", r"ll[áa]m\w*", r"contatt\w*", r"contact\w*", r"kontakt\w*", r"recapit\w*", r"joignable", r"erreichbar"]
