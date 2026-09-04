import React, { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import {
  AlertTriangle, ArrowLeftRight, ChevronDown, ChevronLeft, ChevronRight,
  Loader2, Lock, Merge, RotateCw, Send, Split,
} from 'lucide-react'
import { Trans, useTranslation } from 'react-i18next'
import DocumentViewer, { docWarnings } from '@/DocumentViewer.jsx'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

// Revisione del registro. Il resolver unisce solo ciò che è
// deterministico ("Acme Analytics" = "Acme Analytics Srl", "Rossi Mario" =
// "Mario Rossi"); un cognome da solo può essere la stessa persona o
// un'altra, e questa è l'unica sede in cui la domanda si può porre.
//
// Il registro su cui agisce arriva da fuori (`registry`: chat o progetto),
// perché lo stesso pannello serve nell'ultimo step del modal di revisione e
// nel dialog che ferma il turno appena il registro nasce (`applies="now"`:
// la scelta entra nel system prompt di QUEL messaggio).
// In nessun caso si riscrive niente all'indietro: i file e i messaggi già
// protetti conservano il loro segnaposto, ed è il system prompt a dire al
// modello che i due tag sono la stessa entità.
export function EntityReview({ registry, suggestions, onChanged, applies = 'next',
                               emptyLabel = null, className = '' }) {
  const [open, setOpen] = useState(false)
  const [entities, setEntities] = useState(null)
  const [busy, setBusy] = useState(false)
  const { t } = useTranslation('anon')
  const list = suggestions || []
  const mergedToast = t(applies === 'now' ? 'review.mergedNow' : 'review.mergedNext')

  function load() {
    registry.list()
      .then((r) => setEntities(r.entities))
      .catch((e) => toast.error(e.message))
  }

  function act(promise, done) {
    setBusy(true)
    promise
      .then((r) => { setEntities(r.entities); onChanged?.(r); toast.success(done) })
      .catch((e) => toast.error(e.message))
      .finally(() => setBusy(false))
  }

  // Le scorciatoie «tutte»: non c'è un endpoint di massa, si va una coppia
  // alla volta perché ogni fusione RICALCOLA le proposte (unire «Rossi» a
  // «Mario Rossi» può far sparire un'altra coppia o incatenarne una nuova).
  // Si riparte sempre dalle proposte fresche della risposta; una coppia già
  // tentata non si ritenta, così un errore non manda il giro in loop.
  async function runAll(kind) {
    setBusy(true)
    const key = (s) => `${s.source}>${s.target}`
    const tried = new Set()
    let pending = list
    let payload = null
    let done = 0
    let failed = 0
    while (pending.length) {
      const s = pending.find((p) => !tried.has(key(p)))
      if (!s) break
      tried.add(key(s))
      try {
        payload = kind === 'merge' ? await registry.merge(s.source, s.target)
                                   : await registry.keepSeparate(s.source)
        done += 1
        pending = payload.suggestions || []
      } catch {
        failed += 1
        pending = pending.filter((p) => key(p) !== key(s))
      }
    }
    if (payload) {
      setEntities(payload.entities)
      onChanged?.(payload)
    }
    if (done) {
      toast.success(kind === 'merge'
        ? t(applies === 'now' ? 'review.mergedAllNow' : 'review.mergedAllNext',
            { count: done })
        : t('review.keptAllSeparate', { count: done }))
    }
    if (failed) toast.error(t('review.bulkFailed', { count: failed }))
    setBusy(false)
  }

  if (!list.length && !open && !emptyLabel) {
    return (
      <div className={cn('border-b border-border bg-card px-4 py-1.5', className)}>
        <button className="text-xs text-muted-foreground underline-offset-2 hover:underline"
                onClick={() => { setOpen(true); load() }}>
          {t('review.showRegistry')}
        </button>
      </div>
    )
  }
  return (
    <div className={cn('overflow-hidden bg-card', className)}>
      {!list.length && emptyLabel && (
        <p className="px-3 py-2.5 text-sm text-muted-foreground">{emptyLabel}</p>
      )}
      {!!list.length && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border bg-secondary/60 px-3 py-2">
          <span className="text-xs font-medium text-secondary-foreground">
            {t('review.pending', { count: list.length })}
          </span>
          <div className="ml-auto flex items-center gap-1">
            <Button size="sm" variant="ghost" disabled={busy}
                    className="gap-1.5 text-primary hover:bg-primary/10 hover:text-primary"
                    onClick={() => runAll('merge')}>
              {busy ? <Loader2 className="animate-spin" /> : <Merge />}
              {t('review.mergeAll')}
            </Button>
            <Button size="sm" variant="ghost" disabled={busy}
                    className="gap-1.5 text-muted-foreground"
                    onClick={() => runAll('keep')}>
              <Split /> {t('review.keepAllSeparate')}
            </Button>
          </div>
        </div>
      )}
      <ul className="divide-y divide-border">
        {list.map((s) => (
          <li key={s.source}
              className="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2.5">
            <div className="flex min-w-0 flex-1 basis-80 items-center gap-2">
              <EntityChip placeholder={s.source_placeholder} value={s.source_value} />
              <ArrowLeftRight className="size-3.5 shrink-0 text-muted-foreground" />
              <EntityChip placeholder={s.target_placeholder} value={s.target_value} />
            </div>
            <div className="ml-auto flex shrink-0 items-center gap-1.5">
              <Button size="sm" variant="outline" disabled={busy}
                      className="gap-1.5 border-primary/40 text-primary hover:bg-primary/10 hover:text-primary"
                      onClick={() => act(registry.merge(s.source, s.target),
                                         mergedToast)}>
                <Merge /> {t('review.merge')}
              </Button>
              <Button size="sm" variant="ghost" disabled={busy}
                      className="gap-1.5 text-muted-foreground"
                      onClick={() => act(registry.keepSeparate(s.source),
                                         t('review.keptSeparate'))}>
                <Split /> {t('review.keepSeparate')}
              </Button>
            </div>
          </li>
        ))}
      </ul>
      <div className="border-t border-border px-3 py-2">
        <button className="inline-flex items-center gap-1 text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                onClick={() => { const next = !open; setOpen(next); if (next) load() }}>
          <ChevronDown className={cn('size-3.5 transition-transform',
                                     open && 'rotate-180')} />
          {t(open ? 'review.hideRegistry' : 'review.showRegistry')}
        </button>
        {open && entities && (
          <div className="mt-2 max-h-56 overflow-y-auto rounded-md border border-border">
            <table className="w-full border-collapse text-xs">
              <thead className="sticky top-0 z-10 bg-secondary/70 text-[10.5px] uppercase tracking-wide text-secondary-foreground">
                <tr>
                  <th className="px-2 py-1.5 text-left font-medium">
                    {t('review.colPlaceholder')}
                  </th>
                  <th className="px-2 py-1.5 text-left font-medium">
                    {t('review.colValue')}
                  </th>
                  <th className="px-2 py-1.5 text-right font-medium">
                    {t('review.colState')}
                  </th>
                </tr>
              </thead>
              <tbody>
                {entities.map((e) => {
                  const state = e.merged_into ? t('review.entity.merged')
                    : e.surfaces.length > 1
                      ? t('review.entity.forms', { count: e.surfaces.length })
                    : !e.searchable ? t('review.entity.tooShort')
                    : ''
                  return (
                    <tr key={e.id}
                        className={cn('border-t border-border/60',
                                      e.merged_into && 'text-muted-foreground')}>
                      <td className="whitespace-nowrap px-2 py-1 font-mono text-[11px] text-primary">
                        {e.placeholder}
                      </td>
                      <td className="px-2 py-1">{e.value}</td>
                      <td className="px-2 py-1 text-right">
                        {state && (
                          <span className="inline-block rounded-full border border-border bg-background px-1.5 py-0.5 text-[10.5px] text-muted-foreground">
                            {state}
                          </span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

// Le due facce di un'entità in una pastiglia sola: il segnaposto (il nome con
// cui la vede il modello) e il valore reale, che è quello su cui l'utente
// decide se le due sono la stessa cosa.
function EntityChip({ placeholder, value }) {
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1">
      {placeholder && (
        <span className="shrink-0 font-mono text-[10.5px] text-primary">
          {placeholder}
        </span>
      )}
      <span className="truncate text-[13px]" title={value}>{value}</span>
    </span>
  )
}

// Le avvertenze del pezzo (valori lasciati in chiaro, sigilli, preview
// troncata…) nel footer: solo un'icona ambra, il testo per intero si apre al
// click in un pannello verso l'alto — le righe gialle a tutta larghezza in
// testa al viewer mangiavano spazio alla preview.
function WarnBadge({ warnings, title }) {
  const [open, setOpen] = useState(false)
  const boxRef = useRef(null)
  useEffect(() => {
    if (!open) return undefined
    const onDoc = (e) => {
      if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])
  if (!warnings.length) return null
  return (
    <div className="relative shrink-0" ref={boxRef}>
      <button type="button" title={title} onClick={() => setOpen((o) => !o)}
              className="inline-flex size-8 cursor-pointer items-center justify-center rounded-md border border-amber-500/40 bg-amber-500/10 text-amber-700 hover:bg-amber-500/20 dark:text-amber-400">
        <AlertTriangle className="size-4" />
      </button>
      {open && (
        <div className="absolute bottom-full left-0 z-30 mb-1 w-[420px] max-w-[85vw] overflow-hidden rounded-lg border border-border bg-popover shadow-lg">
          {warnings.map((w, i) => (
            <div key={i} className="flex items-start gap-2 border-b border-border px-3 py-2 last:border-b-0">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-amber-700 dark:text-amber-400" />
              <span className="text-[11px] leading-snug text-muted-foreground">{w}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// L'ULTIMO step, quando il registro propone fusioni, è il registro stesso: sta
// in fondo perché anonimizzare o deanonimizzare a mano nei pezzi precedenti
// cambia le coppie proposte (ri-includere un valore lasciato in chiaro ne fa
// ricomparire una, escluderlo la toglie). Una volta comparso lo step resta:
// farlo sparire mentre l'utente ci sta sopra lo sbatterebbe indietro.
// `filename` è l'etichetta della pastiglia in alto: si traduce al montaggio
const MERGE_STEP = { id: '__merge__', kind: 'merge' }

// Revisione dell'anonimizzazione PRIMA che qualcosa esca dai confini in cui è
// stato protetto — il turno di chat prima dell'invio, il file di progetto
// prima della conferma che lo rende visibile alle chat. È UN SOLO componente
// perché la domanda è una sola: ogni pezzo si guarda nello STESSO
// visualizzatore (originale a sinistra, anonimizzato a destra, con
// anonimizza-in-più e deanonimizza), si scorre con «Avanti», e sull'ultimo
// pezzo il bottone conferma l'operazione. Quello che cambia tra i due usi sono
// solo gli item, i servizi del viewer, il registro su cui agire e il verbo
// finale.
//
// I suggerimenti di fusione NON arrivano da fuori: si chiedono al registro
// all'apertura e dopo ogni modifica, perché tra la fine dell'elaborazione e
// il momento in cui l'utente apre la revisione il registro può essere
// cambiato (altri dieci file caricati nel frattempo).
export default function ReviewModal({
  items, services, registry, onChange, onCancel, onConfirm,
  confirmLabel, confirmIcon: ConfirmIcon = Send,
  onRealign = null, onJobStart = null,
}) {
  const { t } = useTranslation('anon')
  const { t: tv } = useTranslation('viewer')
  const confirmText = confirmLabel || t('review.send')
  const [index, setIndex] = useState(0)
  // tab del viewer (Confronto/Mappa), CONTROLLATA da qui: sta nel footer del
  // modal per non spendere una riga in testa alla preview
  const [tab, setTab] = useState('preview')
  useEffect(() => { setTab('preview') }, [index])
  const [suggestions, setSuggestions] = useState(null)   // null = non ancora chiesti
  const [mergeStep, setMergeStep] = useState(false)
  const [working, setWorking] = useState(false)

  const refreshSuggestions = useCallback(() => (
    registry.list()
      .then((r) => setSuggestions(r.suggestions || []))
      .catch((e) => { setSuggestions([]); toast.error(e.message) })
  ), [registry])

  useEffect(() => { refreshSuggestions() }, [refreshSuggestions])
  useEffect(() => { if (suggestions?.length) setMergeStep(true) }, [suggestions])

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onCancel() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])

  // ogni modifica ai pezzi ri-redige e cambia il registro: le coppie proposte
  // vanno ricalcolate (lo step compare, o cambia contenuto)
  function handleChange(payload) {
    onChange?.(payload)
    refreshSuggestions()
  }

  function run(fn) {
    if (!fn) return
    setWorking(true)
    Promise.resolve(fn()).finally(() => setWorking(false))
  }

  const steps = mergeStep
    ? [...(items || []), { ...MERGE_STEP, filename: t('review.registry') }]
    : (items || [])
  const item = steps[Math.min(index, steps.length - 1)]
  const last = index >= steps.length - 1
  const loading = suggestions === null

  if (!item) return null
  const merging = item.kind === 'merge'
  // l'item "prompt" arriva dal server con un'etichetta fissa in `filename`:
  // il nome mostrato si traduce QUI, in ogni punto in cui compare
  const displayName = (it) =>
    (it.kind === 'prompt' ? t('review.yourMessage') : it.filename)
  return (
    <div className="fixed inset-0 z-40 grid place-items-center bg-black/50 p-3">
      <div className="flex h-[94vh] w-[min(1500px,97vw)] flex-col overflow-hidden rounded-xl border border-border bg-background shadow-2xl">
        <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border px-4 py-3">
          <span className="inline-flex items-center gap-2 font-medium text-emerald-700 dark:text-emerald-400">
            <Lock className="size-4" /> {t('review.title')}
          </span>
          <span className="text-sm text-muted-foreground">
            <Trans i18nKey="review.position" ns="anon"
                   values={{ index: index + 1, total: steps.length,
                             confirm: confirmText }}
                   components={{ b: <b className="text-foreground" /> }} />
          </span>
          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            {steps.map((it, i) => (
              <button key={it.id} type="button" onClick={() => setIndex(i)}
                      title={it.kind === 'merge' ? t('review.registryTitle')
                        : t('review.chipFile', { file: displayName(it),
                                                 count: it.n_entities ?? 0 })}
                      className={cn(
                        'max-w-[220px] truncate rounded-full border px-2.5 py-0.5 text-xs',
                        i === index
                          ? 'border-emerald-500/60 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
                          : 'border-border text-muted-foreground hover:bg-accent')}>
                {i + 1}. {displayName(it)}
              </button>
            ))}
          </div>
        </header>
        {merging ? (
          <div className="review-body min-h-0 flex-1 overflow-y-auto p-6">
            <div className="mx-auto w-full max-w-3xl overflow-hidden rounded-xl border border-border bg-card shadow-sm">
              <div className="flex items-start gap-3 border-b border-border px-4 py-3">
                <span className="mt-0.5 inline-flex size-8 shrink-0 items-center justify-center rounded-lg bg-secondary text-primary">
                  <Merge className="size-4" />
                </span>
                <div className="min-w-0">
                  <h3 className="font-medium">{t('review.registryTitle')}</h3>
                  <p className="mt-1 text-sm leading-snug text-muted-foreground">
                    {t('review.stepHelp')}
                  </p>
                </div>
              </div>
              <EntityReview registry={registry} suggestions={suggestions}
                            applies="now"
                            emptyLabel={t('review.noMerges')}
                            onChanged={(r) => {
                              setSuggestions(r.suggestions || [])
                              // la chat risponde col turno in anteprima
                              // aggiornato: la fusione non ri-redige niente,
                              // ma il descrittore risale comunque intero
                              if (r.staged) onChange?.(r.staged)
                            }} />
            </div>
          </div>
        ) : (
          /* .main: le primitive CSS del viewer sono scopate lì;
             .review-body: canvas neutro del modal (vedi styles.css) */
          <div className="main review-body min-h-0 flex-1 overflow-hidden"
               style={{ padding: 12 }}>
            <DocumentViewer key={item.id} doc={item} services={services} tab={tab}
                            onChange={handleChange} onJobStart={onJobStart} />
          </div>
        )}
        <footer className="flex items-center gap-2 border-t border-border px-4 py-3">
          {/* la .tabs è la primitiva CSS del viewer, globale: qui pilota la
              tab del DocumentViewer sopra */}
          {!merging && (
            <div className="tabs shrink-0">
              <button type="button" className={tab === 'preview' ? 'on' : ''}
                      onClick={() => setTab('preview')}>
                {tv('tab.compare')}
              </button>
              <button type="button" className={tab === 'mapping' ? 'on' : ''}
                      onClick={() => setTab('mapping')}>
                {tv('tab.mapping',
                    { count: Object.keys(item.mapping || {}).length })}
              </button>
            </div>
          )}
          {!merging && (
            <WarnBadge warnings={docWarnings(item, tv)}
                       title={t('review.warnings')} />
          )}
          {onRealign && (
            <Button variant="ghost" className="gap-1.5" disabled={working}
                    title={t('review.realignHelp')}
                    onClick={() => run(onRealign)}>
              <RotateCw className="size-3.5" /> {t('review.realign')}
            </Button>
          )}
          <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
            {merging ? t('review.footerMerging') : ''}
          </span>
          <Button variant="ghost" disabled={working} onClick={onCancel}>
            {t('actions.cancel', { ns: 'common' })}
          </Button>
          <Button variant="outline" className="gap-1" disabled={index === 0 || working}
                  onClick={() => setIndex((i) => Math.max(0, i - 1))}>
            <ChevronLeft className="size-4" /> {t('review.back')}
          </Button>
          {last ? (
            <Button className="gap-2" disabled={loading || working}
                    onClick={() => run(onConfirm)}>
              {loading || working
                ? <Loader2 className="size-4 animate-spin" />
                : <ConfirmIcon className="size-4" />}
              {confirmText}
            </Button>
          ) : (
            <Button className="gap-1" disabled={working}
                    onClick={() => setIndex((i) => i + 1)}>
              {t('review.next')} <ChevronRight className="size-4" />
            </Button>
          )}
        </footer>
      </div>
    </div>
  )
}
