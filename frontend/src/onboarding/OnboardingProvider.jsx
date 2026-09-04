import React, {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
} from 'react'
import { useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { driver } from 'driver.js'
import 'driver.js/dist/driver.css'
import { api } from '@/api.js'
import { useAuth } from '@/auth.jsx'
import { useLanguage } from '@/i18n/LanguageProvider.jsx'
import { CHAPTERS, chapterFor } from './chapters.js'

// Tutorial del primo accesso, costruito su driver.js. Sta dentro
// DashboardLayout: parte solo quando l'utente è dentro l'app vera (password
// cambiata, lingua scelta) e il menu è montato.
//
// Due livelli di memoria:
//  - `user.tour_done` sul profilo (server): il tutorial è finito o è stato
//    saltato. È l'unica cosa che conta da un accesso all'altro, e segue la
//    persona e non il browser (postazioni condivise).
//  - i capitoli già visti, in localStorage per utente: servono solo a non
//    ripetere un capitolo finché il tutorial non è completo. Se si perdono,
//    il peggio che succede è rivedere un capitolo.
const OnboardingContext = createContext(null)

const storageKey = (username) => `blockingbear-tour-${username}`

function readSeen(username) {
  try {
    const raw = localStorage.getItem(storageKey(username))
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr : []
  } catch { return [] }
}

function writeSeen(username, seen) {
  try { localStorage.setItem(storageKey(username), JSON.stringify(seen)) } catch { /* storage negato */ }
}

// Un elemento su cui vale la pena fermarsi: c'è, ha una dimensione (non è
// nascosto) e non è disabilitato. Un pulsante che l'admin ha spento non deve
// diventare un "clicca qui".
function usable(selector) {
  const el = document.querySelector(selector)
  if (!el) return false
  if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false
  const r = el.getBoundingClientRect()
  return r.width > 0 && r.height > 0
}

export function OnboardingProvider({ children }) {
  const { user, setUser } = useAuth()
  const { asking } = useLanguage()
  const location = useLocation()
  const { t, i18n } = useTranslation('tour')
  const [status, setStatus] = useState(null)
  const drv = useRef(null)
  const username = user?.username

  // condizione per mostrare qualcosa: profilo dentro l'app, tutorial non
  // ancora fatto, e il popup della lingua (che è modale) non aperto
  const active = Boolean(user) && !user.tour_done && !user.must_change_password && !asking

  // la policy di anonimizzazione decide il testo dello step sul modo della
  // chat: la si legge una volta, quando serve
  useEffect(() => {
    if (!active || status != null) return
    api.orStatus().then(setStatus).catch(() => setStatus({}))
  }, [active, status])

  // Fine del tutorial (completato o saltato): sul server, e subito in
  // `user` così nessun altro capitolo parte nel frattempo.
  const finish = useCallback(() => {
    if (!username) return
    try { localStorage.removeItem(storageKey(username)) } catch { /* storage negato */ }
    setUser((u) => (u ? { ...u, tour_done: true } : u))
    api.saveMyTour(true).catch(() => {})
  }, [username, setUser])

  // Da Impostazioni: rivedere il tutorial dall'inizio.
  const restart = useCallback(() => {
    if (!username) return Promise.resolve(false)
    try { localStorage.removeItem(storageKey(username)) } catch { /* storage negato */ }
    return api.saveMyTour(false)
      .then(() => { setUser((u) => (u ? { ...u, tour_done: false } : u)); return true })
      .catch(() => false)
  }, [username, setUser])

  useEffect(() => {
    if (!active || status == null) return undefined
    const chapter = chapterFor(location.pathname)
    if (!chapter) return undefined
    if (readSeen(username).includes(chapter.key)) return undefined

    const ctx = {
      t, user,
      anonRequired: status?.chat_anonymization_policy === 'required',
    }
    const all = chapter.steps(ctx)
    const needsDom = all.some((s) => s.element)

    // La pagina può montare i suoi elementi dopo un caricamento (la chat
    // mostra uno spinner finché non ha la conversazione): si aspetta che
    // almeno uno dei bersagli compaia, per un tempo ragionevole. Se non
    // compare nessuno il capitolo non parte e non viene segnato come visto.
    let attempts = 0
    let cancelled = false
    const timer = setInterval(() => {
      attempts += 1
      const steps = all.filter((s) => !s.element || usable(s.element))
      const ready = !needsDom || steps.some((s) => s.element)
      if (!ready) {
        if (attempts >= 20) clearInterval(timer)
        return
      }
      clearInterval(timer)
      if (cancelled) return
      start(chapter, steps)
    }, 150)

    function markSeen() {
      const seen = readSeen(username)
      if (!seen.includes(chapter.key)) seen.push(chapter.key)
      writeSeen(username, seen)
      if (CHAPTERS.every((c) => seen.includes(c.key))) finish()
    }

    function start(ch, steps) {
      const d = driver({
        showProgress: steps.length > 1,
        allowClose: true,
        overlayOpacity: 0.55,
        stagePadding: 6,
        stageRadius: 10,
        popoverClass: 'blockingbear-tour',
        nextBtnText: t('btn.next'),
        prevBtnText: t('btn.prev'),
        doneBtnText: t('btn.done'),
        // i segnaposto sono di driver.js, non di i18next: la riga è la stessa in tutte le lingue
        progressText: '{{current}} / {{total}}',
        steps: steps.map((s) => ({
          element: s.element,
          popover: {
            title: s.title, description: s.description,
            side: s.side, align: s.align,
            ...(s.popoverClass ? { popoverClass: s.popoverClass } : {}),
          },
        })),
        // la X è «salta il tutorial»: chi la preme non lo vuole, e non deve
        // ritrovarselo alla prossima pagina
        onCloseClick: () => { finish(); d.destroy() },
        onDestroyed: () => {
          if (drv.current === d) drv.current = null
          // chiusura per cambio pagina (cleanup qui sotto): niente da segnare,
          // il capitolo ricomincerà quando l'utente ripasserà di qui
          if (d.__silent) return
          markSeen()
        },
      })
      drv.current = d
      d.drive()
    }

    return () => {
      cancelled = true
      clearInterval(timer)
      if (drv.current) {
        drv.current.__silent = true
        drv.current.destroy()
        drv.current = null
      }
    }
    // i18n.language: cambiando lingua a metà, il capitolo riparte tradotto
  }, [active, status, location.pathname, username, user, t, i18n.language, finish])

  const value = useMemo(() => ({ restart, done: Boolean(user?.tour_done) }), [restart, user?.tour_done])

  return (
    <OnboardingContext.Provider value={value}>{children}</OnboardingContext.Provider>
  )
}

export function useOnboarding() {
  return useContext(OnboardingContext)
}
