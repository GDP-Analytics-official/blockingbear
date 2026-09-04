import React, {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
} from 'react'
import i18n, { DEFAULT_LANG, LANGS } from './index.js'
import { api } from '@/api.js'

// La lingua vive in UN SOLO posto: `User.lang` sul profilo. Questo file la
// porta da lì a i18next e ce la riporta quando l'utente la cambia.
//
// Il browser non ne conserva copia. Una postazione può essere condivisa e la
// lingua deve seguire la persona: chi entra col proprio profilo trova la
// propria interfaccia, senza ereditare quella del collega che ha appena
// finito. Prima del login non c'è nessuna lingua da sapere — la schermata di
// accesso è scritta per non dipendere da nessuna delle due.
//
// Un profilo senza lingua (`lang` NULL: mai scelta) fa comparire il popup, una
// volta sola nella vita dell'utente. Finché non risponde non c'è niente da
// salvare, e la risposta va sul server prima di considerarla acquisita.
const LanguageContext = createContext(null)

export function LanguageProvider({ children }) {
  const [lang, setLang] = useState(DEFAULT_LANG)
  // profilo entrato senza lingua: è la condizione che apre il popup
  const [asking, setAsking] = useState(false)

  useEffect(() => {
    if (i18n.language !== lang) i18n.changeLanguage(lang)
    // <html lang>: lettori di schermo, sillabazione e correttore ortografico
    // dei campi di testo lo leggono da lì
    document.documentElement.lang = lang
  }, [lang])

  // Chiamata da auth.jsx a ogni cambio di sessione (login, /me, logout).
  // Deve restare STABILE (nessuna dipendenza): la chiama un effetto che ha
  // `user` fra le dipendenze, e un'identità che cambia lo rieseguirebbe
  // riaprendo il popup appena chiuso.
  const applyUser = useCallback((user) => {
    if (!user) {
      // logout, o avvio senza sessione: si torna alla schermata di accesso,
      // che non ha una lingua da rispettare
      setAsking(false)
      setLang(DEFAULT_LANG)
    } else if (LANGS.includes(user.lang)) {
      setAsking(false)
      setLang(user.lang)
    } else if (user.must_change_password) {
      // prima l'obbligo, poi la domanda: sulla schermata di cambio password
      // il popup non compare (e sarebbe anche l'unica risposta a rischio di
      // essere richiesta: `user.lang` resta null finché setUser non ripassa
      // da /me o dal login). Quando il flag cade, setUser rifà questo giro
      // con lang ancora null e la domanda arriva DENTRO l'app, una volta sola.
      setAsking(false)
    } else {
      setAsking(true)              // profilo nuovo: gliela chiediamo adesso
    }
  }, [])

  // Scelta ESPLICITA: popup del primo accesso, bandierine accanto al logout o
  // impostazioni. L'interfaccia cambia subito, il profilo appena il server
  // conferma. Torna `true` solo se il salvataggio è andato a buon fine: chi
  // ignora il risultato (bandierine, impostazioni) tiene comunque la lingua
  // già applicata; il popup invece resta aperto e lo dice, perché lì il
  // salvataggio è il punto.
  const choose = useCallback((code) => {
    if (!LANGS.includes(code)) return Promise.resolve(false)
    setLang(code)
    return api.saveMyLanguage(code)
      .then(() => { setAsking(false); return true })
      .catch(() => false)
  }, [])

  const value = useMemo(
    () => ({ lang, asking, choose, applyUser }),
    [lang, asking, choose, applyUser])

  return (
    <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>
  )
}

export function useLanguage() {
  return useContext(LanguageContext)
}
