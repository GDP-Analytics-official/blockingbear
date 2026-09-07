import React, { useMemo, useRef, useState } from 'react'
import FloatingPanel from './FloatingPanel.jsx'
import { ChevronDown, Search, Wrench, Check, ShieldOff } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

// Selettore fra i (molti) modelli OpenRouter: ricerca testuale + filtro
// "solo con strumenti" attivo di default (senza il tool `tools` il code
// interpreter non funziona). Nessuna lista hardcoded: tutto dai metadati del
// catalogo. Le varianti (:free, :nitro...) restano voci a sé, etichettate.
//
// I modelli senza provider Zero Data Retention seguono la deroga
// dell'installazione (prop `allowNonZdr`, = chat_allow_non_zdr): se
// l'amministratore NON l'ha abilitata vengono tolti dalla lista, perché con
// le regole privacy attive la richiesta verrebbe comunque rifiutata e
// sceglierli porterebbe solo a un blocco; se l'ha abilitata restano visibili
// ma marcati, e la deroga si concede per singola conversazione (ChatPage).
// In fondo alla lista si dice quanti sono stati nascosti: un modello che
// «manca» senza spiegazione sembra un difetto del catalogo.
//
// `allowedIds` (array di id o null = tutti) è la white list dell'amministratore
// come la manda il server per CHI chiede (/api/openrouter/models → allowed):
// per un utente normale la lista si riduce a quelli, senza contatore — non è
// una cosa da spiegare all'utente, è semplicemente il catalogo che vede.
export function noZdr(m) {
  return Array.isArray(m.zdr_providers) && m.zdr_providers.length === 0
}

export function priceLabel(m, t) {
  const inp = Number(m.pricing?.prompt)
  const out = Number(m.pricing?.completion)
  if (!Number.isFinite(inp) && !Number.isFinite(out)) return null
  if ((inp || 0) === 0 && (out || 0) === 0) return t('selector.free')
  const fmt = (v) => (Number.isFinite(v) ? (v * 1e6).toFixed(2) : '—')
  return t('selector.price', { input: fmt(inp), output: fmt(out) })
}

// Ordine della tendina: prima OpenAI, poi Anthropic, poi tutti gli altri
// nell'ordine in cui l'API li restituisce. È una scelta di PRESENTAZIONE
// (le due aziende che gli utenti cercano per prime), non un filtro: il resto
// del catalogo c'è tutto, e la ricerca testuale non ne risente.
const FIRST_AUTHORS = ['openai', 'anthropic']

export function authorRank(m) {
  const i = FIRST_AUTHORS.indexOf(m.id.split('/')[0])
  return i === -1 ? FIRST_AUTHORS.length : i
}

// `inline`: pannello nel flusso della pagina invece che a tendina (FloatingPanel)
// `allowNonZdr`: la deroga dell'installazione; finché non è nota (false) si
// sta dalla parte prudente e i modelli senza ZDR non compaiono
export default function ModelSelector({ models, value, onChange, disabled, inline = false,
                                        allowNonZdr = false, allowedIds = null }) {
  const [open, setOpen] = useState(false)
  const panelRef = useRef(null)
  const [query, setQuery] = useState('')
  const [onlyTools, setOnlyTools] = useState(true)
  const boxRef = useRef(null)
  const { t } = useTranslation('models')

  const selected = models.find((m) => m.id === value)

  const { filtered, hiddenZdr } = useMemo(() => {
    const q = query.trim().toLowerCase()
    const allowed = allowedIds ? new Set(allowedIds) : null
    const matching = models
      .filter((m) => !allowed || allowed.has(m.id))
      .filter((m) => (!onlyTools || m.tools))
      .filter((m) => !q || m.id.toLowerCase().includes(q) ||
                     (m.name || '').toLowerCase().includes(q))
    // si contano i nascosti fra quelli che l'utente avrebbe visto: il numero
    // deve spiegare la lista che ha davanti, non l'intero catalogo
    const shown = allowNonZdr ? matching : matching.filter((m) => !noZdr(m))
    // il modello in uso sta in cima, prima dell'ordine per azienda: chi apre
    // la tendina deve vedere subito da dove parte
    const rank = (m) => (m.id === value ? -1 : authorRank(m))
    return {
      hiddenZdr: matching.length - shown.length,
      // sort è stabile: dentro ogni gruppo resta l'ordine dell'API
      filtered: shown.sort((a, b) => rank(a) - rank(b)).slice(0, 200),
    }
  }, [models, query, onlyTools, allowNonZdr, allowedIds, value])

  // chiusura al click fuori
  React.useEffect(() => {
    if (!open) return
    const onDoc = (e) => {
      const inBox = boxRef.current && boxRef.current.contains(e.target)
      const inPanel = panelRef.current && panelRef.current.contains(e.target)
      if (!inBox && !inPanel) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  return (
    <div className={inline ? 'contents' : 'relative min-w-0 max-w-full'} ref={boxRef}>
      <Button variant="outline" size="sm" disabled={disabled}
              className="w-full max-w-[320px] justify-between gap-2"
              aria-expanded={open} aria-label={t('selector.placeholder')}
              onClick={() => setOpen((o) => !o)}>
        <span className="truncate">
          {selected ? selected.name : t('selector.placeholder')}
        </span>
        <ChevronDown className="opacity-60" />
      </Button>

      {open && (
        <FloatingPanel inline={inline} panelRef={panelRef}
                       className={cn(inline ? 'max-w-full' : 'w-[380px] max-w-[80vw]', 'rounded-lg border border-border bg-popover shadow-lg')}>
          <div className="flex items-center gap-2 border-b border-border px-2.5 py-2">
            <Search className="size-4 shrink-0 text-muted-foreground" />
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t('selector.search')}
              className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            />
          </div>
          <label className="flex cursor-pointer items-center gap-2 border-b border-border px-2.5 py-1.5 text-xs text-muted-foreground">
            <input type="checkbox" checked={onlyTools}
                   onChange={(e) => setOnlyTools(e.target.checked)} />
            <Wrench className="size-3" />
            {t('selector.onlyTools')}
          </label>
          <ul className="max-h-[320px] overflow-y-auto py-1">
            {filtered.length === 0 && (
              <li className="px-3 py-4 text-center text-sm text-muted-foreground">
                {t('selector.noMatch')}
              </li>
            )}
            {filtered.map((m) => (
              <li key={m.id}>
                <button
                  className={cn(
                    'touch-control flex w-full items-start gap-2 px-2.5 py-1.5 text-left hover:bg-accent',
                    m.id === value && 'bg-accent'
                  )}
                  onClick={() => { onChange(m.id); setOpen(false); setQuery('') }}
                >
                  <Check className={cn('mt-0.5 size-3.5 shrink-0',
                                       m.id === value ? 'opacity-100' : 'opacity-0')} />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <span className="truncate text-sm font-medium">{m.name}</span>
                      {m.tools && <Wrench className="size-3 shrink-0 text-muted-foreground" />}
                      {noZdr(m) && (
                        <span title={t('selector.noZdr')}>
                          <ShieldOff className="size-3 shrink-0 text-amber-600" />
                        </span>
                      )}
                    </span>
                    <span className="block truncate text-xs text-muted-foreground">
                      {m.id}{priceLabel(m, t) ? ` · ${priceLabel(m, t)}` : ''}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
          {hiddenZdr > 0 && (
            <div className="flex items-center gap-1.5 border-t border-border px-2.5 py-1.5 text-xs text-muted-foreground">
              <ShieldOff className="size-3 shrink-0" />
              {t('selector.hiddenNoZdr', { count: hiddenZdr })}
            </div>
          )}
        </FloatingPanel>
      )}
    </div>
  )
}
