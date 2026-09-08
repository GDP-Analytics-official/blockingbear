import React, { useState } from 'react'
import { ChevronRight, CircleHelp, Plus, X } from 'lucide-react'
import { Trans, useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import { groupChecked, splitGroup, toggleGroup } from '@/lib/tagGroups.js'

// Editor delle opzioni di anonimizzazione della pagina Impostazioni (livello
// GLOBALE dell'admin: categorie escluse + termini che valgono per tutti).
// value = {excluded_tags: [...], custom_terms: [{text, tag}]}; tags = lista
// dei tag rilevabili (da /api/tags, campo "all"); defaults = lista di
// esclusione di partenza (lib/anonDefaults.js) per il bottone «Default»,
// null per non mostrarlo; groups = campo "groups"
// della stessa risposta ({cyber: [...]}: quali tag stanno sotto la voce
// «Cybersecurity»). Le etichette restano i tag del modello, senza traduzioni
// mantenute a mano.
//
// La sola tabella dei termini è esportata a parte (TermsEditor): la usa anche
// la sezione «I miei termini», che ha la stessa forma ma scrive sulla lista
// personale. Un editor solo per entrambi i livelli — se le due tabelle
// divergessero, le regole di validità andrebbero tenute allineate a mano.

// ripulisce le righe vuote (aggiunte e mai compilate) prima dell'invio
export function cleanTerms(terms) {
  return (terms || []).filter((t) => t.text.trim())
}

export function cleanOptions(value) {
  return { ...value, custom_terms: cleanTerms(value.custom_terms) }
}

// true se una riga compilata non è inviabile (stesse regole del backend:
// almeno 2 caratteri alfanumerici, non 2 sole cifre, tag non vuoto)
export function termsInvalid(terms) {
  return (terms || []).some((t) => {
    if (!t.text.trim()) return false            // riga vuota: verrà scartata
    const alnum = t.text.replace(/[^\p{L}\p{N}]+/gu, '')
    return alnum.length < 2 || (alnum.length === 2 && /^\d+$/.test(alnum)) ||
           !(t.tag || '').trim()
  })
}

export function optionsInvalid(value) {
  return termsInvalid(value.custom_terms)
}

// Punto interrogativo con tooltip al passaggio del mouse (e al focus da
// tastiera): solo CSS, niente libreria per una riga di aiuto.
function HelpTip({ text, side = 'right' }) {
  return (
    <span className="group relative inline-flex">
      <button type="button" tabIndex={0} aria-label={text}
              className="inline-flex text-muted-foreground/70 hover:text-foreground focus:outline-none">
        <CircleHelp className="size-3.5" />
      </button>
      <span role="tooltip"
            className={cn('pointer-events-none absolute top-full z-20 mt-1.5 hidden w-60 max-w-[80vw] rounded-md border border-border bg-popover p-2.5 text-xs font-normal normal-case text-popover-foreground shadow-md group-hover:block group-focus-within:block',
                          side === 'left' ? 'right-0' : 'left-0')}>
        {text}
      </span>
    </span>
  )
}

// La tabella «testo -> TAG» + il bottone per aggiungere una riga. `datalistId`
// va distinto quando in pagina ci sono due editor: due <datalist> con lo
// stesso id e il browser ne ignora uno.
export function TermsEditor({ terms, tags, onChange, datalistId = 'anon-tag-list' }) {
  const { t } = useTranslation('anon')
  function setTerm(i, patch) {
    onChange(terms.map((t, j) => (j === i ? { ...t, ...patch } : t)))
  }

  return (
    <>
      <div className="flex flex-col gap-2">
        {/* intestazioni allineate alle due colonne, con la spiegazione al
            passaggio del mouse: da soli «testo» e «TAG» non dicono cosa
            succede al documento */}
        <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
          <div className="flex flex-1 items-center gap-1">
            {t('terms.textLabel')}<HelpTip text={t('terms.textHelp')} />
          </div>
          <div className="flex w-24 sm:w-40 shrink-0 items-center gap-1">
            {t('terms.tagLabel')}<HelpTip text={t('terms.tagHelp')} side="left" />
          </div>
          <div className="w-7 shrink-0" />
        </div>
        {/* `term`, non `t`: `t` è la funzione di traduzione di i18next */}
        {terms.map((term, i) => (
          <div key={i} className="term-row flex items-center gap-2">
            <Input type="text" aria-label={t('terms.textLabel')} placeholder={t('terms.textPlaceholder')} value={term.text}
                   onChange={(e) => setTerm(i, { text: e.target.value })} />
            <Input type="text" aria-label={t('terms.tagLabel')} placeholder={t('terms.tagPlaceholder')}
                   list={datalistId} value={term.tag}
                   className="w-24 sm:w-40 shrink-0 font-mono text-xs uppercase"
                   onChange={(e) => setTerm(i, {
                     tag: e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, ''),
                   })} />
            <Button type="button" variant="ghost" size="icon-sm"
                    title={t('terms.remove')} aria-label={t('terms.removeAria')}
                    className="shrink-0 text-muted-foreground hover:text-destructive"
                    onClick={() => onChange(terms.filter((_t, j) => j !== i))}>
              <X />
            </Button>
          </div>
        ))}
      </div>
      <datalist id={datalistId}>
        {(tags || []).map((tag) => <option key={tag} value={tag} />)}
      </datalist>
      <Button type="button" variant="outline" size="sm" className="mt-2"
              onClick={() => onChange([...terms, { text: '', tag: 'CUSTOM' }])}>
        <Plus /> {t('terms.add')}
      </Button>
    </>
  )
}

// La griglia delle categorie con «Default» / «Tutte» / «Nessuna». `excluded` è la lista
// dei tag SPENTI (si salva l'esclusione, non l'inclusione: un tag nuovo del
// modello nasce attivo). La usano la pagina Impostazioni e il wizard.
export function CategoriesPicker({ tags, groups = null, excluded, onChange, defaults = null }) {
  const { t } = useTranslation('anon')
  const [cyberOpen, setCyberOpen] = useState(false)
  const off = new Set(excluded)
  // `defaults`: lista di esclusione di partenza; il bottone si accende appena
  // la scelta se ne discosta, in più o in meno
  const isDefault = defaults !== null
    && JSON.stringify([...off].sort()) === JSON.stringify([...defaults].sort())
  // il gruppo «Cybersecurity» sta in fondo, richiuso, sotto una casella padre
  // che non è un tag: si deriva dai figli (lib/tagGroups.js)
  const { rest, inGroup: cyber } = splitGroup(tags, groups)
  const cyberOn = cyber.filter((tag) => !off.has(tag)).length

  function toggleTag(tag) {
    const next = new Set(off)
    if (next.has(tag)) next.delete(tag)
    else next.add(tag)
    onChange([...next].sort())
  }

  const row = (tag) => (
    <label key={tag}
           className="touch-control flex cursor-pointer select-none items-center gap-2 rounded-md px-1.5 py-1 hover:bg-accent">
      <Checkbox checked={!off.has(tag)} onCheckedChange={() => toggleTag(tag)} />
      <span className="min-w-0 break-all font-mono text-xs">{tag}</span>
    </label>
  )

  return (
    <section>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-semibold">{t('categories.title')}</h4>
        <div className="flex gap-1">
          {defaults !== null && (
            <Button type="button" variant="ghost" size="sm" className="h-6 px-2 text-xs"
                    disabled={isDefault} onClick={() => onChange([...defaults].sort())}>
              {t('categories.default')}
            </Button>
          )}
          <Button type="button" variant="ghost" size="sm" className="h-6 px-2 text-xs"
                  disabled={off.size === 0} onClick={() => onChange([])}>
            {t('categories.all')}
          </Button>
          <Button type="button" variant="ghost" size="sm" className="h-6 px-2 text-xs"
                  disabled={off.size === tags.length}
                  onClick={() => onChange([...tags].sort())}>
            {t('categories.none')}
          </Button>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3">
        {rest.map(row)}
      </div>
      {cyber.length > 0 && (
        <div className="mt-2 border-t border-border/60 pt-2">
          <div className="flex items-center gap-2 rounded-md px-1.5 py-1 hover:bg-accent">
            <Checkbox checked={groupChecked(cyber, off)}
                      onCheckedChange={() => onChange(toggleGroup(cyber, off))} />
            <button type="button" aria-expanded={cyberOpen}
                    className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                    title={t('categories.cyberHelp')}
                    onClick={() => setCyberOpen((o) => !o)}>
              <span className="text-xs font-medium">{t('categories.cyber')}</span>
              <span className="text-[11px] tabular-nums text-muted-foreground">
                {cyberOn}/{cyber.length}
              </span>
              <ChevronRight className={cn('ml-auto size-3.5 shrink-0 text-muted-foreground transition-transform',
                                          cyberOpen && 'rotate-90')} />
            </button>
          </div>
          {cyberOpen && (
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 pl-6 sm:grid-cols-3">
              {cyber.map(row)}
            </div>
          )}
        </div>
      )}
    </section>
  )
}

export default function AnonOptions({ tags, groups = null, value, onChange, defaults = null }) {
  const { t } = useTranslation('anon')

  return (
    <div className="flex flex-col gap-6">
      <CategoriesPicker tags={tags} groups={groups} excluded={value.excluded_tags} defaults={defaults}
                        onChange={(next) => onChange({ ...value, excluded_tags: next })} />

      <section>
        <h4 className="mb-1 text-sm font-semibold">{t('terms.globalTitle')}</h4>
        <p className="mb-2 text-xs text-muted-foreground">
          {t('terms.globalHelp')}{' '}
          <Trans i18nKey="terms.globalHelpScope" ns="anon" components={{ b: <b /> }} />
        </p>
        <TermsEditor terms={value.custom_terms} tags={tags} datalistId="anon-tag-list"
                     onChange={(next) => onChange({ ...value, custom_terms: next })} />
      </section>
    </div>
  )
}
