import React, { useEffect, useRef, useState } from 'react'
import { Check } from 'lucide-react'
import { LANGUAGES, languageOf } from '@/i18n/languages.jsx'
import { useLanguage } from '@/i18n/LanguageProvider.jsx'
import { cn } from '@/lib/utils'

// La bandiera della lingua ATTIVA accanto al logout; al click si apre verso
// l'alto l'elenco delle lingue, con quella corrente marcata.
//
// La bandiera non basta da sola (una lingua non è un paese): nel menu ogni
// voce porta la sua etichetta, e il bottone la tiene nel title, dove la legge
// anche chi non riconosce la Union Jack.
export default function LanguageSwitcher({ className }) {
  const { lang, choose } = useLanguage()
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  const current = languageOf(lang)

  // chiusura al click fuori e con Escape: il menu non ha un overlay sotto
  useEffect(() => {
    if (!open) return undefined
    const onDown = (e) => { if (!ref.current?.contains(e.target)) setOpen(false) }
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div ref={ref} className={cn('relative', className)}>
      <button type="button" onClick={() => setOpen((o) => !o)}
              title={current.label} aria-label={current.label}
              aria-haspopup="menu" aria-expanded={open}
              className="flex cursor-pointer items-center rounded p-1 transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40">
        <current.Flag className="w-5" />
      </button>
      {open && (
        <div role="menu"
             className="absolute bottom-full right-0 z-50 mb-1.5 flex min-w-36 flex-col gap-0.5 rounded-md border border-border bg-popover p-1 shadow-md">
          {LANGUAGES.map(({ code, label, Flag }) => {
            const active = code === lang
            return (
              <button key={code} type="button" role="menuitem"
                      onClick={() => { setOpen(false); choose(code) }}
                      className={cn(
                        'flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-left text-sm',
                        'hover:bg-accent',
                        active ? 'font-medium text-foreground' : 'text-muted-foreground')}>
                <Flag className="w-5 shrink-0" />
                <span className="flex-1">{label}</span>
                {active && <Check className="size-3.5 shrink-0 text-primary" />}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
