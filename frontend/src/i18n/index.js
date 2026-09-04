// Fondamenta della traduzione: qui si dichiarano le lingue, i cataloghi e la
// lingua di partenza. Tutto il resto dell'app parla solo di chiavi.
//
// I cataloghi sono importati staticamente: due lingue e pochi namespace
// pesano meno del codice che servirebbe a caricarli a richiesta (e un
// namespace che arriva dopo il primo render fa lampeggiare le chiavi).
// Quando i file cresceranno, il passaggio al caricamento lazy si fa qui
// dentro senza toccare le pagine.
import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'

export const LANGS = ['it', 'en']
export const DEFAULT_LANG = 'it'

// Nessun rilevamento dalla lingua del browser (niente
// i18next-browser-languagedetector): la scelta si fa nel popup subito dopo il
// primo accesso e vive sul profilo. Dedurla da navigator.language darebbe
// un'interfaccia inglese a chi ha solo Windows in inglese, e renderebbe il
// popup una domanda a cui si è già risposto per conto suo.
//
// La scelta NON viene tenuta nel browser: l'unico posto dove vive è
// `User.lang` sul server (vedi LanguageProvider). Il browser può essere una
// postazione condivisa, e la lingua deve seguire la persona, non il computer.
// Qui si parte quindi sempre dal default: è la lingua della sola schermata di
// accesso, che è scritta per non dipendere da nessuna delle due.

// I namespace si scoprono dai file: locales/<lingua>/<namespace>.json. Un
// namespace nuovo costa due file e nient'altro — nessun elenco da tenere
// allineato qui, che è esattamente il genere di riga che si dimentica.
// `eager` rende il glob una serie di import statici: nel bundle finisce lo
// stesso codice che avrebbero prodotto a mano.
const modules = import.meta.glob('./locales/*/*.json', { eager: true })

const resources = {}
for (const [path, mod] of Object.entries(modules)) {
  const [, lang, ns] = path.match(/\.\/locales\/([^/]+)\/(.+)\.json$/)
  resources[lang] = resources[lang] || {}
  resources[lang][ns] = mod.default
}

i18n.use(initReactI18next).init({
  resources,
  lng: DEFAULT_LANG,
  // l'italiano è anche la lingua sorgente: una chiave non ancora tradotta in
  // inglese esce in italiano invece di mostrare il nome della chiave
  fallbackLng: DEFAULT_LANG,
  supportedLngs: LANGS,
  ns: Object.keys(resources[DEFAULT_LANG] || {}),
  defaultNS: 'common',
  interpolation: { escapeValue: false },   // in React è già schermato
})

export default i18n
