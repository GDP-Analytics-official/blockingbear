import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'
import {
  AlertTriangle, Check, Clock, Eye, File, FileCode, FileImage,
  FileSpreadsheet, FileText, Loader2, Lock, LockOpen, MessageSquare, Pencil,
  Plus, Presentation, RefreshCw, Trash2, UploadCloud,
} from 'lucide-react'
import { Trans, useTranslation } from 'react-i18next'
import { api, fetchProjectPagePng } from './api.js'
import { useJobs } from './jobs.jsx'
import ReviewModal from '@/components/ReviewModal.jsx'
import UnconfirmedFilesDialog from '@/components/UnconfirmedFilesDialog.jsx'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table'
import { usePageTitle } from '@/lib/usePageTitle.js'
import { useLocale } from '@/lib/format.js'
import { cn } from '@/lib/utils'

// tutti i formati che il motore sa redigere (progetti anonimizzati);
// nei progetti in chiaro il file si salva com'è e va bene tutto
const ACCEPT_ANON = '.pdf,.docx,.doc,.odt,.pptx,.ppt,.odp,.xlsx,.xls,.xlsm,.ods,.txt,.md,.csv,.tsv,.json,.xml,.png,.jpg,.jpeg,.bmp,.gif,.tif,.tiff,.webp'
const IMAGE_RE = /\.(png|jpe?g|bmp|gif|tiff?|webp)$/i

function FileIcon({ name, className }) {
  const ext = (name.match(/\.([a-z0-9]+)$/i)?.[1] || '').toLowerCase()
  const cls = cn('size-4 shrink-0', className)
  if (['xlsx', 'xls', 'xlsm', 'ods'].includes(ext))
    return <FileSpreadsheet className={cn(cls, 'text-emerald-700')} />
  if (['pptx', 'ppt', 'odp'].includes(ext))
    return <Presentation className={cn(cls, 'text-orange-600')} />
  if (['docx', 'doc', 'odt'].includes(ext))
    return <FileText className={cn(cls, 'text-blue-700')} />
  if (ext === 'pdf') return <FileText className={cn(cls, 'text-red-700')} />
  if (['csv', 'tsv'].includes(ext))
    return <FileSpreadsheet className={cn(cls, 'text-teal-700')} />
  if (['json', 'xml'].includes(ext))
    return <FileCode className={cn(cls, 'text-violet-700')} />
  if (['txt', 'md'].includes(ext))
    return <FileText className={cn(cls, 'text-muted-foreground')} />
  if (['png', 'jpg', 'jpeg', 'bmp', 'gif', 'tif', 'tiff', 'webp'].includes(ext))
    return <FileImage className={cn(cls, 'text-amber-600')} />
  return <File className={cn(cls, 'text-muted-foreground')} />
}

const fmtDate = (iso, locale) =>
  new Date(iso).toLocaleString(locale, {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
  })

const fmtSize = (n) => {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

// Editor delle categorie del progetto (le stesse per file e chat).
function CategoriesDialog({ open, onOpenChange, project, tags, onSaved }) {
  const [excluded, setExcluded] = useState([])
  const [saving, setSaving] = useState(false)
  const { t } = useTranslation('project')
  useEffect(() => {
    if (open) setExcluded(project?.anon_options?.excluded_tags || [])
  }, [open, project])
  const set = new Set(excluded)
  function toggle(tag) {
    const next = new Set(set)
    if (next.has(tag)) next.delete(tag)
    else next.add(tag)
    setExcluded([...next].sort())
  }
  async function save() {
    setSaving(true)
    try {
      const p = await api.patchProject(project.id, {
        anon_options: { excluded_tags: excluded },
      })
      onSaved(p)
      onOpenChange(false)
    } catch (e) {
      toast.error(e.message)
    } finally {
      setSaving(false)
    }
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t('catsDialog.title')}</DialogTitle>
          <DialogDescription>{t('catsDialog.description')}</DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3">
          {tags.map((tag) => (
            <label key={tag}
                   className="flex cursor-pointer select-none items-center gap-2 rounded-md px-1.5 py-1 hover:bg-accent">
              <Checkbox checked={!set.has(tag)} onCheckedChange={() => toggle(tag)} />
              <span className="font-mono text-xs">{tag}</span>
            </label>
          ))}
        </div>
        <DialogFooter>
          <Button variant="outline" disabled={saving} onClick={() => onOpenChange(false)}>
            {t('actions.cancel', { ns: 'common' })}
          </Button>
          <Button disabled={saving} onClick={save}>
            {t('actions.save', { ns: 'common' })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// Route /projects/:id — l'ambiente del progetto: file pre-caricati (con
// anonimizzazione in coda job nei progetti anonimizzati) e le sue chat.
export default function ProjectPage() {
  const { id } = useParams()
  const [project, setProject] = useState(null)
  const [error, setError] = useState('')
  const [dragOver, setDragOver] = useState(false)
  // Coda dei file scelti: [{key, name, size, state}] con state 'wait' | 'up'.
  // 'up' copre anche la SONDA pre-upload, che di quel viaggio è la prima metà.
  const [uploads, setUploads] = useState([])
  // popup OCR del lotto: null | {items: [{key, name, images, isImage, noText}]}
  const [askOcr, setAskOcr] = useState(null)
  const [renaming, setRenaming] = useState(false)
  const [name, setName] = useState('')
  const [editCats, setEditCats] = useState(false)
  const [tags, setTags] = useState([])
  const [toDeleteFile, setToDeleteFile] = useState(null)
  const [askUnconfirmed, setAskUnconfirmed] = useState(false)
  const [askRebuild, setAskRebuild] = useState(null)  // null | [file] da ri-redigere
  const [verifying, setVerifying] = useState(null)    // file id in verifica
  const [review, setReview] = useState(null)   // descrittore del file nel modal
  const [opening, setOpening] = useState(null) // file id in apertura
  const fileInput = useRef(null)
  // i lotti in attesa (un lotto = i file scelti o trascinati in un colpo
  // solo), la sentinella dell'unico lavoratore che li svuota e la risposta
  // del popup OCR, che il lavoratore aspetta come una promessa qualsiasi
  const batchQueue = useRef([])
  const uploadRunning = useRef(false)
  const uploadSeq = useRef(0)
  const askResolve = useRef(null)
  const navigate = useNavigate()
  const { addJob, startUpload, dismissJob, docsTick, docRefresh } = useJobs()
  const { t } = useTranslation('project')
  const locale = useLocale()
  usePageTitle(project?.name || t('fallbackTitle'))

  const refresh = useCallback(() => {
    api.getProject(id)
      .then((p) => { setProject(p); setError('') })
      .catch((e) => setError(e.message))
  }, [id])

  // docsTick cambia quando un job finisce: file nuovi compaiono da soli
  useEffect(() => { refresh() }, [refresh, docsTick])
  useEffect(() => {
    api.getTags().then((r) => setTags(r.all)).catch(() => {})
  }, [])

  // il registro condiviso del progetto: le fusioni proposte se le chiede il
  // modal all'apertura, così tengono conto di TUTTI i file caricati fin lì
  const registry = useMemo(() => api.projectRegistry(id), [id])

  // stessi contratti dei servizi Documenti, sugli endpoint del progetto
  const reviewId = review?.id
  const services = useMemo(() => (reviewId ? {
    // qui l'anonimizzazione di colonna è un JOB in coda (nell'anteprima
    // della chat è sincrona): il viewer ne tiene conto nella conferma
    columnJobs: true,
    fetchPagePng: (_fid, source, n, rev) =>
      fetchProjectPagePng(id, reviewId, source, n, rev),
    extractText: (_fid, source, page, rect) =>
      api.projectExtractText(id, reviewId, source, page, rect),
    deanonymize: (_fid, body) => api.projectDeanonymize(id, reviewId, body),
    anonymizeText: (_fid, text, saveTerm, termTag) =>
      api.projectAnonymizeText(id, reviewId, text, saveTerm, termTag),
    getColumns: () => api.projectGetColumns(id, reviewId),
    columnInfo: (_fid, sheet, column) =>
      api.projectColumnInfo(id, reviewId, sheet, column),
    anonymizeColumn: (_fid, sheet, column) =>
      api.projectAnonymizeColumn(id, reviewId, sheet, column),
    deanonymizeColumn: (_fid, sheet, column) =>
      api.projectDeanonymizeColumn(id, reviewId, sheet, column),
    sealArea: (_fid, page, rect, allPages) =>
      api.projectSealArea(id, reviewId, page, rect, allPages),
    removeSeal: (_fid, n) => api.projectRemoveSeal(id, reviewId, n),
    reprocessOcr: (_fid) => api.projectReprocessOcr(id, reviewId),
  } : null), [id, reviewId])

  // job di colonna o rielaborazione OCR sul file aperto nel modal: mappa e
  // anteprime sono cambiate sotto i piedi
  useEffect(() => {
    if (docRefresh.n > 0 && reviewId && docRefresh.docId === reviewId) {
      api.getProjectFile(id, reviewId).then(setReview).catch(() => {})
    }
  }, [docRefresh])           // eslint-disable-line react-hooks/exhaustive-deps

  const isAnon = !!project?.anonymized

  // Un LOTTO sono i file scelti (o trascinati) in un colpo solo: la domanda
  // sull'OCR si fa una volta per lotto. Quelli scelti mentre la coda gira
  // formano un lotto nuovo, che aspetta il suo turno.
  function handleFiles(list) {
    const files = Array.from(list || [])
    if (!files.length || !project) return
    const items = files.map((file) => ({
      key: `up-${++uploadSeq.current}`, file, name: file.name, size: file.size,
    }))
    batchQueue.current.push(items)
    // i file esistono per l'utente da quando li sceglie, non da quando
    // partono: il chip in coda c'è già adesso
    setUploads((u) => [...u, ...items.map(({ file, ...chip }) => ({ ...chip, state: 'wait' }))])
    pumpBatches()
  }

  const markUpload = (key, state) =>
    setUploads((u) => u.map((x) => (x.key === key ? { ...x, state } : x)))
  const dropUpload = (key) => setUploads((u) => u.filter((x) => x.key !== key))

  // Ne gira SEMPRE una copia sola: i lotti si svuotano in fila, e verso il
  // server parte un file alla volta — la coda è solo lato client.
  async function pumpBatches() {
    if (uploadRunning.current) return
    uploadRunning.current = true
    try {
      while (batchQueue.current.length) {
        const batch = batchQueue.current.shift()
        try {
          await runBatch(batch)
        } catch (e) {
          // imprevisto fuori dai singoli file: la coda non deve morire qui
          toast.error(e.message, { duration: 8000 })
          batch.forEach((it) => dropUpload(it.key))
        }
      }
    } finally {
      uploadRunning.current = false
    }
  }

  async function runBatch(batch) {
    if (!isAnon) {
      // progetto in chiaro: si salva com'è, nessuna sonda e nessun popup
      const done = []
      for (const it of batch) {
        markUpload(it.key, 'up')
        const card = startUpload(it.name)
        try {
          await api.uploadProjectFile(project.id, it.file)
          done.push(it.name)
        } catch (e) {
          // un file rifiutato non ferma quelli dopo di lui
          toast.error(`${it.name}: ${e.message}`, { duration: 8000 })
        } finally {
          dismissJob(card)
          dropUpload(it.key)
        }
      }
      if (done.length) {
        toast.success(done.length === 1 ? t('toast.uploaded')
                                        : t('toast.uploadedMany', { count: done.length }),
                      { description: done.length === 1 ? done[0] : undefined })
        refresh()
      }
      return
    }

    // La sonda porta già sul server TUTTI i byte e li lascia da parte
    // (probe_id): l'upload vero non li ritrasferisce. È il trasferimento
    // vero e proprio, quindi ha la sua card. Se fallisce si procede lo
    // stesso — sarà l'upload a dire cosa non va del file.
    for (const it of batch) {
      markUpload(it.key, 'up')
      const card = startUpload(it.name)
      it.probe = await api.probeProject(it.file).catch(() => null)
      dismissJob(card)
      markUpload(it.key, 'wait')
    }

    // Una sola domanda per tutto il lotto, sui file che hanno davvero
    // qualcosa da leggere.
    const ask = batch.filter((it) => it.probe?.ocr_available
      && (it.probe.images > 0 || IMAGE_RE.test(it.name)))
    let useOcr = false
    if (ask.length) {
      const answer = await askOcrBatch(ask)
      if (answer === null) {
        // annullato: i file della domanda si fermano qui e i byte messi da
        // parte dalla sonda non restano sul server fino alla scadenza; gli
        // altri file del lotto non c'entrano e proseguono
        for (const it of ask) {
          if (it.probe?.probe_id) api.dropProbe(it.probe.probe_id).catch(() => {})
          it.skipped = true
          dropUpload(it.key)
        }
      } else {
        useOcr = answer
      }
    }

    const queued = []
    for (const it of batch) {
      if (it.skipped) continue
      // Un'immagine senza OCR non avrebbe niente da anonimizzare: lì l'OCR
      // resta acceso comunque, esattamente come quando si caricava un file
      // solo (il popup, per un'immagine, non offriva il "solo testo").
      const ocr = !!it.probe?.ocr_available
        && (useOcr || (IMAGE_RE.test(it.name) && ask.includes(it)))
      const options = ocr ? { ocr: true } : {}
      markUpload(it.key, 'up')
      const card = startUpload(it.name)
      try {
        let job
        try {
          job = await api.uploadProjectFile(project.id, it.file, options,
                                            it.probe?.probe_id)
        } catch (e) {
          // biglietto scaduto o già consumato: si rimanda il file davvero
          if (e.status !== 410 || !it.probe?.probe_id) throw e
          job = await api.uploadProjectFile(project.id, it.file, options)
        }
        addJob(job)   // 202: il descrittore del job, seguito dalla sidebar
        queued.push(it.name)
      } catch (e) {
        toast.error(`${it.name}: ${e.message}`, { duration: 8000 })
      } finally {
        dismissJob(card)
        dropUpload(it.key)
      }
    }
    if (queued.length) {
      toast.success(queued.length === 1 ? t('toast.queued')
                                        : t('toast.queuedMany', { count: queued.length }),
                    { description: queued.length === 1 ? queued[0] : undefined })
    }
  }

  // Il popup vive fuori dal flusso: la coda si ferma su questa promessa e
  // riparte con la risposta (true = OCR, false = solo testo, null = annulla).
  function askOcrBatch(items) {
    return new Promise((resolve) => {
      askResolve.current = resolve
      setAskOcr({
        items: items.map((it) => ({
          key: it.key,
          name: it.name,
          images: it.probe?.images || 0,
          isImage: IMAGE_RE.test(it.name),
          noText: !!it.probe?.pdf_no_text,
        })),
      })
    })
  }

  function answerOcr(value) {
    const resolve = askResolve.current
    askResolve.current = null
    setAskOcr(null)
    resolve?.(value)
  }

  // Un file da rivedere si apre SOLO nel modal (lì si conferma); una volta
  // confermato non c'è più niente da decidere e si guarda nella sua pagina.
  function openFile(f) {
    if (!isAnon || f.busy || opening) return
    if (f.confirmed) navigate(`/projects/${project.id}/files/${f.id}`)
    else openReview(f)
  }

  // «Da rivedere»: la revisione del file si apre nello STESSO modal
  // dell'anteprima pre-invio della chat. È l'unico posto in cui si conferma.
  async function openReview(f) {
    setOpening(f.id)
    try {
      setReview(await api.getProjectFile(project.id, f.id))
    } catch (e) {
      toast.error(e.message)
    } finally {
      setOpening(null)
    }
  }

  async function confirmReview() {
    try {
      await api.confirmProjectFile(project.id, review.id)
      toast.success(t('toast.confirmed'), { description: review.filename })
      setReview(null)
      refresh()
    } catch (e) {
      // il 409 del controllo di uscita spiega già cosa fare (riallineare):
      // il modal resta aperto, il riallineamento è lì nel footer
      toast.error(e.message, { duration: 10000 })
    }
  }

  async function realignReview() {
    try {
      setReview(await api.realignProjectFile(project.id, review.id))
      toast.success(t('toast.realigned'))
    } catch (e) {
      toast.error(e.message, { duration: 10000 })
    }
  }

  async function removeFile(f) {
    try {
      await api.deleteProjectFile(project.id, f.id)
      toast.success(t('toast.deleted'), { description: f.filename })
      refresh()
    } catch (e) {
      toast.error(e.message)
    }
  }

  async function saveName() {
    setRenaming(false)
    const next = name.trim()
    if (!next || next === project.name) return
    try {
      const p = await api.patchProject(project.id, { name: next })
      setProject((cur) => ({ ...cur, ...p }))
    } catch (e) {
      toast.error(e.message)
    }
  }

  // I file non confermati non arrivano al modello: prima di aprire una chat
  // si avvisa (a meno che il progetto non abbia già detto «non chiedermelo
  // più»). Restano fuori quelli ancora in lavorazione: non sono confermabili
  // adesso e il loro stato è già visibile nella riga.
  const unconfirmed = isAnon
    ? (project?.files || []).filter((f) => !f.confirmed && !f.busy)
    : []

  function newChat() {
    if (unconfirmed.length && !project.skip_unconfirmed_warning) {
      setAskUnconfirmed(true)
      return
    }
    doNewChat()
  }

  async function doNewChat() {
    setAskUnconfirmed(false)
    try {
      const c = await api.createChat('', isAnon, project.id)
      navigate(`/projects/${project.id}/chat?chat=${c.id}`)
    } catch (e) {
      toast.error(e.message)
    }
  }

  // File rimasti indietro rispetto alla mappa: il registro ha imparato — da
  // altri file o dalle chat — valori che questi contengono ancora in chiaro.
  const staleFiles = (project?.files || []).filter((f) => f.stale && !f.busy)
  const staleValues = [...new Set(staleFiles.flatMap((f) => f.stale_values || []))]
  const uncheckedFiles = (project?.files || [])
    .filter((f) => f.stale === null && !f.busy)

  async function verifyStale(f) {
    setVerifying(f.id)
    try {
      const { file } = await api.projectStaleCheck(project.id, f.id)
      setProject((cur) => ({
        ...cur,
        files: (cur.files || []).map((x) => (x.id === file.id ? file : x)),
      }))
      if (!file.stale) toast.success(t('stale.aligned'), { description: f.filename })
    } catch (e) {
      toast.error(e.message)
    } finally {
      setVerifying(null)
    }
  }

  // La ri-redazione è un job come l'upload: su un file grosso dura uguale.
  async function rebuildFiles(files) {
    setAskRebuild(null)
    for (const f of files) {
      try {
        addJob(await api.rebuildProjectFile(project.id, f.id))
      } catch (e) {
        toast.error(t('toast.rebuildFailed', { file: f.filename, error: e.message }),
                    { duration: 8000 })
      }
    }
    refresh()   // le righe passano a «In lavorazione»
  }

  async function skipUnconfirmedWarning() {
    setProject((cur) => ({ ...cur, skip_unconfirmed_warning: true }))
    try {
      await api.patchProject(project.id, { skip_unconfirmed_warning: true })
    } catch (e) {
      toast.error(e.message)
    }
  }

  if (error) {
    return (
      <main className="min-w-0 flex-1 overflow-y-auto p-6">
        <Link to="/projects" className="text-sm text-muted-foreground hover:underline">
          {t('backToProjects')}
        </Link>
        <div className="mt-4 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      </main>
    )
  }
  if (!project) {
    return (
      <main className="min-w-0 flex-1 overflow-y-auto p-6 text-muted-foreground">
        {t('state.loading', { ns: 'common' })}
      </main>
    )
  }

  // il file davvero in viaggio (gli altri della coda aspettano dietro, e
  // mentre il popup è aperto non si muove niente: la dropzone non deve
  // raccontare un trasferimento che non c'è)
  const active = uploads.find((u) => u.state === 'up')
  const busy = !!active
  const sizeLabel = active ? fmtSize(active.size) : ''
  // il popup del lotto: tutte immagini => niente "solo testo" (come per una
  // singola immagine: senza OCR non ci sarebbe nulla da anonimizzare)
  const askItems = askOcr?.items || []
  const askImages = askItems.filter((i) => i.isImage)
  const askDocs = askItems.filter((i) => !i.isImage)

  return (
    <main className="min-w-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 p-6">
        <Link to="/projects" className="text-sm text-muted-foreground hover:underline">
          {t('backToProjects')}
        </Link>

        <header className="flex flex-wrap items-center gap-3">
          {renaming ? (
            <Input value={name} autoFocus className="max-w-sm text-lg font-semibold"
                   onChange={(e) => setName(e.target.value)}
                   onBlur={saveName}
                   onKeyDown={(e) => {
                     if (e.key === 'Enter') saveName()
                     if (e.key === 'Escape') setRenaming(false)
                   }} />
          ) : (
            <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
              {project.name}
              <Button variant="ghost" size="icon-sm" title={t('rename')}
                      aria-label={t('renameAria')}
                      onClick={() => { setName(project.name); setRenaming(true) }}>
                <Pencil className="size-3.5" />
              </Button>
            </h1>
          )}
          <span className={cn(
            'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs',
            isAnon ? 'border-emerald-500/40 text-emerald-700 dark:text-emerald-400'
                   : 'border-amber-500/40 text-amber-700 dark:text-amber-400')}
                title={t('modeHint')}>
            {isAnon ? <Lock className="size-3" /> : <LockOpen className="size-3" />}
            {t(isAnon ? 'modeAnon' : 'modePlain')}
          </span>
          {isAnon && (
            <Button variant="outline" size="sm" className="h-6 px-2 text-xs"
                    onClick={() => setEditCats(true)}>
              {t('categories')}
            </Button>
          )}
          <Button className="ml-auto gap-2" onClick={() => navigate(`/projects/${project.id}/chat`)}>
            <MessageSquare className="size-4" /> {t('openChats')}
          </Button>
        </header>

        {isAnon && (
          <p className="text-xs text-muted-foreground">
            {/* due frasi intere invece di una coda appiccicata: in inglese
                l'aggiunta non cade nello stesso punto */}
            <Trans ns="project" components={{ b: <b /> }}
                   i18nKey={project.has_messages ? 'anonIntroLocked' : 'anonIntro'} />
          </p>
        )}

        {/* dropzone */}
        <div
          role="button"
          tabIndex={0}
          aria-label={t('drop.aria')}
          data-tour={isAnon ? 'dropzone-anon' : 'dropzone-plain'}
          className={cn(
            'flex cursor-pointer flex-col items-center gap-2 rounded-xl border-2 border-dashed border-border bg-card px-6 py-6 text-center transition-colors',
            'hover:border-primary/60 hover:bg-secondary/40',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40',
            dragOver && 'border-primary bg-secondary/60',
            // verso il server i file vanno uno alla volta, ma la dropzone
            // resta viva: quelli scelti adesso si mettono in coda
            busy && 'border-primary/50 bg-secondary/40'
          )}
          aria-busy={busy}
          onClick={() => fileInput.current?.click()}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') fileInput.current?.click() }}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDragOver(false)
            handleFiles(e.dataTransfer.files)
          }}
        >
          <span className="flex size-9 items-center justify-center rounded-full bg-secondary text-secondary-foreground">
            {busy ? <Loader2 className="size-4 animate-spin" /> : <UploadCloud className="size-4" />}
          </span>
          <div className="text-sm font-medium">
            {busy
              ? <Trans i18nKey="drop.busy" ns="project"
                       values={{ name: active?.name || '',
                                 size: sizeLabel ? ` (${sizeLabel})` : '' }}
                       components={{ name: <span className="break-all" /> }} />
              : t('drop.idle')}
          </div>
          <div className="text-xs text-muted-foreground">
            {busy ? t('drop.busyHint')
                  : t(isAnon ? 'drop.hintAnon' : 'drop.hintPlain')}
          </div>
          {busy && (
            <div className="text-xs text-muted-foreground">{t('drop.busyMore')}</div>
          )}
          <input ref={fileInput} type="file" hidden multiple
                 accept={isAnon ? ACCEPT_ANON : undefined}
                 onChange={(e) => { handleFiles(e.target.files); e.target.value = '' }} />
        </div>

        {/* la coda: chi sta viaggiando e chi aspetta il suo turno */}
        {uploads.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {uploads.map((u) => (
              <div key={u.key}
                   className="inline-flex items-center gap-1.5 rounded-md border border-dashed border-border bg-card px-2 py-1.5 text-xs text-muted-foreground">
                {u.state === 'up'
                  ? <Loader2 className="size-3 shrink-0 animate-spin text-primary" />
                  : <Clock className="size-3 shrink-0" />}
                <span className="max-w-[220px] truncate text-card-foreground"
                      title={u.name}>{u.name}</span>
                <span className="shrink-0">
                  {t(u.state === 'up' ? 'queue.up' : 'queue.wait',
                     { size: fmtSize(u.size) })}
                </span>
              </div>
            ))}
          </div>
        )}

        {/* file rimasti indietro rispetto alla mappa corrente */}
        {staleFiles.length > 0 && (
          <div className="flex flex-col gap-2.5 rounded-xl border border-amber-500/40 bg-amber-500/5 px-4 py-3">
            <div className="flex items-center gap-2 text-sm font-medium text-amber-700 dark:text-amber-400">
              <AlertTriangle className="size-4 shrink-0" />
              {t('stale.title', { count: staleFiles.length })}
            </div>
            <p className="text-xs text-muted-foreground">
              {/* i valori trapelati stanno DENTRO la frase, in grassetto: la
                  loro presenza cambia la punteggiatura, quindi sono due frasi
                  distinte e non un pezzo cucito a runtime */}
              <Trans ns="project" count={staleFiles.length}
                     i18nKey={staleValues.length ? 'stale.bodyValues' : 'stale.body'}
                     values={{ values: staleValues.slice(0, 3).join(', ') }}
                     components={{ b: <b className="text-foreground" /> }} />
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" className="h-7 gap-1.5 px-2.5 text-xs"
                      onClick={() => setAskRebuild(staleFiles)}>
                <RefreshCw className="size-3" />
                {t('stale.rebuild', { count: staleFiles.length })}
              </Button>
              <span className="text-xs text-muted-foreground">
                {staleFiles.map((f) => f.filename).join(' · ')}
              </span>
            </div>
          </div>
        )}

        {/* file troppo grossi per verificarli all'apertura del progetto */}
        {uncheckedFiles.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-card px-4 py-2.5 text-xs text-muted-foreground">
            <span>{t('unchecked.label', { count: uncheckedFiles.length })}</span>
            {uncheckedFiles.map((f) => (
              <Button key={f.id} variant="outline" size="sm"
                      className="h-6 gap-1 px-2 text-[11px]"
                      disabled={verifying === f.id}
                      onClick={() => verifyStale(f)}>
                {verifying === f.id
                  ? <Loader2 className="size-3 animate-spin" />
                  : <RefreshCw className="size-3" />}
                {t('unchecked.verify', { file: f.filename })}
              </Button>
            ))}
          </div>
        )}

        {/* file del progetto */}
        {project.files.length > 0 && (
          <div className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>{t('table.file')}</TableHead>
                  {isAnon && <TableHead className="w-32">{t('table.status')}</TableHead>}
                  <TableHead className="w-24 text-right">{t('table.size')}</TableHead>
                  <TableHead className="w-40">{t('table.uploaded')}</TableHead>
                  {isAnon && <TableHead className="w-28" />}
                  <TableHead className="w-12" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {project.files.map((f) => (
                  <TableRow key={f.id}
                            className={cn(isAnon && !f.busy && 'cursor-pointer')}
                            onClick={() => openFile(f)}>
                    <TableCell className="max-w-0">
                      <div className="flex items-center gap-2.5">
                        <FileIcon name={f.filename} />
                        <div className="min-w-0">
                          <div className="truncate font-medium" title={f.filename}>
                            {f.filename}
                          </div>
                          {f.briefing?.label && (
                            <div className="truncate text-xs text-muted-foreground"
                                 title={f.briefing.label}>
                              {f.briefing.label}
                            </div>
                          )}
                        </div>
                      </div>
                    </TableCell>
                    {isAnon && (
                      <TableCell>
                        <div className="flex flex-col items-start gap-1">
                          {f.busy ? (
                            <Badge variant="outline" className="gap-1 text-xs">
                              <Loader2 className="size-3 animate-spin" /> {t('status.busy')}
                            </Badge>
                          ) : f.confirmed ? (
                            <Badge variant="outline"
                                   className="gap-1 border-emerald-500/40 text-xs text-emerald-700 dark:text-emerald-400">
                              <Check className="size-3" /> {t('status.confirmed')}
                            </Badge>
                          ) : null}
                          {!f.busy && f.stale && (
                            <Badge variant="outline"
                                   className="gap-1 border-amber-500/40 text-xs text-amber-700 dark:text-amber-400"
                                   title={(f.stale_values || []).length
                                     ? t('stale.badgeHintValues',
                                         { values: f.stale_values.join(', ') })
                                     : t('stale.badgeHint')}>
                              <AlertTriangle className="size-3" /> {t('stale.badge')}
                            </Badge>
                          )}
                        </div>
                      </TableCell>
                    )}
                    <TableCell className="text-right tabular-nums text-muted-foreground">
                      {fmtSize(f.size)}
                    </TableCell>
                    <TableCell className="whitespace-nowrap tabular-nums text-muted-foreground">
                      {fmtDate(f.created_at, locale)}
                    </TableCell>
                    {isAnon && (
                      <TableCell className="text-right">
                        {f.busy ? null : f.confirmed ? (
                          <Button variant="ghost" size="sm"
                                  className="h-6 gap-1 px-2 text-[11px]"
                                  title={t('status.open')}
                                  onClick={(e) => { e.stopPropagation(); openFile(f) }}>
                            <Eye className="size-3" /> {t('status.view')}
                          </Button>
                        ) : (
                          <Button variant="outline" size="sm"
                                  className="h-6 gap-1 border-amber-500/40 px-2 text-[11px] text-amber-700 dark:text-amber-400"
                                  disabled={opening === f.id}
                                  title={t('status.reviewHint')}
                                  onClick={(e) => { e.stopPropagation(); openFile(f) }}>
                            {opening === f.id
                              ? <Loader2 className="size-3 animate-spin" />
                              : <Clock className="size-3" />}
                            {t('status.toConfirm')}
                          </Button>
                        )}
                      </TableCell>
                    )}
                    <TableCell className="text-right">
                      <Button variant="ghost" size="icon-sm"
                              title={t('actions.delete', { ns: 'common' })}
                              aria-label={t('deleteAria', { file: f.filename })}
                              className="text-muted-foreground hover:text-destructive"
                              onClick={(e) => { e.stopPropagation(); setToDeleteFile(f) }}>
                        <Trash2 />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}

        {/* chat del progetto */}
        <section className="flex flex-col gap-3">
          <header className="flex items-center gap-2.5">
            <h2 className="text-base font-semibold">{t('chats.title')}</h2>
            {project.chats.length > 0 && <Badge variant="secondary">{project.chats.length}</Badge>}
            <Button variant="outline" size="sm" className="ml-auto gap-1.5" data-tour="project-new-chat" onClick={newChat}>
              <Plus className="size-3.5" /> {t('chats.new')}
            </Button>
          </header>
          {project.chats.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {t(isAnon ? 'chats.emptyAnon' : 'chats.emptyPlain')}
            </p>
          ) : (
            <ul className="flex flex-col overflow-hidden rounded-xl border border-border bg-card shadow-sm">
              {project.chats.map((c) => (
                <li key={c.id} className="border-b border-border last:border-b-0">
                  <button type="button"
                          className="flex w-full items-center gap-2.5 px-4 py-2.5 text-left text-sm hover:bg-accent"
                          onClick={() => navigate(`/projects/${project.id}/chat?chat=${c.id}`)}>
                    <MessageSquare className="size-4 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate font-medium">{c.title}</span>
                    <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                      {fmtDate(c.updated_at, locale)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      {/* revisione + conferma del file: lo stesso modal dell'anteprima
          pre-invio della chat, con «Conferma» al posto di «Invia» */}
      {review && services && (
        <ReviewModal items={[review]} services={services} registry={registry}
                     confirmLabel={t('confirmReview')} confirmIcon={Check}
                     onChange={setReview}
                     onJobStart={addJob}
                     onRealign={realignReview}
                     onCancel={() => setReview(null)}
                     onConfirm={confirmReview} />
      )}

      {/* popup OCR (solo progetti anonimizzati): una domanda sola per tutto
          il lotto, con l'elenco dei file che riguarda */}
      <Dialog open={!!askOcr}
              onOpenChange={(open) => { if (!open && askResolve.current) answerOcr(null) }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>
              {askDocs.length === 0
                ? t('ocr.titleImage', { count: askImages.length })
                : t('ocr.titleDoc', { count: askDocs.length })}
            </DialogTitle>
            <DialogDescription className="space-y-2 break-words">
              <span className="block">
                {askDocs.length === 0 ? t('ocr.bodyImage', { count: askImages.length })
                                      : t('ocr.bodyBatch')}
              </span>
            </DialogDescription>
          </DialogHeader>
          {/* cosa c'è da leggere, file per file */}
          <ul className="max-h-52 space-y-1 overflow-y-auto text-sm">
            {askItems.map((i) => (
              <li key={i.key} className="flex items-start gap-2">
                <FileIcon name={i.name} className="mt-0.5 text-muted-foreground" />
                <span className="min-w-0 flex-1 break-words">
                  {i.name}
                  <span className="text-muted-foreground">
                    {' — '}
                    {i.isImage ? t('ocr.itemImage')
                               : t('ocr.itemImages', { count: i.images })}
                  </span>
                  {i.noText && (
                    <span className="block text-xs font-medium text-foreground">
                      {t('ocr.noText')}
                    </span>
                  )}
                </span>
              </li>
            ))}
          </ul>
          {/* "solo testo" non vale per le immagini vere e proprie: senza OCR
              non ci sarebbe niente da anonimizzare, e il file singolo non
              offriva quella scelta */}
          {askDocs.length > 0 && askImages.length > 0 && (
            <p className="text-xs text-muted-foreground">
              {t('ocr.batchImagesNote', { count: askImages.length })}
            </p>
          )}
          <DialogFooter>
            {askDocs.length === 0 ? (
              <Button variant="outline" onClick={() => answerOcr(null)}>
                {t('actions.cancel', { ns: 'common' })}
              </Button>
            ) : (
              <Button variant="outline" onClick={() => answerOcr(false)}>
                {t('ocr.textOnly')}
              </Button>
            )}
            <Button onClick={() => answerOcr(true)}>
              {t(askDocs.length === 0 ? 'ocr.useImage' : 'ocr.useDoc')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* conferma eliminazione file */}
      <AlertDialog open={!!toDeleteFile} onOpenChange={(open) => { if (!open) setToDeleteFile(null) }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('confirmDeleteFile.title')}</AlertDialogTitle>
            <AlertDialogDescription className="break-words">
              {t('confirmDeleteFile.text', { file: toDeleteFile?.filename || '' })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('actions.cancel', { ns: 'common' })}</AlertDialogCancel>
            <AlertDialogAction onClick={() => { const f = toDeleteFile; setToDeleteFile(null); removeFile(f) }}>
              {t('actions.delete', { ns: 'common' })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* ri-redazione dei file rimasti indietro */}
      <AlertDialog open={!!askRebuild}
                   onOpenChange={(open) => { if (!open) setAskRebuild(null) }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('confirmRebuild.title', { count: askRebuild?.length || 0 })}
            </AlertDialogTitle>
            <AlertDialogDescription className="space-y-2 break-words">
              <span className="block">
                {t('confirmRebuild.body', { count: askRebuild?.length || 0 })}
              </span>
              {(askRebuild || []).some((f) => f.confirmed) && (
                <span className="block">
                  {t('confirmRebuild.confirmed', {
                    count: askRebuild.filter((f) => f.confirmed).length,
                  })}
                </span>
              )}
              <span className="block">{t('confirmRebuild.queued')}</span>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('actions.cancel', { ns: 'common' })}</AlertDialogCancel>
            <AlertDialogAction onClick={() => rebuildFiles(askRebuild || [])}>
              {t('confirmRebuild.action')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* file ancora da confermare: il modello non li vedrebbe */}
      <UnconfirmedFilesDialog open={askUnconfirmed}
                              onOpenChange={setAskUnconfirmed}
                              files={unconfirmed}
                              onProceed={doNewChat}
                              onSkipForever={skipUnconfirmedWarning} />

      <CategoriesDialog open={editCats} onOpenChange={setEditCats}
                        project={project} tags={tags}
                        onSaved={(p) => setProject((cur) => ({ ...cur, ...p }))} />
    </main>
  )
}
