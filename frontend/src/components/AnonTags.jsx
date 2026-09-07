import React, { useEffect, useRef, useState } from 'react'
import { ChevronRight, Loader2, RotateCcw, ShieldCheck, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from '@/api.js'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { cn } from '@/lib/utils'
import { groupChecked, splitGroup, toggleGroup } from '@/lib/tagGroups.js'
import FloatingPanel from './FloatingPanel.jsx'

// Quali categorie di dati vengono coperte in QUESTA conversazione.
//
// Rispondeva a una domanda che la chat anonimizzata non permetteva di porre:
// «cosa sta anonimizzando?». Le etichette sono i TAG del modello, letti da
// /api/tags: nessuna lista mantenuta a mano, un modello aggiornato porta i suoi
// tag da solo (e un tag nuovo nasce ATTIVO — si salva l'esclusione, non
// l'inclusione).
//
// Il conteggio accanto al tag è il registro della conversazione: dice quante
// entità di quella categoria sono GIÀ state trovate qui. È la parte che
// rende la risposta concreta — non "cosa potrei coprire" ma "cosa ho coperto".
//
// La scelta vale dai turni SUCCESSIVI: i messaggi già inviati sono partiti
// come sono partiti e non si riscrivono. Spegnere una categoria che ha già
// prodotto segnaposto significa che da adesso quei valori usciranno in chiaro,
// e il pannello lo dice.
export default function AnonTags({ convId, tags, groups = null, value, onChange, disabled }) {
  const [open, setOpen] = useState(false)
  const [cyberOpen, setCyberOpen] = useState(false)
  const [counts, setCounts] = useState(null)
  const [loading, setLoading] = useState(false)
  const boxRef = useRef(null)
  const { t } = useTranslation('anon')

  // il registro si rilegge a ogni apertura: durante la conversazione cresce
  useEffect(() => {
    if (!open || !convId) return
    let alive = true
    setLoading(true)
    api.listChatEntities(convId)
      .then((r) => {
        if (!alive) return
        const by = {}
        for (const e of r.entities || []) {
          if (e.merged_into) continue     // assorbita da un'altra: non si conta due volte
          by[e.label] = (by[e.label] || 0) + 1
        }
        setCounts(by)
      })
      .catch(() => { if (alive) setCounts({}) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [open, convId])

  const excluded = new Set(value?.excluded_tags || [])
  const terms = value?.custom_terms || []
  const inherited = value?.inherited !== false
  // l'elenco è l'UNIONE dei tag rilevabili e di quelli esclusi: un default
  // dell'admin che nomina un tag non più prodotto dal modello resta comunque
  // visibile, invece di essere un'esclusione invisibile
  const all = [...new Set([...tags, ...excluded])].sort()
  const { rest, inGroup: cyber } = splitGroup(all, groups)
  const cyberFound = cyber.reduce((n, tag) => n + (counts?.[tag] || 0), 0)

  function toggle(tag) {
    const next = new Set(excluded)
    if (next.has(tag)) next.delete(tag)
    else next.add(tag)
    onChange([...next].sort())
  }

  const row = (tag) => {
    const off = excluded.has(tag)
    const n = counts?.[tag] || 0
    return (
      <label key={tag}
             className="touch-control flex cursor-pointer select-none items-center gap-2 rounded-md px-1.5 py-1 hover:bg-accent">
        <Checkbox checked={!off} disabled={disabled}
                  onCheckedChange={() => toggle(tag)} />
        <span className={cn('min-w-0 flex-1 truncate font-mono text-[11px]',
                            off && 'text-muted-foreground line-through')}>
          {tag}
        </span>
        {n > 0 && (
          <span className={cn('shrink-0 rounded px-1 text-[10px] tabular-nums',
                              off ? 'bg-amber-500/15 text-amber-700 dark:text-amber-400'
                                  : 'bg-accent text-muted-foreground')}
                title={t(off ? 'panel.foundOff' : 'panel.found',
                         { count: n })}>
            {n}
          </span>
        )}
      </label>
    )
  }

  return (
    <div className="relative" ref={boxRef}>
      {/* solo lo scudo, nello stesso vestito del bottone opzioni modello che
          gli sta accanto (Button outline), col verde della barra sotto anche
          nell'ombra; le categorie lasciate in chiaro restano dette dal
          tooltip e dal pannello */}
      <Button type="button" variant="outline" size="sm" disabled={disabled}
              title={excluded.size
                ? t('chip.someClear', { count: excluded.size })
                : t('chip.allCovered')}
              className="shadow-emerald-500/40" aria-label={t('panel.title')} aria-expanded={open}
              onClick={() => setOpen((o) => !o)}>
        <ShieldCheck className="size-3.5 text-emerald-700 dark:text-emerald-400" />
      </Button>

      {open && (
        <FloatingPanel anchorRef={boxRef} onClose={() => setOpen(false)} aria-label={t('panel.title')}
                       className="w-[380px] rounded-lg border border-border bg-popover shadow-lg">
          <div className="flex items-center justify-between border-b border-border px-3 py-2">
            <span className="text-xs font-semibold">{t('panel.title')}</span>
            <button className="touch-control flex size-7 shrink-0 items-center justify-center text-muted-foreground hover:text-foreground"
                    title={t('actions.close', { ns: 'common' })}
                    onClick={() => setOpen(false)}>
              <X className="size-3.5" />
            </button>
          </div>

          <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-1.5">
            <span className="text-[11px] text-muted-foreground">
              {t('panel.help')}
            </span>
            <div className="flex shrink-0 gap-1">
              <Button type="button" variant="ghost" size="sm" className="h-6 px-2 text-xs"
                      disabled={excluded.size === 0} onClick={() => onChange([])}>
                {t('categories.all')}
              </Button>
              <Button type="button" variant="ghost" size="sm" className="h-6 px-2 text-xs"
                      disabled={excluded.size === all.length}
                      onClick={() => onChange(all)}>
                {t('categories.none')}
              </Button>
            </div>
          </div>

          <div className="floating-panel-scroll max-h-[50dvh] overflow-y-auto p-2">
            {all.length === 0 ? (
              <div className="px-1 py-2 text-[11px] text-muted-foreground">
                {t('panel.noTags')}
              </div>
            ) : (
              <>
                <div className="grid grid-cols-2 gap-x-2 gap-y-0.5">
                  {rest.map(row)}
                </div>
                {cyber.length > 0 && (
                  /* il gruppo «Cybersecurity»: casella padre a tre stati
                     derivata dai figli, richiuso finché non lo si apre; il
                     numero dice quanti figli sono accesi, il badge quante
                     entità del gruppo il registro ha già trovato */
                  <div className="mt-1.5 border-t border-border/60 pt-1.5">
                    <div className="flex items-center gap-2 rounded-md px-1.5 py-1 hover:bg-accent">
                      <Checkbox checked={groupChecked(cyber, excluded)} disabled={disabled}
                                onCheckedChange={() => onChange(toggleGroup(cyber, excluded))} />
                      <button type="button" aria-expanded={cyberOpen}
                              className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                              title={t('categories.cyberHelp')}
                              onClick={() => setCyberOpen((o) => !o)}>
                        <span className="text-[11px] font-medium">{t('categories.cyber')}</span>
                        <span className="text-[10px] tabular-nums text-muted-foreground">
                          {cyber.filter((tag) => !excluded.has(tag)).length}/{cyber.length}
                        </span>
                        {cyberFound > 0 && (
                          <span className="shrink-0 rounded bg-accent px-1 text-[10px] tabular-nums text-muted-foreground"
                                title={t('panel.found', { count: cyberFound })}>
                            {cyberFound}
                          </span>
                        )}
                        <ChevronRight className={cn('ml-auto size-3.5 shrink-0 text-muted-foreground transition-transform',
                                                    cyberOpen && 'rotate-90')} />
                      </button>
                    </div>
                    {cyberOpen && (
                      <div className="grid grid-cols-2 gap-x-2 gap-y-0.5 pl-6">
                        {cyber.map(row)}
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
            {loading && (
              <div className="flex items-center gap-1.5 px-1.5 pt-1 text-[11px] text-muted-foreground">
                <Loader2 className="size-3 animate-spin" /> {t('panel.loading')}
              </div>
            )}
          </div>

          {terms.length > 0 && (
            /* i termini sempre coperti arrivano da DUE liste (globale
               dell'amministratore + personale): si distinguono col `scope`,
               perché si tolgono in due posti diversi — e attribuirli tutti
               all'admin manderebbe l'utente a cercare i propri dove non sono */
            <div className="flex flex-col gap-0.5 border-t border-border px-3 py-1.5 text-[11px] text-muted-foreground">
              {['global', 'personal'].map((scope) => {
                const list = terms.filter((t) => (t.scope || 'global') === scope)
                if (!list.length) return null
                return (
                  <div key={scope}>
                    {/* non «i miei»: in un progetto sono quelli del suo
                        proprietario, e un amministratore può aprire il
                        progetto di chiunque */}
                    {t(scope === 'global' ? 'panel.alwaysGlobal'
                                          : 'panel.alwaysPersonal')}
                    {list.map((t) => t.text).join(', ')}
                  </div>
                )
              })}
            </div>
          )}

          <div className="flex items-center justify-between gap-2 border-t border-border px-3 py-2">
            <span className="text-[11px] text-muted-foreground">
              {t('panel.fromNextSend')}
            </span>
            <Button variant="ghost" size="sm" className="gap-1.5 text-xs"
                    disabled={inherited} onClick={() => onChange(null)}>
              <RotateCcw className="size-3" /> {t('panel.reset')}
            </Button>
          </div>
        </FloatingPanel>
      )}
    </div>
  )
}
