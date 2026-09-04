import { useTranslation } from 'react-i18next'

// Date e numeri seguono la lingua scelta, non l'italiano cablato nelle pagine.
//
// Per l'inglese si usa en-GB e non en-US: giorno/mese/anno e orologio a 24 ore
// restano quelli che l'utente vede in tutto il resto dell'applicazione (e nei
// documenti che ci carica dentro). Cambiare lingua deve cambiare le parole,
// non il modo in cui si legge una data.
export const LOCALES = { it: 'it-IT', en: 'en-GB' }

export function localeOf(lang) {
  return LOCALES[lang] || LOCALES.it
}

// Il tag BCP-47 della lingua corrente, da passare a toLocaleString/Intl.
export function useLocale() {
  const { i18n } = useTranslation()
  return localeOf(i18n.language)
}
