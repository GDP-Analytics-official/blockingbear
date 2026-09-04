import React, { useId } from 'react'
import { cn } from '@/lib/utils'

// Le bandiere sono SVG, NON emoji: su Windows 🇮🇹 e 🇬🇧 non si vedono. Segoe
// UI Emoji non contiene i caratteri bandiera e Chrome/Edge disegnano al loro
// posto le due lettere del codice paese — proprio nel punto in cui l'utente
// deve riconoscere la lingua a colpo d'occhio.
//
// Il bordo non è decorazione: la banda bianca dell'Italia e i campi chiari
// della Union Jack sparirebbero sul fondo chiaro della sidebar.
const FRAME = 'block h-auto rounded-[2px] ring-1 ring-black/15 dark:ring-white/20'

export function FlagIt({ className }) {
  return (
    <svg viewBox="0 0 3 2" className={cn(FRAME, className)} aria-hidden="true">
      <rect width="1" height="2" fill="#008C45" />
      <rect x="1" width="1" height="2" fill="#F4F5F0" />
      <rect x="2" width="1" height="2" fill="#CD212A" />
    </svg>
  )
}

export function FlagGb({ className }) {
  // l'id del clip cambia a ogni istanza: due <clipPath> con lo stesso id nella
  // pagina sono un id duplicato, e il secondo riuso è a discrezione del browser
  const clip = useId()
  return (
    <svg viewBox="0 0 60 40" className={cn(FRAME, className)} aria-hidden="true">
      <clipPath id={clip}>
        {/* i quattro mezzi quadranti che danno il CONTROCAMBIO: le diagonali
            rosse della Union Jack sono sfalsate rispetto alle bianche, non
            centrate su di esse, e senza questo taglio il disegno è sbagliato */}
        <path d="M30,20 h30 v20 z v20 h-30 z h-30 v-20 z v-20 h30 z" />
      </clipPath>
      <rect width="60" height="40" fill="#012169" />
      <path d="M0,0 L60,40 M60,0 L0,40" stroke="#FFF" strokeWidth="8" />
      <path d="M0,0 L60,40 M60,0 L0,40" stroke="#C8102E" strokeWidth="5"
            clipPath={`url(#${clip})`} />
      <path d="M30,0 V40 M0,20 H60" stroke="#FFF" strokeWidth="13" />
      <path d="M30,0 V40 M0,20 H60" stroke="#C8102E" strokeWidth="8" />
    </svg>
  )
}

// L'ordine è quello in cui compaiono nel popup e nelle bandierine: prima la
// lingua di default. Le etichette NON si traducono — «Italiano» si scrive
// così anche a un inglese, ed è l'unica riga della pagina che deve restare
// leggibile a chi non capisce la lingua corrente.
export const LANGUAGES = [
  { code: 'it', label: 'Italiano', Flag: FlagIt },
  { code: 'en', label: 'English', Flag: FlagGb },
]

export function languageOf(code) {
  return LANGUAGES.find((l) => l.code === code) || LANGUAGES[0]
}
