// Categorie di anonimizzazione ATTIVE di partenza: quelle con meno falsi
// positivi. Tutte le altre del modello nascono spente. Il gruppo
// «Cybersecurity» (credenziali e identificativi tecnici, regex di formato)
// nasce tutto acceso: pochi falsi positivi e dati che non devono uscire.
// Si salva l'esclusione: excluded = tags − DEFAULT_ON. La usano il wizard
// iniziale (stato di partenza dello step) e la pagina Impostazioni (bottone
// «Default» accanto a «Tutte» / «Nessuna»): un'unica lista per entrambi.
export const DEFAULT_ON = new Set(['CF', 'CITY', 'CREDITCARDNUMBER', 'EMAIL', 'FULLNAME', 'IBAN',
                                   'ORG', 'PASSWORD', 'PIVA', 'SECRET', 'STREET', 'TELEPHONENUM',
                                   'URL', 'USERNAME',
                                   'CERTIFICATE', 'CRYPTO_WALLET', 'DEVICE_ID', 'HOSTNAME',
                                   'IP_ADDRESS', 'MAC_ADDRESS', 'PASSWORD_HASH', 'PRODUCT_KEY',
                                   'SSH_FINGERPRINT', 'SSH_KEY', 'TOTP_SECRET', 'WINDOWS_SID'])

// lista di esclusione di default per i tag `tags` del modello (ordinata)
export function defaultExcluded(tags) {
  return (tags || []).filter((tag) => !DEFAULT_ON.has(tag)).sort()
}
