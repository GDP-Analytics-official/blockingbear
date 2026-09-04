import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { Loader2, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api, jobEvents } from './api.js'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

/* Gestione dei job di elaborazione (upload e ri-anonimizzazioni di colonna).
   Vive nel layout autenticato, SOPRA le route: cambiare pagina non chiude
   gli EventSource e le card restano visibili nella sidebar. Le pagine
   comunicano col provider tramite:
   - addJob(job)      — registra un job appena creato (202) e ne segue lo stato
   - startUpload(nome)— card LOCALE per il trasferimento del file, prima che
                        il job esista: torna l'id da passare a dismissJob
   - docsTick         — contatore: un job è finito, la lista file del progetto
                        va ricaricata
   - docRefresh       — {docId, n}: il file aperto è cambiato (job di colonna
                        concluso) e il descrittore va ricaricato */
const JobsContext = createContext(null)

// Testo di stato di una card job. position = job serviti prima di questo;
// progress = {phase, phase_index, phase_total, done, total} dal worker.
// `t` è quella del namespace jobs: il worker manda CHIAVI di fase
// (engine/progress.py, PHASES), il nome leggibile lo mette qui il frontend.
function jobStatusText(j, t) {
  // card locale: il file sta ancora attraversando la rete, il job non esiste
  if (j.status === 'uploading') return t('status.uploading')
  if (j.canceling && (j.status === 'queued' || j.status === 'processing'))
    return t('status.canceling')
  if (j.status === 'queued')
    return j.position > 0 ? t('status.queuedAfter', { count: j.position })
                          : t('status.queuedNext')
  if (j.status === 'processing') {
    const p = j.progress
    if (!p) return t('status.processing')
    // una chiave sconosciuta (engine nuovo, catalogo non aggiornato) esce
    // com'è: si legge male, ma si legge — meglio di una card vuota
    const phase = t(`phase.${p.phase}`, { defaultValue: p.phase })
    // "Fase x di y" solo quando la sequenza è dichiarata (es. la conversione
    // d'ingresso .doc -> .docx precede la scelta della pipeline: niente x/y)
    let s = p.phase_index && p.phase_total
      ? t('status.phaseOf', { index: p.phase_index, total: p.phase_total, phase })
      : t('status.phaseAlone', { phase })
    if (p.done != null && p.total) s += ` (${p.done}/${p.total})`
    return s
  }
  if (j.status === 'done') return t('status.done')
  if (j.status === 'canceled') return t('status.canceled')
  return j.error || t('status.failed')
}

// Percentuale complessiva stimata: fasi completate + frazione della corrente.
// null = granularità ignota (barra indeterminata).
function jobPercent(j) {
  const p = j.progress
  if (!p || !p.phase_index || !p.phase_total) return null
  const frac = p.total ? Math.min(1, (p.done ?? 0) / p.total) : 0
  return Math.round(100 * ((p.phase_index - 1) + frac) / p.phase_total)
}

export function JobsProvider({ children }) {
  const [jobs, setJobs] = useState([])
  const [error, setError] = useState('')
  const [docsTick, setDocsTick] = useState(0)
  const [docRefresh, setDocRefresh] = useState({ docId: null, n: 0 })
  const sourcesRef = useRef({})              // job_id -> EventSource
  const uploadSeq = useRef(0)                // id delle card locali di upload
  // dentro i callback SSE location sarebbe stale: serve il ref
  const pathRef = useRef('')
  pathRef.current = useLocation().pathname

  const dismissJob = useCallback((id) => {
    setJobs((js) => js.filter((x) => x.id !== id))
    sourcesRef.current[id]?.close()
    delete sourcesRef.current[id]
  }, [])

  const watchJob = useCallback((job) => {
    sourcesRef.current[job.id]?.close()
    sourcesRef.current[job.id] = jobEvents(job.id, (j) => {
      if (j.status === 'done') {
        setDocsTick((n) => n + 1)
        const path = pathRef.current
        // rielaborazione OCR di un allegato in ANTEPRIMA di chat: il turno è
        // stato ri-redatto dal worker e il modal (che copre la sidebar) deve
        // rileggerlo — la pagina della chat filtra da sola sul suo staged
        if (j.kind === 'chat_reprocess') {
          dismissJob(j.id)
          setDocRefresh((r) => ({ docId: j.doc_id, n: r.n + 1 }))
          return
        }
        // job di COLONNA, RIELABORAZIONE OCR o RI-REDAZIONE: il file esiste
        // già — se è aperto va ricaricato il descrittore (mappa e preview
        // sono cambiate)
        if (j.kind === 'project_column' || j.kind === 'project_reprocess'
            || j.kind === 'project_realign') {
          dismissJob(j.id)
          if (path === `/projects/${j.project_id}/files/${j.doc_id}`) {
            setDocRefresh((r) => ({ docId: j.doc_id, n: r.n + 1 }))
          }
          return
        }
        // upload: chi sta guardando il progetto vede già la lista
        // aggiornarsi (docsTick); da altrove la card resta con "Apri"
        if (path === `/projects/${j.project_id}`) {
          dismissJob(j.id)
          return
        }
      }
      // merge (non replace): conserva il flag locale `canceling` finché
      // il worker non conferma lo stato finale
      setJobs((js) => js.map((x) => (x.id === j.id ? { ...x, ...j } : x)))
    })
  }, [dismissJob])

  const addJob = useCallback((job) => {
    setJobs((js) => [...js, job])
    watchJob(job)
  }, [watchJob])

  // Il trasferimento del file precede il job (che nasce solo col 202) e su un
  // documento pesante dura secondi: la card locale occupa da subito il posto
  // dove poi comparirà quella vera, così l'attesa non è mai muta. `local`
  // la distingue: niente SSE e niente annullamento.
  const startUpload = useCallback((filename) => {
    const id = `upload-${uploadSeq.current++}`
    setJobs((js) => [...js, { id, filename, status: 'uploading', local: true }])
    return id
  }, [])

  const cancelJob = useCallback(async (id) => {
    setJobs((js) => js.map((x) => (x.id === id ? { ...x, canceling: true } : x)))
    try {
      const j = await api.cancelJob(id)
      // in coda: già `canceled` nella risposta; in lavorazione: resta
      // `processing` e la conferma arriva via SSE al prossimo checkpoint
      setJobs((js) => js.map((x) => (x.id === j.id ? { ...x, ...j } : x)))
    } catch (e) {
      setError(e.message)
      setJobs((js) => js.map((x) => (x.id === id ? { ...x, canceling: false } : x)))
    }
  }, [])

  useEffect(() => {
    // job ancora attivi da un reload della pagina: riaggancia le card
    api.listJobs().then((js) => {
      const active = js.filter((j) => j.status === 'queued' || j.status === 'processing')
      setJobs(active)
      active.forEach(watchJob)
    }).catch(() => { /* il resto dell'app funziona comunque */ })
    const sources = sourcesRef.current
    return () => Object.values(sources).forEach((es) => es.close())
  }, [watchJob])

  return (
    <JobsContext.Provider value={{ jobs, error, addJob, startUpload, cancelJob,
                                   dismissJob, docsTick, docRefresh }}>
      {children}
    </JobsContext.Provider>
  )
}

export function useJobs() {
  return useContext(JobsContext)
}

// Card dei job (in coda / in corso / falliti), mostrate nella sidebar della
// dashboard così restano visibili su qualunque pagina.
export function JobCards() {
  const { jobs, error, cancelJob, dismissJob } = useJobs()
  const navigate = useNavigate()
  const { t } = useTranslation('jobs')
  if (jobs.length === 0 && !error) return null
  return (
    <div className="flex flex-col gap-2">
      <div className="px-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {t('heading')}
      </div>
      {error && <div className="px-1 text-xs text-destructive">{error}</div>}
      <ul className="flex flex-col gap-1.5">
        {jobs.map((j) => {
          const active = j.status === 'queued' || j.status === 'processing'
                         || j.status === 'uploading'
          const pct = jobPercent(j)
          return (
            <li
              key={j.id}
              className={cn(
                'flex items-start gap-2 rounded-lg border border-border bg-card p-2.5 text-[13px] shadow-sm',
                j.status === 'done' && 'border-primary/50',
                j.status === 'failed' && 'border-destructive/50',
                j.status === 'canceled' && 'opacity-70'
              )}
            >
              {active && <Loader2 className="mt-0.5 size-3.5 shrink-0 animate-spin text-primary" />}
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium" title={j.filename}>{j.filename}</div>
                <div className={cn('text-xs', j.status === 'failed' ? 'text-destructive' : 'text-muted-foreground')}>
                  {jobStatusText(j, t)}
                </div>
                {(j.status === 'processing' || j.status === 'uploading') && (
                  <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-primary/15">
                    <div
                      className={cn('h-full rounded-full bg-primary transition-[width] duration-300',
                                    pct == null && 'progress-indeterminate')}
                      style={pct == null ? undefined : { width: pct + '%' }}
                    />
                  </div>
                )}
              </div>
              {/* «Apri» solo dove c'è una pagina da aprire: un allegato di
                  chat vive nel modal dell'anteprima, non ha una sua rotta */}
              {j.status === 'done' && j.project_id && (
                <Button variant="outline" size="sm" className="h-6 px-2 text-[11px]"
                        onClick={() => {
                          dismissJob(j.id)
                          navigate(`/projects/${j.project_id}/files/${j.doc_id}`)
                        }}>
                  {t('open')}
                </Button>
              )}
              {/* una card locale non ha un job da annullare: il trasferimento
                  si ferma solo cambiando pagina */}
              {active && !j.local && (
                <Button variant="ghost" size="icon-sm" className="size-5" title={t('cancel')}
                        aria-label={t('cancel')} disabled={!!j.canceling}
                        onClick={() => cancelJob(j.id)}>
                  <X />
                </Button>
              )}
              {(j.status === 'failed' || j.status === 'canceled') && (
                <Button variant="ghost" size="icon-sm" className="size-5"
                        title={t('actions.close', { ns: 'common' })}
                        aria-label={t('actions.close', { ns: 'common' })}
                        onClick={() => dismissJob(j.id)}>
                  <X />
                </Button>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
