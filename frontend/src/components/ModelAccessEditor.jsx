import React, { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Lock, Search, ShieldOff, Wrench } from 'lucide-react'
import { Checkbox } from '@/components/ui/checkbox'
import { Button } from '@/components/ui/button'
import { authorRank, noZdr, priceLabel } from './ModelSelector.jsx'
import { cn } from '@/lib/utils'

// Quali modelli vedono (e possono scegliere) gli utenti NON admin: tutti,
// oppure solo quelli flaggati qui dal catalogo. Si salva {enabled, models}
// (backend: settings_store.model_access); l'insieme che il server applica è
// la lista PIÙ il modello predefinito dell'installazione (`locked`), che
// entra da solo: qui è mostrato selezionato e non deselezionabile, perché
// una white list senza il default farebbe nascere chat su un modello che
// l'utente non può vedere. Con la regola guidata `locked` cambia col
// catalogo — lo calcola il server, questo componente lo riceve e basta.
// Stesso componente nel wizard (ultimo passo) e in Impostazioni.
//
// I modelli senza provider Zero Data Retention seguono la deroga come nel
// selettore: con la deroga spenta non compaiono — flaggarli non li renderebbe
// utilizzabili — e in fondo si dice quanti sono.
export default function ModelAccessEditor({ models, value, onChange, locked = '',
                                            allowNonZdr = false }) {
  const { t } = useTranslation('models')
  const [query, setQuery] = useState('')
  const [onlyTools, setOnlyTools] = useState(false)
  const [onlySelected, setOnlySelected] = useState(false)
  const enabled = !!value?.enabled
  const chosen = useMemo(() => new Set(value?.models || []), [value])
  const isOn = (m) => m.id === locked || chosen.has(m.id)

  const { list, hiddenZdr } = useMemo(() => {
    const q = query.trim().toLowerCase()
    const matching = models
      .filter((m) => !onlyTools || m.tools)
      .filter((m) => !onlySelected || m.id === locked || chosen.has(m.id))
      .filter((m) => !q || m.id.toLowerCase().includes(q) ||
                     (m.name || '').toLowerCase().includes(q))
    // il default resta visibile anche senza ZDR: è quello che vale davvero
    const shown = allowNonZdr ? matching
                              : matching.filter((m) => m.id === locked || !noZdr(m))
    // il modello predefinito dell'amministratore sta in cima, prima ancora
    // dell'ordine per azienda del selettore: è l'unica voce che l'utente
    // vedrà di sicuro, e chi configura la lista deve trovarla subito
    const rank = (m) => (m.id === locked ? -1 : authorRank(m))
    return {
      hiddenZdr: matching.length - shown.length,
      list: shown.sort((a, b) => rank(a) - rank(b)),
    }
  }, [models, query, onlyTools, onlySelected, allowNonZdr, chosen, locked])

  function toggle(id, on) {
    const next = new Set(chosen)
    if (on) next.add(id)
    else next.delete(id)
    // nell'ordine del catalogo: la lista salvata resta leggibile
    onChange({ enabled, models: models.filter((m) => next.has(m.id)).map((m) => m.id) })
  }

  // il default conta fra i selezionati anche se non è nella lista salvata
  const count = chosen.size + (locked && !chosen.has(locked) ? 1 : 0)

  return (
    <div className="flex flex-col gap-3">
      <select className="h-9 w-fit min-w-0 max-w-full rounded-md border border-input bg-card px-3 text-sm"
              value={enabled ? 'selected' : 'all'}
              onChange={(e) => onChange({ enabled: e.target.value === 'selected',
                                          models: value?.models || [] })}>
        <option value="all">{t('access.all')}</option>
        <option value="selected">{t('access.selected')}</option>
      </select>

      {enabled && (
        <div className="flex flex-col rounded-lg border border-border bg-card">
          <div className="flex items-center gap-2 border-b border-border px-2.5 py-2">
            <Search className="size-4 shrink-0 text-muted-foreground" />
            <input value={query} onChange={(e) => setQuery(e.target.value)}
                   placeholder={t('selector.search')}
                   className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground" />
          </div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-border px-2.5 py-1.5 text-xs text-muted-foreground">
            <label className="flex cursor-pointer items-center gap-1.5">
              <input type="checkbox" checked={onlyTools}
                     onChange={(e) => setOnlyTools(e.target.checked)} />
              <Wrench className="size-3" />
              {t('selector.onlyTools')}
            </label>
            <label className="flex cursor-pointer items-center gap-1.5">
              <input type="checkbox" checked={onlySelected}
                     onChange={(e) => setOnlySelected(e.target.checked)} />
              {t('access.onlySelected')}
            </label>
          </div>
          <ul className="max-h-[320px] overflow-y-auto py-1">
            {list.length === 0 && (
              <li className="px-3 py-4 text-center text-sm text-muted-foreground">
                {t('selector.noMatch')}
              </li>
            )}
            {list.map((m) => {
              const isLocked = m.id === locked
              return (
                <li key={m.id}>
                  <label className={cn(
                    'flex w-full cursor-pointer items-start gap-2.5 px-2.5 py-1.5 text-left hover:bg-accent',
                    isLocked && 'cursor-default')}>
                    <Checkbox className="mt-0.5" checked={isOn(m)} disabled={isLocked}
                              onCheckedChange={(v) => toggle(m.id, Boolean(v))} />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-1.5">
                        <span className="truncate text-sm font-medium">{m.name}</span>
                        {m.tools && <Wrench className="size-3 shrink-0 text-muted-foreground" />}
                        {noZdr(m) && (
                          <span title={t('selector.noZdr')}>
                            <ShieldOff className="size-3 shrink-0 text-amber-600" />
                          </span>
                        )}
                        {isLocked && (
                          <span className="inline-flex items-center gap-1 rounded-full border border-border px-1.5 text-[10px] text-muted-foreground"
                                title={t('access.lockedHelp')}>
                            <Lock className="size-2.5" />
                            {t('access.locked')}
                          </span>
                        )}
                      </span>
                      <span className="block truncate text-xs text-muted-foreground">
                        {m.id}{priceLabel(m, t) ? ` · ${priceLabel(m, t)}` : ''}
                      </span>
                    </span>
                  </label>
                </li>
              )
            })}
          </ul>
          {hiddenZdr > 0 && (
            <div className="flex items-center gap-1.5 border-t border-border px-2.5 py-1.5 text-xs text-muted-foreground">
              <ShieldOff className="size-3 shrink-0" />
              {t('selector.hiddenNoZdr', { count: hiddenZdr })}
            </div>
          )}
          <div className="flex items-center justify-between gap-2 border-t border-border px-2.5 py-1.5 text-xs text-muted-foreground">
            <span className={cn(count === 0 && 'text-destructive')}>
              {count === 0 ? t('access.empty') : t('access.count', { count })}
            </span>
            {chosen.size > 0 && (
              <Button type="button" variant="ghost" size="sm" className="h-6 text-xs"
                      onClick={() => onChange({ enabled, models: [] })}>
                {t('access.clear')}
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
