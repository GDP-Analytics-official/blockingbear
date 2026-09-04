import React, { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Loader2, Lock, LockOpen, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from '../api.js'
import {
  Dialog, DialogContent, DialogTitle,
} from '@/components/ui/dialog.jsx'
import { Input } from '@/components/ui/input.jsx'
import { cn } from '@/lib/utils'

// Evidenzia nel testo le occorrenze esatte dei termini cercati (i match
// fuzzy del backend restano senza evidenza: meglio niente che sbagliata).
function Highlight({ text, tokens }) {
  const parts = useMemo(() => {
    if (!text) return ['']
    const esc = tokens.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    if (esc.length === 0) return [text]
    // un solo gruppo di cattura: con split gli indici dispari sono i match
    return text.split(new RegExp(`(${esc.join('|')})`, 'gi'))
  }, [text, tokens])
  return parts.map((p, i) => (i % 2
    ? (
      <mark key={i} className="rounded-sm bg-amber-200/80 text-inherit dark:bg-amber-500/30">
        {p}
      </mark>
    )
    : <React.Fragment key={i}>{p}</React.Fragment>))
}

// Il modal di ricerca delle chat libere (lente nella sidebar). La ricerca la
// fa il backend (/api/chats/search, substring + fuzzy sui typo); qui restano
// il debounce, l'evidenziazione e la navigazione da tastiera.
export default function ChatSearchDialog({ open, onOpenChange }) {
  const { t } = useTranslation('chat')
  const navigate = useNavigate()
  const [q, setQ] = useState('')
  const [results, setResults] = useState(null) // null = nessuna ricerca fatta
  const [busy, setBusy] = useState(false)
  const [sel, setSel] = useState(0)
  const seq = useRef(0) // scarta le risposte arrivate fuori ordine

  // il modal riparte sempre vuoto: una ricerca è usa-e-getta
  useEffect(() => {
    if (open) { setQ(''); setResults(null); setSel(0) }
  }, [open])

  useEffect(() => {
    const query = q.trim()
    if (query.length < 2) {
      setResults(null)
      setBusy(false)
      return undefined
    }
    setBusy(true)
    const id = ++seq.current
    const timer = setTimeout(() => {
      api.searchChats(query)
        .then((rs) => { if (seq.current === id) { setResults(rs); setSel(0) } })
        .catch(() => { if (seq.current === id) setResults([]) })
        .finally(() => { if (seq.current === id) setBusy(false) })
    }, 250)
    return () => clearTimeout(timer)
  }, [q])

  const tokens = useMemo(
    () => q.trim().toLowerCase().split(/\s+/).filter((tk) => tk.length >= 2),
    [q],
  )

  function openChat(id) {
    onOpenChange(false)
    navigate(`/chats/${id}`)
  }

  function onKeyDown(e) {
    if (!results?.length) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setSel((s) => Math.min(s + 1, results.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setSel((s) => Math.max(s - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      openChat(results[sel].id)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* ancorato in alto come una palette: i risultati crescono verso il
          basso senza far ballare il campo di ricerca */}
      <DialogContent className="top-[16%] translate-y-0 gap-3 p-4">
        <DialogTitle className="sr-only">{t('search.title')}</DialogTitle>
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            autoFocus
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={t('search.placeholder')}
            className="pl-8 pr-8"
          />
          {busy && (
            <Loader2 className="absolute right-2.5 top-1/2 size-4 -translate-y-1/2 animate-spin text-muted-foreground" />
          )}
        </div>
        {results !== null && (
          <ul className="-mx-1 flex max-h-[50vh] flex-col gap-0.5 overflow-y-auto">
            {results.map((r, i) => (
              <li key={r.id}>
                <button
                  type="button"
                  onClick={() => openChat(r.id)}
                  onMouseEnter={() => setSel(i)}
                  className={cn(
                    'flex w-full cursor-pointer flex-col gap-0.5 rounded-md px-3 py-2 text-left',
                    i === sel && 'bg-accent',
                  )}
                >
                  <span className="flex items-center gap-2 text-sm font-medium text-foreground">
                    {r.anonymized
                      ? <Lock className="size-3 shrink-0 text-emerald-600 dark:text-emerald-400" />
                      : <LockOpen className="size-3 shrink-0 text-amber-600 dark:text-amber-400" />}
                    <span className="min-w-0 flex-1 truncate">
                      <Highlight text={r.title} tokens={tokens} />
                    </span>
                    {r.n_hits > 0 && (
                      <span className="shrink-0 text-[11px] font-normal text-muted-foreground">
                        {t('search.hits', { count: r.n_hits })}
                      </span>
                    )}
                  </span>
                  {r.snippet && (
                    <span className="line-clamp-2 text-xs text-muted-foreground">
                      <Highlight text={r.snippet} tokens={tokens} />
                    </span>
                  )}
                </button>
              </li>
            ))}
            {results.length === 0 && !busy && (
              <li className="px-3 py-6 text-center text-sm text-muted-foreground">
                {t('search.noResults')}
              </li>
            )}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  )
}
