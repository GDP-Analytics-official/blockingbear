import { useMediaQuery } from '@/lib/useMediaQuery.js'
import ResponsiveSidebar from '@/components/ResponsiveSidebar.jsx'
import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import {
  Plus, Trash2, Send, Square, Paperclip, Loader2, FileDown, FolderKanban,
  Terminal, ChevronRight, ChevronDown, AlertCircle, Brain, MessageSquarePlus, ShieldOff,
  ShieldCheck, ShieldAlert, Lock, LockOpen, Check, Globe, BookOpen, ScanText,
  Clock, Copy, FileUp,
} from 'lucide-react'
import { Trans, useTranslation } from 'react-i18next'
import { useAuth } from './auth.jsx'
import { useChats } from './chats.jsx'
import { useJobs } from './jobs.jsx'
import {
  api, attachChatStream, downloadFile, fetchAttachmentUrl, fetchStagedPagePng,
  streamChatMessage,
} from './api.js'
import ReviewModal, { EntityReview } from '@/components/ReviewModal.jsx'
import UnconfirmedFilesDialog from '@/components/UnconfirmedFilesDialog.jsx'
import ModelSelector from '@/components/ModelSelector.jsx'
import ModelOptions from '@/components/ModelOptions.jsx'
import AnonTags from '@/components/AnonTags.jsx'
import FloatingPanel from '@/components/FloatingPanel.jsx'
import Markdown, { Linkify } from '@/components/Markdown.jsx'
import { Button } from '@/components/ui/button'
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { usePageTitle } from '@/lib/usePageTitle.js'
import { cn } from '@/lib/utils'

function fmtBytes(n) {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

// I costi per messaggio sono spesso frazioni di centesimo: due decimali li
// mostrerebbero tutti come "$0.00".
function fmtCost(c) {
  if (!c) return '$0'
  if (c < 0.01) return `$${c.toFixed(4)}`
  return `$${c.toFixed(2)}`
}

// Tempo di risposta del modello: sotto il minuto i decimi contano (le
// risposte brevi stanno sui 2-8 s), sopra no.
function fmtDuration(ms) {
  if (ms == null) return null
  const s = ms / 1000
  if (s < 10) return `${s.toFixed(1)} s`
  if (s < 60) return `${Math.round(s)} s`
  return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`
}

// L'SVG resta fuori: è un raster in meno da guardare (nelle chat anonimizzate
// il modello lo salva come SORGENTE del grafico, e il png accanto mostra già
// la stessa figura) e soprattutto un SVG è un documento, non un'immagine —
// aperto in una scheda esegue gli script che contiene, e qui l'ha scritto il
// modello. Come chip si scarica e basta.
function isImage(att) {
  const mime = att.mime || ''
  return mime.startsWith('image/') && !mime.startsWith('image/svg')
}

// Il ragionamento è contesto, non la risposta: sta su una riga sola e si
// apre solo se l'utente lo chiede. Mentre arriva mostriamo la CODA (l'ultima
// riga, o gli ultimi caratteri se il modello scrive un unico paragrafo): la
// riga si muove e si vede che sta ancora pensando.
function reasoningPreview(text, live) {
  const t = (text || '').trim()
  if (!t) return ''
  if (!live) return t.replace(/\s+/g, ' ')
  const last = t.split('\n').map((l) => l.trim()).filter(Boolean).pop() || ''
  return last.length > 110 ? `…${last.slice(-110)}` : last
}

function Reasoning({ text, live }) {
  const [open, setOpen] = useState(false)
  const { t } = useTranslation('chat')
  const preview = reasoningPreview(text, live)
  return (
    <div className="rounded-md border border-border bg-accent text-xs text-muted-foreground">
      <button type="button" onClick={() => setOpen((o) => !o)}
              title={t(open ? 'reasoning.hide' : 'reasoning.show')}
              className="flex w-full items-center gap-1.5 px-2 py-1.5 text-left">
        <Brain className={cn('size-3.5 shrink-0', live && 'animate-pulse text-primary')} />
        {open ? (
          <span className="min-w-0 flex-1 font-medium">
            {t(live ? 'reasoning.live' : 'reasoning.label')}
          </span>
        ) : (
          <span className="min-w-0 flex-1 truncate italic">
            {preview || t('reasoning.label')}
          </span>
        )}
        <ChevronRight className={cn('size-3.5 shrink-0 transition-transform', open && 'rotate-90')} />
      </button>
      {open && (
        <div className="max-h-72 overflow-y-auto whitespace-pre-wrap border-t border-border/60 px-2 py-1.5 italic leading-relaxed">
          {text}
        </div>
      )}
    </div>
  )
}

// Tre puntini: dice "non ho finito" dove il testo si è fermato.
function TypingDots() {
  return (
    <span className="inline-flex items-center gap-[3px]" aria-hidden="true">
      {[0, 160, 320].map((d) => (
        <span key={d} className="size-1.5 animate-bounce rounded-full bg-current opacity-70"
              style={{ animationDelay: `${d}ms` }} />
      ))}
    </span>
  )
}

// Cosa sta facendo il modello adesso: il tool in esecuzione ha la precedenza,
// poi il testo, poi il ragionamento; senza nulla, sta ancora rispondendo.
function liveLabel(msg, t) {
  const running = (msg.steps || []).find((s) => !s.result)
  if (running) {
    if (running.kind === 'search') return t('live.searching')
    if (running.kind === 'page') return t('live.reading')
    if (running.kind === 'ocr') return t('live.ocr')
    return t('live.running')
  }
  if (msg.content) return t('live.writing')
  if (msg.reasoning) return t('live.thinking')
  return t('live.waiting')
}

const STEP_ICONS = { code: Terminal, search: Globe, page: BookOpen, ocr: ScanText }

// Uno step tool nel messaggio assistant: collassabile, con la forma del suo
// `kind` — "code" mostra codice e output (troncato lato server), "search" la
// query REALE partita e i risultati come link, "page" l'URL e il contenuto
// letto. È la trasparenza che rende leggibile l'analisi (e, nelle chat
// anonimizzate, rende l'egress ispezionabile ricerca per ricerca).
function ToolStep({ step }) {
  const [open, setOpen] = useState(false)
  const { t } = useTranslation('chat')
  const r = step.result
  const running = !r
  const bad = r && r.outcome !== 'ok'
  const kind = step.kind || 'code'
  const args = step.args || {}
  const Icon = STEP_ICONS[kind] || Terminal
  const results = Array.isArray(r?.results) ? r.results : null
  const images = Array.isArray(r?.images) ? r.images : null
  const label = running ? t(`step.${kind}.running`)
    : bad ? t(`step.${kind}.failed`, { outcome: r.outcome })
    : kind === 'search' && results ? t('step.search.done', { count: results.length })
    : kind === 'ocr' && images ? t('step.ocr.done', { count: images.length })
    : t(`step.${kind}.done`)
  // il dettaglio in testata: la query per la ricerca, titolo o URL per la
  // pagina, il nome del file per l'OCR
  const hint = kind === 'search' ? (args.query || '')
    : kind === 'page' ? (r?.title || r?.url || args.url || '')
    : kind === 'ocr' ? (args.filename || '') : ''
  return (
    <div className="rounded-md border border-border bg-card/60 text-xs">
      <button className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left"
              onClick={() => setOpen((o) => !o)}>
        {running
          ? <Loader2 className="size-3.5 shrink-0 animate-spin text-primary" />
          : <Icon className={cn('size-3.5 shrink-0', bad ? 'text-destructive' : 'text-muted-foreground')} />}
        <span className="shrink-0 font-medium">{label}</span>
        {hint && <span className="min-w-0 truncate text-muted-foreground">{hint}</span>}
        {r && <span className="shrink-0 text-muted-foreground">{r.elapsed_ms} ms</span>}
        <ChevronRight className={cn('ml-auto size-3.5 shrink-0 transition-transform', open && 'rotate-90')} />
      </button>
      {open && (
        <div className="flex flex-col gap-2 border-t border-border px-2.5 py-2">
          {kind === 'search' && args.query && (
            <div className="text-muted-foreground">
              {t('step.search.query')}: <span className="text-foreground">{args.query}</span>
            </div>
          )}
          {kind === 'search' && results && results.map((res, i) => (
            <div key={i} className="flex min-w-0 flex-col">
              <a href={res.url} target="_blank" rel="noopener noreferrer"
                 className="truncate font-medium text-primary hover:underline">
                {res.title || res.url}
              </a>
              <span className="truncate text-[10px] text-muted-foreground">{res.url}</span>
              {res.snippet && <span className="text-muted-foreground">{res.snippet}</span>}
            </div>
          ))}
          {kind === 'page' && (r?.url || args.url) && (
            <a href={r?.url || args.url} target="_blank" rel="noopener noreferrer"
               className="truncate font-medium text-primary hover:underline">
              {r?.url || args.url}
            </a>
          )}
          {kind === 'page' && r?.content && (
            <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap rounded bg-muted/60 p-2 text-[11px]">
              {r.content}
            </pre>
          )}
          {kind === 'ocr' && images && images.map((img, i) => (
            <div key={i} className="flex min-w-0 flex-col gap-1">
              <span className="font-medium text-muted-foreground">
                {t('step.ocr.image', { index: img.index })}
                {img.page ? t('step.ocr.page', { page: img.page }) : ''}
              </span>
              {img.text && (
                <pre className="max-h-72 overflow-y-auto whitespace-pre-wrap rounded bg-muted/60 p-2 text-[11px]">
                  {img.text}
                </pre>
              )}
              {img.note && <span className="text-muted-foreground">{img.note}</span>}
            </div>
          ))}
          {kind === 'code' && step.code && (
            <pre className="overflow-x-auto rounded bg-muted p-2 font-mono text-[11px]">
              <code style={{ background: 'transparent', padding: 0 }}>{step.code}</code>
            </pre>
          )}
          {r?.stdout && (
            <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-muted/60 p-2 font-mono text-[11px]">
              {r.stdout}
            </pre>
          )}
          {r?.stderr && (
            <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-destructive/10 p-2 font-mono text-[11px] text-destructive">
              {r.stderr}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

// Download via fetch autenticato: un <a href> nudo non porta l'header
// Authorization e risponderebbe 401 (stesso motivo dei PNG di pagina).
//
// Gli stati sono pochi perché la scelta è della CHAT: un allegato di una
// chat anonimizzata è «in attesa dell'invio» finché non parte, e da lì in
// poi «anonimizzato». Non c'è niente da decidere sul singolo file.
function AttachmentState({ att }) {
  const { t } = useTranslation('chat')
  if (att.direction !== 'in') {
    const rep = att.anonymization_report
    if (att.anonymization_status === 'restored') {
      // il notice porta anche i limiti del ripristino PDF (valori scritti più
      // piccoli, segnaposto dentro le immagini): vanno letti anche quando il
      // ripristino è riuscito, non solo quando qualcosa è rimasto indietro
      const caveat = rep?.shrunk?.length || rep?.images
      return <span className="text-emerald-700 dark:text-emerald-400"
                   title={rep?.notice || ''}>
        {rep?.restored ? t('attachment.restoredWith', { what: rep.restored })
                       : t('attachment.restored')}
        {caveat ? t('attachment.withCaveats') : ''}
      </span>
    }
    return att.anonymization_status === 'protected'
      ? <span className="text-amber-700 dark:text-amber-400"
              title={rep?.notice || ''}>
          {rep?.remaining?.length
            ? t('attachment.stillPlaceholders', { count: rep.remaining.length })
            : t('attachment.stillProtected')}
        </span>
      : null
  }
  const count = att.anonymization_report?.n_entities
  if (att.anonymization_status === 'protected') {
    return <span className="text-emerald-700 dark:text-emerald-400">
      {count != null ? t('attachment.anonymizedCount', { count })
                     : t('attachment.anonymized')}
    </span>
  }
  if (att.anonymization_status === 'failed') {
    return <span className="text-destructive"
                 title={att.anonymization_report?.error || ''}>
      {t('attachment.failed')}
    </span>
  }
  if (att.anonymization_status === 'pending') {
    return <span className="text-muted-foreground">{t('attachment.pending')}</span>
  }
  return <span className="text-amber-700 dark:text-amber-400">
    {t(att.message_id ? 'attachment.sentPlain' : 'attachment.willSendPlain')}
  </span>
}

// text-card-foreground esplicito: dentro la bolla utente il colore ereditato è
// quello del fondo scuro, e il nome del file sarebbe bianco su bianco.
function AttachmentChip({ convId, att, pending = false, busy = false, onRemove }) {
  const { t } = useTranslation('chat')
  const card = att.briefing
  const hint = card?.label ? ` · ${card.label}` : ''
  // La copia redatta esiste solo quando anonymize_turn l'ha scritta (invio o
  // anteprima pre-invio): quando c'è, la card cambia forma — nome file come
  // etichetta (niente download al click) e due bottoni, anonimizzato in
  // evidenza e originale in seconda fila.
  const hasAnonymized = att.direction === 'in' && att.anonymization_status === 'protected'
  const downloadAnonymized = () =>
    downloadFile(api.chatAttachmentAnonymizedUrl(convId, att.id),
                 att.anonymized_filename || att.filename)
      .catch((e) => toast.error(e.message))
  const downloadOriginal = () =>
    downloadFile(api.chatAttachmentUrl(convId, att.id), att.filename)
      .catch((e) => toast.error(e.message))
  const removeButton = pending && (
    <button type="button"
            className="inline-flex cursor-pointer items-center gap-1 rounded-md border border-destructive/50 px-2 py-1 text-destructive hover:bg-destructive/10 disabled:cursor-default disabled:opacity-50"
            disabled={busy} onClick={() => onRemove?.(att)}
            title={t('attachment.removeHelp')}>
      <Trash2 className="size-3" /> {t('attachment.remove')}
    </button>
  )
  if (hasAnonymized) {
    return (
      <div className="inline-flex min-w-0 max-w-full flex-col gap-1.5 rounded-md border border-border bg-card px-2 py-1.5 text-xs text-card-foreground">
        <span className="max-w-[220px] truncate"
              title={`${att.filename} · ${fmtBytes(att.size)}${hint}`}>
          {att.filename}
        </span>
        <div className="flex flex-wrap items-center gap-1.5">
          <button type="button"
                  className="inline-flex cursor-pointer items-center gap-1 rounded-md border border-primary/50 px-2 py-1 font-medium text-primary hover:bg-primary/10"
                  onClick={downloadAnonymized}
                  title={t('attachment.downloadAnonHelp')}>
            <FileDown className="size-3" /> {t('attachment.downloadAnon')}
          </button>
          <button type="button"
                  className="inline-flex cursor-pointer items-center gap-1 rounded-md border border-foreground/30 px-2 py-1 text-foreground/85 hover:bg-accent hover:text-foreground"
                  onClick={downloadOriginal}
                  title={t('attachment.downloadOriginalHelp')}>
            <FileDown className="size-3" /> {t('attachment.downloadOriginal')}
          </button>
          {removeButton}
        </div>
      </div>
    )
  }
  if (pending) {
    return (
      <div className="inline-flex min-w-0 max-w-full flex-col gap-1.5 rounded-md border border-border bg-card px-2 py-1.5 text-xs text-card-foreground">
        <span className="max-w-[220px] truncate"
              title={`${att.filename} · ${fmtBytes(att.size)}${hint}`}>
          {att.filename}
        </span>
        <AttachmentState att={att} />
        <div className="flex flex-wrap items-center gap-1.5">
          <button type="button"
                  className="inline-flex cursor-pointer items-center gap-1 rounded-md border border-foreground/30 px-2 py-1 text-foreground/85 hover:bg-accent hover:text-foreground"
                  onClick={downloadOriginal}
                  title={t('attachment.downloadHelp', { file: att.filename })}>
            <FileDown className="size-3" /> {t('attachment.download')}
          </button>
          {removeButton}
        </div>
      </div>
    )
  }
  return (
    <div className="inline-flex min-w-0 max-w-full flex-col gap-1 rounded-md border border-border bg-card px-2 py-1 text-xs text-card-foreground">
      <button type="button"
              onClick={downloadOriginal}
              className="inline-flex cursor-pointer items-center gap-1.5 hover:text-primary"
              title={`${t('attachment.downloadHelp', { file: att.filename })} · ${fmtBytes(att.size)}${hint}`}>
        <FileDown className="size-3.5 text-muted-foreground" />
        <span className="max-w-[220px] truncate">{att.filename}</span>
        {card?.label && <span className="hidden text-muted-foreground sm:inline">{card.label}</span>}
      </button>
      <AttachmentState att={att} />
    </div>
  )
}

// Un'immagine (grafico prodotto dalla sandbox, foto allegata) si GUARDA nel
// thread: come chip di download sarebbe il risultato più importante
// dell'analisi nascosto dietro un click.
function AttachmentImage({ convId, att }) {
  const [url, setUrl] = useState(null)
  const [failed, setFailed] = useState(false)
  const { t } = useTranslation('chat')
  useEffect(() => {
    // la blob URL va revocata anche se il fetch finisce DOPO lo smontaggio
    // (cambio di conversazione a immagine in caricamento)
    let alive = true
    let current = null
    fetchAttachmentUrl(convId, att.id)
      .then((u) => {
        current = u
        if (alive) setUrl(u)
        else URL.revokeObjectURL(u)
      })
      .catch(() => { if (alive) setFailed(true) })
    return () => { alive = false; if (current) URL.revokeObjectURL(current) }
  }, [convId, att.id])
  if (failed) return <AttachmentChip convId={convId} att={att} />
  return (
    <figure className="flex flex-col gap-1">
      {url ? (
        <img src={url} alt={att.filename}
             className="max-h-[420px] max-w-full cursor-zoom-in rounded-md border border-border bg-card object-contain"
             onClick={() => window.open(url, '_blank', 'noopener')} />
      ) : (
        <div className="flex h-24 items-center justify-center rounded-md border border-border bg-muted/40">
          <Loader2 className="size-4 animate-spin text-muted-foreground" />
        </div>
      )}
      <figcaption className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
        <span className="truncate">{att.filename} · {fmtBytes(att.size)}</span>
        <button className="inline-flex items-center gap-1 hover:text-foreground"
                onClick={() => downloadFile(api.chatAttachmentUrl(convId, att.id), att.filename)
                  .catch((e) => toast.error(e.message))}>
          <FileDown className="size-3" /> {t('attachment.downloadShort')}
        </button>
      </figcaption>
      <span className="text-[11px]"><AttachmentState att={att} /></span>
    </figure>
  )
}

// Gli allegati di un messaggio: immagini in evidenza, il resto come chip.
function Attachments({ convId, atts }) {
  if (!atts.length) return null
  const images = atts.filter(isImage)
  const files = atts.filter((a) => !isImage(a))
  return (
    // pb-1: gli allegati chiudono la bolla, e il padding verticale della
    // bolla (py-2.5) da solo lascia le card troppo attaccate al bordo
    <div className="flex flex-col gap-2 pb-1 pt-1">
      {images.map((a) => <AttachmentImage key={a.id} convId={convId} att={a} />)}
      {files.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {files.map((a) => <AttachmentChip key={a.id} convId={convId} att={a} />)}
        </div>
      )}
    </div>
  )
}

// I modelli inventano link ai percorsi interni della sandbox
// ("[file.xlsx](computer:///workspace/outputs/file.xlsx)", "sandbox:/..."):
// non sono cliccabili né sensati nel browser — resta solo il nome, il
// download vero è il chip sotto il messaggio.
function stripSandboxLinks(text) {
  return text
    .replace(/\[([^\[\]]+)\]\((?:[a-z][a-z0-9+.-]*:\/*)?\/?(?:workspace|mnt)\/[^)]*\)/gi, '$1')
    .replace(/\[([^\[\]]+)\]\((?:sandbox|attachment|computer):[^)]*\)/gi, '$1')
}

// Copia il SOLO testo del messaggio: niente allegati, niente meta (modello,
// costo, tempi). Sta nell'ultima riga della bolla, quella delle info di
// servizio, a destra: così non si confonde con i bottoni dei blocchi di codice.
function CopyMessage({ text, className }) {
  const { t } = useTranslation('chat')
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    } catch { toast.error(t('message.copyFailed')) }
  }
  return (
    <button type="button" onClick={copy}
            title={t(copied ? 'message.copied' : 'message.copy')}
            aria-label={t('message.copy')}
            className={cn('touch-control inline-flex size-5 shrink-0 cursor-pointer items-center justify-center rounded',
                          'opacity-70 transition-opacity hover:opacity-100 focus-visible:opacity-100',
                          className)}>
      {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
    </button>
  )
}

// Un messaggio del thread (utente o assistant). Gli step tool sono inline nel
// flusso dell'assistant, nell'ordine in cui sono avvenuti.
function Message({ msg, convId, attachments, streaming = false }) {
  const { t } = useTranslation('chat')
  const isUser = msg.role === 'user'
  const all = attachments || []
  // gli allegati sono legati al TURNO da message_id: gli upload alla bolla
  // dell'utente che li ha inviati, gli artifact all'assistant che li ha
  // prodotti. Durante lo streaming il message_id non esiste ancora, quindi gli
  // artifact del messaggio in corso arrivano dagli step.
  const own = all.filter((a) => a.message_id === msg.id)
  const inAtts = own.filter((a) => a.direction === 'in')
  // lo stesso artifact può comparire in più step (file riscritto nel turno,
  // stessa riga riusata dal backend): una card sola, col descrittore più recente
  const fromSteps = new Map((msg.steps || [])
    .flatMap((s) => s.attachments || []).map((a) => [a.id, a]))
  const seen = new Set(own.map((a) => a.id))
  const outAtts = [...own.filter((a) => a.direction === 'out'),
                   ...[...fromSteps.values()].filter((a) => !seen.has(a.id))]
  const usage = msg.usage
  // tempo di risposta: dal primo segno di attività del modello alla fine del
  // turno; assente sui messaggi per cui non è stato misurato
  const respMs = usage?.response_ms ?? usage?.elapsed_ms
  return (
    <div className={cn('flex', isUser ? 'justify-end' : 'justify-start')}>
      {/* la bolla assistant va a larghezza PIENA (w-full fino al max-w)
          appena arriva qualcosa (ragionamento, testo, step): da lì il
          contenuto cambia di continuo e una bolla che si allarga e si
          restringe balla. Finché è solo "in attesa del modello" resta
          compatta. */}
      {/* break-words: una "parola" senza spazi (JWT, URL, hash, chiave in
          una riga) non ha punti di rottura e uscirebbe dalla bolla; qui si
          spezza dove serve. Si eredita da tutto il contenuto; i blocchi di
          codice (white-space: pre) non ne sono toccati e scrollano in
          orizzontale come prima. */}
      <div className={cn('flex min-w-0 max-w-[95%] sm:max-w-[85%] flex-col gap-2 break-words rounded-2xl px-4 py-2.5',
                         isUser ? 'bg-primary text-primary-foreground'
                                : 'bg-card border border-border',
                         !isUser && (msg.content || msg.reasoning || msg.error
                                     || (msg.steps || []).length > 0)
                           && 'w-full')}>
        {msg.reasoning && !isUser && (
          // finché non arriva testo il ragionamento è ancora quello "vivo"
          <Reasoning text={msg.reasoning} live={streaming && !msg.content} />
        )}
        {(msg.steps || []).map((s, i) => <ToolStep key={i} step={s} />)}
        {isUser
          ? <div className="whitespace-pre-wrap text-sm"><Linkify text={msg.content} /></div>
          : msg.content && <Markdown
              text={msg.entities?.length ? msg.content : stripSandboxLinks(msg.content)}
              entities={msg.entities} codeValues={msg.code_values} />}
        {/* turno fermato col bottone stop: senza questa riga la bolla resta
            identica a una risposta mai arrivata (il modello non ha scritto
            niente dopo l'annullamento) e sembra un guasto */}
        {!isUser && msg.finish_reason === 'canceled' && (
          <div className="flex items-center gap-1.5 text-xs text-amber-700 dark:text-amber-400">
            <Square className="size-3" /> {t('message.canceled')}
          </div>
        )}
        {msg.error && (
          <div className="flex items-center gap-1.5 text-xs text-destructive">
            <AlertCircle className="size-3.5" /> {msg.error}
          </div>
        )}
        {inAtts.length > 0 && <Attachments convId={convId} atts={inAtts} />}
        {outAtts.length > 0 && <Attachments convId={convId} atts={outAtts} />}
        {/* ultima riga della bolla (e quindi del thread): finché c'è, il
            modello non ha finito */}
        {streaming && (
          <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
            <TypingDots />
            <span>{liveLabel(msg, t)}</span>
          </div>
        )}
        {/* ultima riga, di servizio: etichetta anonimizzazione, modello/costo/
            tempi e l'icona copia. Solo a turno concluso: durante lo stream il
            testo cambia ancora e l'ultima riga è l'indicatore qui sopra. */}
        {!streaming && (msg.anonymized != null || (usage && !isUser) || msg.content) && (
          <div className="flex items-center justify-between gap-2 text-[11px]">
            <div className={cn('flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5',
                               isUser ? 'opacity-80' : 'text-muted-foreground')}>
              {msg.anonymized != null && (
                <span className={cn(
                  'inline-flex items-center gap-1',
                  !isUser && (msg.anonymized ? 'text-emerald-700 dark:text-emerald-400'
                                             : 'text-amber-700 dark:text-amber-400'))}>
                  {msg.anonymized ? <ShieldCheck className="size-3" />
                                  : <ShieldAlert className="size-3" />}
                  {t(msg.anonymized ? 'message.anonymized' : 'message.plain')}
                </span>
              )}
              {usage && !isUser && (
                <span title={respMs != null ? t('message.timeTitle', {
                        response: fmtDuration(respMs),
                        total: fmtDuration(usage.elapsed_ms ?? respMs),
                      }) : undefined}>
                  {msg.model || ''}
                  {usage.cost ? ` · ${fmtCost(usage.cost)}` : ''}
                  {usage.prompt_tokens
                    ? t('message.tokens', {
                        count: usage.prompt_tokens + (usage.completion_tokens || 0) })
                    : ''}
                  {respMs != null ? ` · ${fmtDuration(respMs)}` : ''}
                </span>
              )}
            </div>
            {msg.content && (
              <CopyMessage text={msg.content}
                           className={isUser ? 'hover:bg-primary-foreground/15'
                                             : 'text-muted-foreground hover:bg-accent'} />
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// riduce i messaggi persistiti (con tool_calls / tool result separati) in
// bolle da mostrare: gli assistant assorbono gli step tool che li seguono
const KIND_BY_TOOL = { execute_python: 'code', web_search: 'search', read_page: 'page', read_document_images: 'ocr' }

function foldMessages(messages) {
  const out = []
  for (const m of messages) {
    if (m.role === 'tool') continue      // gli step arrivano dal tool_call
    if (m.role === 'assistant') {
      const steps = (m.tool_calls || []).map((tc) => ({
        id: tc.id,
        kind: KIND_BY_TOOL[tc.function?.name] || 'code',
        code: safeArgs(tc.function?.arguments).code || '',
        // display_args = argomenti decodificati dal server (la query reale);
        // dove non ci sono (chat normale) valgono quelli canonici
        args: tc.display_args || safeArgs(tc.function?.arguments),
        result: { outcome: 'ok', elapsed_ms: 0 }, // gli esiti storici non si ri-mostrano espansi
      }))
      out.push({ ...m, steps })
    } else {
      out.push(m)
    }
  }
  return out
}

function safeArgs(argsJson) {
  try {
    const parsed = JSON.parse(argsJson || '{}')
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch { return {} }
}

// Avviso privacy in alto: un modello senza provider Zero Data Retention non è
// utilizzabile con le regole che imponiamo su ogni richiesta (arriverebbe un
// 503). La deroga vale per la SINGOLA conversazione ed esiste solo se
// l'amministratore l'ha abilitata per l'installazione (Impostazioni → Chat
// LLM); finché è attiva resta dichiarata, perché in quel periodo il
// provider può conservare i dati e usarli per addestrare.
function PrivacyNotice({ model, relaxed, allowed, isAdmin, onChange }) {
  const { t } = useTranslation('chat')
  const blocked = Array.isArray(model?.zdr_providers)
                  && model.zdr_providers.length === 0
  if (!blocked && !relaxed) return null
  if (relaxed) {
    return (
      <div className="flex items-start gap-2 border-b border-border bg-amber-500/10 px-4 py-2 text-sm text-amber-700 dark:text-amber-400">
        <ShieldOff className="mt-0.5 size-4 shrink-0" />
        <span className="flex-1">{t('privacy.relaxed')}</span>
        <Button variant="outline" size="sm" className="h-6 shrink-0 text-xs"
                onClick={() => onChange(false)}>
          {t('privacy.restore')}
        </Button>
      </div>
    )
  }
  return (
    <div className="flex items-start gap-2 border-b border-border bg-destructive/10 px-4 py-2 text-sm text-destructive">
      <ShieldOff className="mt-0.5 size-4 shrink-0" />
      <span className="flex-1">
        <Trans i18nKey="privacy.blocked" ns="chat"
               values={{ model: model.name }} components={{ b: <b /> }} />
        {t(allowed ? 'privacy.blockedAllowed'
                   : isAdmin ? 'privacy.blockedAdmin' : 'privacy.blockedUser')}
      </span>
      {allowed && (
        <Button variant="outline" size="sm" className="h-6 shrink-0 text-xs"
                onClick={() => onChange(true)}>
          {t('privacy.useAnyway')}
        </Button>
      )}
    </div>
  )
}

// Controlli di testata con pannellino al click:
// per contesto e costo la spiegazione è troppo lunga per un tooltip.
function InfoPill({ className, detail, children }) {
  const [open, setOpen] = useState(false)
  const boxRef = useRef(null)
  return (
    <div className="relative shrink-0" ref={boxRef}>
      <Button type="button" variant="outline" size="sm"
              aria-label={detail} aria-expanded={open}
              onClick={() => setOpen((o) => !o)}
              className={cn('min-w-11 px-2 tabular-nums', className)}>
        {children}
      </Button>
      {open && (
        <FloatingPanel anchorRef={boxRef} align="end" onClose={() => setOpen(false)} aria-label={detail}
                       className="w-[280px] rounded-lg border border-border bg-popover px-3 py-2 text-[11px] leading-snug text-muted-foreground shadow-lg">
          {detail}
        </FloatingPanel>
      )}
    </div>
  )
}

// I guasti d'ambiente (sandbox giù, browser web giù) condensati in un unico
// pill rosso «N errori»: al click un pannello — stesso stile del popup delle
// opzioni modello — elenca ogni problema con la sua spiegazione.
function StatusErrors({ problems }) {
  const [open, setOpen] = useState(false)
  const boxRef = useRef(null)
  const { t } = useTranslation('chat')
  if (!problems.length) return null
  return (
    <div className="relative shrink-0" ref={boxRef}>
      <button type="button" onClick={() => setOpen((o) => !o)}
              aria-expanded={open}
              title={t('banner.errorsHint')}
              className="inline-flex cursor-pointer items-center gap-1 rounded-full border border-destructive/40 bg-card px-2 py-0.5 text-xs text-destructive hover:bg-destructive/10">
        <AlertCircle className="size-3" />
        {t('banner.errors', { count: problems.length })}
      </button>
      {open && (
        <FloatingPanel anchorRef={boxRef} align="end" onClose={() => setOpen(false)} aria-label={t('banner.errorsHint')}
                       className="w-[320px] rounded-lg border border-border bg-popover shadow-lg">
          {problems.map(({ key, Icon, title, detail }) => (
            <div key={key} className="flex flex-col gap-1 border-b border-border px-3 py-2 last:border-b-0">
              <span className="inline-flex items-center gap-1.5 text-xs font-medium text-destructive">
                <Icon className="size-3.5 shrink-0" /> {title}
              </span>
              <span className="text-[11px] leading-snug text-muted-foreground">
                {detail}
              </span>
            </div>
          ))}
        </FloatingPanel>
      )}
    </div>
  )
}

// Avanzamento dell'anonimizzazione del turno: quanti pezzi, a che punto è
// quello in corso. Gli snapshot hanno la stessa forma di quelli delle card
// dei job (jobs.jsx: fase x di y + done/total), qui con in più "pezzo i di n".
function anonStatusText(p, t) {
  if (!p) return t('anonProgress.preparing')
  const stage = t(`anonProgress.stage.${p.stage === 'redact' ? 'redact'
    : p.stage === 'preview' ? 'preview' : 'analyze'}`)
  // `p.phase` è la CHIAVE di fase del worker (engine/progress.py, PHASES):
  // il nome leggibile sta nel catalogo dei job, che le raccoglie tutte
  const phase = p.phase
    ? t(`phase.${p.phase}`, { ns: 'jobs', defaultValue: p.phase }) : ''
  let s = p.phase_index && p.phase_total
    ? t('anonProgress.phaseOf', { stage, index: p.phase_index,
                                  total: p.phase_total, phase })
    : phase ? t('anonProgress.phaseAlone', { stage, phase })
            : t('anonProgress.stageAlone', { stage })
  if (p.units_done != null && p.units_total) s += ` (${p.units_done}/${p.units_total})`
  return s
}

function anonPercent(p) {
  if (!p || !p.phase_index || !p.phase_total) return null
  const frac = p.units_total ? Math.min(1, (p.units_done ?? 0) / p.units_total) : 0
  return Math.round(100 * ((p.phase_index - 1) + frac) / p.phase_total)
}

function AnonProgress({ state }) {
  const { t } = useTranslation('chat')
  const pieces = [...(state.files || []).map((f) => f.filename),
                  t('anonProgress.yourMessage')]
  const p = state.progress
  const current = p?.index ?? 1
  const pct = anonPercent(p)
  return (
    <div className="flex justify-start">
      <div className="flex min-w-0 max-w-[95%] sm:max-w-[85%] flex-col gap-2 rounded-2xl border border-emerald-500/40 bg-emerald-500/5 px-4 py-3">
        <span className="inline-flex items-center gap-2 text-sm font-medium text-emerald-700 dark:text-emerald-400">
          <Lock className="size-4" />
          {t('anonProgress.heading', { current, total: pieces.length })}
        </span>
        <ul className="flex flex-col gap-1 text-xs">
          {pieces.map((name, i) => {
            const n = i + 1
            const done = n < current || (state.done && true)
            return (
              <li key={name + n} className="flex items-center gap-2">
                {done
                  ? <Check className="size-3 shrink-0 text-emerald-600" />
                  : n === current
                    ? <Loader2 className="size-3 shrink-0 animate-spin text-primary" />
                    : <span className="size-3 shrink-0" />}
                <span className={cn('min-w-0 flex-1 truncate',
                                    n === current ? 'text-foreground'
                                                  : 'text-muted-foreground')}>
                  {name}
                </span>
                {n === current && !state.done && (
                  <span className="shrink-0 text-muted-foreground">
                    {anonStatusText(p, t)}
                  </span>
                )}
              </li>
            )
          })}
        </ul>
        <div className="h-1 overflow-hidden rounded-full bg-primary/15">
          <div className={cn('h-full rounded-full bg-primary transition-[width] duration-300',
                             pct == null && 'progress-indeterminate')}
               style={pct == null ? undefined : { width: pct + '%' }} />
        </div>
        <span className="text-[11px] text-muted-foreground">
          {t('anonProgress.footer')}
        </span>
      </div>
    </div>
  )
}

// La schermata della chat. Sopra ci sono i pezzi che compongono il thread
// (bolle, step tool, allegati, pill di testata); qui sotto la macchina che
// governa il turno, che è la parte complicata dell'app.
//
// Un turno NON è una richiesta: vive sul SERVER. Il componente lo segue con
// uno stream aperto a mano (fetch + reader, non EventSource: serve un POST
// con body), e se la pagina si ricarica o si cambia scheda ci si RIAGGANCIA
// — il server rigioca gli eventi dall'inizio e poi prosegue in diretta
// (`attachLive`). Il turno non si perde perché il browser se ne va.
//
// Prima che esca un solo byte il turno può fermarsi a dei cancelli, e ognuno
// è un pezzo di stato qui dentro: `askUnconfirmed` (file di progetto che il
// modello non vedrebbe), `askOcr` (immagini negli allegati, da leggere o no),
// `mergeCheck` (fusioni del registro da confermare), `staged` (l'anteprima
// «anonimizza e mostra prima di inviare»). Finché uno di questi è aperto,
// verso OpenRouter non è partito niente.
export default function ChatPage() {
  const { user } = useAuth()
  // /projects/:projectId/chat: le chat DEL PROGETTO (modo ereditato, registro
  // e file condivisi), con l'elenco in un aside come sempre;
  // /chats/:chatId: una chat LIBERA — l'elenco sta nella sidebar principale
  // (DashboardLayout) e la nuova chat nasce dalla pagina /new
  const { projectId, chatId } = useParams()
  const compact = useMediaQuery('(max-width: 639px)')
  const navigate = useNavigate()
  const { refresh: refreshFreeChats } = useChats()
  const [searchParams, setSearchParams] = useSearchParams()
  const [project, setProject] = useState(null)
  const [chats, setChats] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [conv, setConv] = useState(null)      // {messages, attachments, model, ...}
  const [models, setModels] = useState([])
  // la white list dell'admin per CHI è loggato (null = tutti): il selettore
  // mostra solo quelli, l'avviso sotto la testata segnala una chat rimasta
  // su un modello che non c'è più
  const [allowedIds, setAllowedIds] = useState(null)
  const [status, setStatus] = useState(null)  // {configured, sandbox}
  const [tags, setTags] = useState([])        // tag rilevabili (dal modello PII)
  const [tagGroups, setTagGroups] = useState(null)  // {cyber: [...]}: la voce «Cybersecurity» del pannello
  const [input, setInput] = useState('')
  const taRef = useRef(null)      // la textarea del composer (auto-altezza)
  const [streaming, setStreaming] = useState(false)
  // il bottone stop prende il POSTO del bottone invia: il primo secondo resta
  // disabilitato, o un doppio click sull'invio annulla il turno appena partito
  const [stopLocked, setStopLocked] = useState(false)
  // stop richiesto, done non ancora arrivato: spinner e scritta "interruzione"
  const [stopping, setStopping] = useState(false)
  const [live, setLive] = useState(null)      // messaggio assistant in costruzione
  // Coda degli allegati in viaggio: [{key, convId, name, size, active}]. Si
  // sceglie anche più di un file alla volta, ma verso il server partono UNO
  // ALLA VOLTA, con la stessa richiesta di sempre. Il server risponde solo
  // dopo aver salvato e schedato il file (briefing + conteggio immagini): su
  // un file pesante sono secondi, e i chip li devono occupare.
  const [uploads, setUploads] = useState([])
  const [anon, setAnon] = useState(null)      // avanzamento anonimizzazione
  const [turnError, setTurnError] = useState('')
  // avviso "file da confermare" prima di aprire una chat del progetto:
  // null | [file] (l'elenco che il modello NON vedrebbe)
  const [askUnconfirmed, setAskUnconfirmed] = useState(null)
  // anteprima pre-invio: null | {items, content, ...} (modal aperto)
  const [staged, setStaged] = useState(null)
  // popup OCR pre-invio: null | {content} — gli allegati del turno contengono
  // immagini e l'utente deve scegliere se leggerle con l'OCR
  const [askOcr, setAskOcr] = useState(null)
  // turno FERMO in attesa che si confermino le fusioni del registro:
  // null | {suggestions} — niente è ancora partito verso il modello, e la
  // scelta entra nel system prompt di questo stesso messaggio
  const [mergeCheck, setMergeCheck] = useState(null)
  // "anonimizza e mostra prima di inviare" — preferenza dell'utente,
  // ricordata tra le sessioni; di default ATTIVA: chi non ha mai scelto vede
  // l'anteprima, e la spegne solo se lo decide
  const [reviewBefore, setReviewBefore] = useState(
    () => localStorage.getItem('chat-review-before-send') !== '0')
  const [atBottom, setAtBottom] = useState(true)
  const abortRef = useRef(null)
  // la conversazione aperta ADESSO: le callback asincrone (eventi dello
  // stream, risposte in ritardo) la confrontano per non applicare stato di
  // una chat che nel frattempo è stata cambiata
  const activeIdRef = useRef(null)
  // setSearchParams di react-router non è stabile tra i render: chi la usa
  // da una closure vecchia (loadConv) passa dalla ref
  const setSearchParamsRef = useRef(setSearchParams)
  useEffect(() => { setSearchParamsRef.current = setSearchParams })
  const scrollRef = useRef(null)
  const atBottomRef = useRef(true)   // letto dentro l'effetto: niente re-render per token
  const fileRef = useRef(null)
  // la coda vera: i File non stanno nello stato (al render non servono) e
  // uploadRunning è la sentinella dell'unico lavoratore che la svuota
  const uploadQueue = useRef([])
  const uploadRunning = useRef(false)
  const uploadSeq = useRef(0)
  // file trascinati dal PC sopra il thread: l'overlay «rilascia qui» si vede
  // finché il puntatore sta sul thread. Il contatore serve perché il browser
  // manda un dragleave a ogni figlio attraversato: si spegne solo quando si
  // esce davvero dal thread, non passando da una bolla all'altra.
  const [dragOver, setDragOver] = useState(false)
  const dragDepth = useRef(0)
  // la rielaborazione con OCR di un allegato dell'anteprima è un JOB, come i
  // caricamenti dei progetti: la card sta nella sidebar e a fine lavoro
  // docRefresh dice quale allegato è cambiato
  const { addJob, docRefresh } = useJobs()
  const { t, i18n } = useTranslation('chat')
  // conteggi per esteso nei pannellini: col separatore delle migliaia della
  // lingua dell'interfaccia ("3.138" in it, "3,138" in en). useGrouping
  // 'always' perché il CLDR italiano non separa i numeri a 4 cifre.
  const fmtInt = (n) => (n ?? 0).toLocaleString(i18n.language,
                                                { useGrouping: 'always' })
  usePageTitle(t('title'))

  // Il registro su cui agiscono la revisione e il dialog delle fusioni: in un
  // progetto è quello del progetto, ma lo scope lo risolve il server (gli
  // endpoint della chat lavorano già sul registro condiviso).
  const registry = useMemo(() => api.chatRegistry(activeId), [activeId])

  // ricarica lo stato dell'anteprima dal server (il modal resta dov'è):
  // serve dopo una rielaborazione con OCR, che gira in un worker
  const refreshStaged = useCallback(() => (
    api.getStaged(activeId)
      .then((s) => { setStaged((cur) => (cur ? { ...cur, ...s } : cur)); return s })
      .catch(() => null)
  ), [activeId])

  // Servizi del visualizzatore sul turno in ANTEPRIMA: stessi contratti degli
  // endpoint dei file di progetto; le modifiche rispondono con TUTTI gli item
  // (la mappa è condivisa).
  const stagedServices = useMemo(() => ({
    fetchPagePng: (itemId, source, n, rev, signal) =>
      fetchStagedPagePng(activeId, itemId, source, n, rev, signal),
    extractText: (itemId, source, page, rect) =>
      api.stagedExtractText(activeId, itemId, source, page, rect),
    deanonymize: (_itemId, body) => api.stagedDeanonymize(activeId, body),
    anonymizeText: (_itemId, text, saveTerm, termTag) =>
      api.stagedAnonymizeText(activeId, text, saveTerm, termTag),
    // sigilli (solo allegati PDF/immagine): il viewer mostra da solo il
    // bottone quando il servizio c'è e il formato è sigillabile
    sealArea: (itemId, page, rect, allPages) =>
      api.stagedSealArea(activeId, itemId, page, rect, allPages),
    removeSeal: (itemId, n) => api.stagedRemoveSeal(activeId, itemId, n),
    // colonne dei fogli di calcolo: qui sono SINCRONE (rispondono col nuovo
    // stato dell'anteprima), non job come nei file di progetto
    getColumns: (itemId) => api.stagedGetColumns(activeId, itemId),
    columnInfo: (itemId, sheet, column) =>
      api.stagedColumnInfo(activeId, itemId, sheet, column),
    anonymizeColumn: (itemId, sheet, column) =>
      api.stagedAnonymizeColumn(activeId, itemId, sheet, column),
    deanonymizeColumn: (itemId, sheet, column) =>
      api.stagedDeanonymizeColumn(activeId, itemId, sheet, column),
    // rilettura con OCR: job in coda. Si ricarica subito lo stato per far
    // comparire l'avviso "elaborazione in corso" sul pezzo giusto
    reprocessOcr: (itemId) => api.stagedReprocessOcr(activeId, itemId)
      .then((job) => { refreshStaged(); return job }),
  }), [activeId, refreshStaged])

  // job concluso su un allegato dell'anteprima: il turno è stato ri-redatto
  // dal worker, il modal deve rileggerlo
  useEffect(() => {
    if (docRefresh.n > 0 && activeId
        && (staged?.items || []).some((i) => i.id === docRefresh.docId)) {
      refreshStaged()
    }
  }, [docRefresh])           // eslint-disable-line react-hooks/exhaustive-deps

  // l'elenco da tenere aggiornato (il titolo nasce col primo turno): quello
  // del progetto sta in questa pagina, quello delle chat libere nella sidebar
  const refreshChats = useCallback(() => {
    if (projectId) api.listChats(projectId).then(setChats).catch(() => {})
    else refreshFreeChats()
  }, [projectId, refreshFreeChats])

  useEffect(() => {
    api.orStatus().then(setStatus).catch(() => {})
    api.orModels().then((r) => {
      setModels(r.models)
      setAllowedIds(r.allowed ?? null)
    }).catch(() => {})
    // le categorie che il pannello mostra: derivate dal config del modello PII
    api.getTags().then((r) => { setTags(r.all); setTagGroups(r.groups || null) }).catch(() => {})
  }, [])

  // contesto progetto: lista delle chat del progetto, selezione azzerata;
  // ?chat=<id> (scritto da loadConv, o dal link della pagina del progetto)
  // riapre quella chat — è ciò che rende il refresh trasparente
  useEffect(() => {
    activeIdRef.current = null
    setActiveId(null)
    setConv(null)
    setProject(null)
    if (!projectId) return   // chat libera: la conversazione arriva dall'URL
    api.getProject(projectId).then(setProject).catch(() => {})
    api.listChats(projectId).then((cs) => {
      setChats(cs)
      const want = searchParams.get('chat')
      if (want && cs.some((c) => c.id === want)) loadConv(want)
    }).catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  // chat libera: l'id sta nel percorso (/chats/:chatId), scelto dalla sidebar
  useEffect(() => {
    if (!projectId && chatId) loadConv(chatId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, chatId])

  // allo smontaggio si chiude solo la LETTURA dello stream: il turno
  // continua sul server e al ritorno ci si riaggancia
  useEffect(() => () => abortRef.current?.(), [])

  // finestra di grazia del bottone stop (vedi stopLocked)
  useEffect(() => {
    if (!streaming) return undefined
    setStopLocked(true)
    const timer = setTimeout(() => setStopLocked(false), 1000)
    return () => { clearTimeout(timer); setStopLocked(false) }
  }, [streaming])

  function loadConv(id) {
    abortRef.current?.()      // lettore della chat precedente: solo lettura,
    abortRef.current = null   // il suo turno (se c'è) continua da solo
    activeIdRef.current = id
    setActiveId(id)
    setLive(null)
    setAnon(null)
    setStaged(null)
    setMergeCheck(null)
    setStreaming(false)
    setStopping(false)
    setTurnError('')
    // solo nel progetto: la chat aperta vive in ?chat= (nelle chat libere
    // sta già nel percorso, scritto dalla sidebar)
    if (projectId) setSearchParamsRef.current({ chat: id }, { replace: true })
    api.getChat(id).then((c) => {
      if (activeIdRef.current !== id) return
      setConv({ ...c, folded: foldMessages(c.messages) })
      if (c.busy) {
        // c'è un turno in corso (refresh a metà risposta, o un'altra
        // scheda): ci si riaggancia e si ricostruisce quel che mostrava
        setStreaming(true)
        attachLive(id)
      } else if (c.anonymized) {
        // niente turno, ma può esserci un'anteprima pre-invio preparata e
        // mai chiusa (refresh col modal aperto): si riapre da dove era
        api.getStaged(id).then((s) => {
          if (activeIdRef.current !== id) return
          setStaged(s)              // il descrittore porta già `content`
        }).catch(() => {})          // 404 = nessuna anteprima, e va bene
      }
    }).catch((e) => {
      toast.error(e.message)
      // chat libera che non esiste (più): si torna alla scelta della nuova
      if (!projectId && activeIdRef.current === id) navigate('/new', { replace: true })
    })
  }

  // "in fondo" con un margine di tolleranza: il pixel esatto non si becca mai
  // (zoom, altezze frazionarie) e basterebbe un pelo per bloccare l'autoscroll.
  const BOTTOM_SLACK = 64
  // ~8 righe di testo: oltre, la barra mangerebbe il thread
  const COMPOSER_MAX_PX = 192

  function onThreadScroll() {
    const el = scrollRef.current
    if (!el) return
    const bottom = el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_SLACK
    atBottomRef.current = bottom
    setAtBottom((cur) => (cur === bottom ? cur : bottom))
  }

  // salto immediato, non 'smooth': durante l'animazione partirebbero eventi di
  // scroll ancora "non in fondo" e il pill lampeggerebbe fino alla fine
  function scrollToBottom() {
    const el = scrollRef.current
    if (!el) return
    atBottomRef.current = true
    setAtBottom(true)
    el.scrollTop = el.scrollHeight
  }

  // Autoscroll SOLO se l'utente è già in fondo: mentre rilegge il parziale
  // scrollando indietro, lo stream non deve strapparlo di nuovo giù.
  useEffect(() => {
    const el = scrollRef.current
    if (el && atBottomRef.current) el.scrollTop = el.scrollHeight
  }, [conv?.folded, live, anon, streaming])

  // Il riquadro del thread cambia altezza anche senza che nessuno scrolli:
  // la barra che cresce col testo, il pannello allegati, un errore. Il browser
  // tiene fermo scrollTop, quindi le ultime righe finirebbero sotto la barra.
  // Se l'utente era in fondo, in fondo resta.
  useEffect(() => {
    const el = scrollRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => {
      if (atBottomRef.current) el.scrollTop = el.scrollHeight
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [conv?.id])

  // La barra cresce col testo (Shift+Invio, incolla lungo) fino a
  // COMPOSER_MAX_PX, poi scrolla al suo interno. Sta nel flusso: il thread
  // sopra si accorcia, e il ResizeObserver qui sopra lo tiene in fondo.
  useLayoutEffect(() => {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    const border = ta.offsetHeight - ta.clientHeight   // box-sizing: border-box
    const wanted = ta.scrollHeight + border
    ta.style.height = `${Math.min(wanted, COMPOSER_MAX_PX)}px`
    ta.style.overflowY = wanted > COMPOSER_MAX_PX ? 'auto' : 'hidden'
  }, [input, conv?.id])

  // Cambio conversazione: si riparte sempre dal fondo, qualunque fosse la
  // posizione nella chat precedente (l'effetto sopra è già passato, quindi
  // questo ha l'ultima parola).
  useEffect(() => {
    if (conv?.id) scrollToBottom()
  }, [conv?.id])

  // solo nel progetto: il modo è quello del progetto, niente da chiedere
  // (le chat libere nascono dalla pagina /new, dove la scelta è la pagina)
  async function newChat() {
    // i file non confermati non arrivano al modello: si avvisa prima di
    // aprire la chat (stato riletto adesso, può essere cambiato altrove)
    const p = await api.getProject(projectId).catch(() => project)
    if (p) setProject(p)
    const pending = p?.anonymized
      ? (p.files || []).filter((f) => !f.confirmed && !f.busy) : []
    if (pending.length && !p.skip_unconfirmed_warning) {
      setAskUnconfirmed(pending)
      return undefined
    }
    return createChat(false)
  }

  async function skipUnconfirmedWarning() {
    setProject((cur) => (cur ? { ...cur, skip_unconfirmed_warning: true } : cur))
    try {
      await api.patchProject(projectId, { skip_unconfirmed_warning: true })
    } catch (e) {
      toast.error(e.message)
    }
  }

  async function createChat(anonymized) {
    try {
      const c = await api.createChat('', anonymized, projectId || null)
      setChats((cs) => [c, ...cs])
      loadConv(c.id)
    } catch (e) { toast.error(e.message) }
  }

  async function removeChat(id, e) {
    e.stopPropagation()
    try {
      await api.deleteChat(id)
      setChats((cs) => cs.filter((c) => c.id !== id))
      if (id === activeId) {
        activeIdRef.current = null
        setActiveId(null)
        setConv(null)
        setSearchParams({}, { replace: true })  // ?chat= di una chat eliminata
      }
    } catch (err) { toast.error(err.message) }
  }

  async function setModel(model) {
    setConv((c) => ({ ...c, model }))
    try { await api.patchChat(activeId, { model }) } catch (e) { toast.error(e.message) }
  }

  async function setOptions(options) {
    setConv((c) => ({ ...c, options }))
    try { await api.patchChat(activeId, { options }) } catch (e) { toast.error(e.message) }
  }

  // Categorie da anonimizzare in questa conversazione: `excluded` null torna a
  // seguire i default dell'amministratore. La risposta del server ha l'ultima
  // parola (normalizza i tag e dice se l'override esiste ancora).
  async function setAnonTags(excluded) {
    try {
      const c = await api.patchChat(activeId, {
        anon_options: { excluded_tags: excluded },
      })
      setConv((cur) => ({ ...cur, anon_options: c.anon_options }))
    } catch (e) { toast.error(e.message) }
  }

  // Il selettore accetta più file in un colpo solo: qui si accodano e basta.
  // Chi li carica è pumpUploads, che nel frattempo può già essere in giro.
  function onPickFiles(e) {
    const files = Array.from(e.target.files || [])
    e.target.value = ''
    enqueueFiles(files)
  }

  // Incolla nella barra con dei file negli appunti (uno screenshot, un file
  // copiato dal file manager): diventano allegati, come dal selettore. Se
  // negli appunti c'è ANCHE del testo vince il testo e i file si ignorano:
  // una selezione da Excel o da una pagina web porta testo + immagine, e chi
  // la incolla vuole la tabella, non uno screenshot.
  function onPaste(e) {
    const dt = e.clipboardData
    const files = Array.from(dt?.files || [])
    if (!files.length || dt.getData('text/plain')) return
    e.preventDefault()
    // il browser chiama ogni immagine incollata "image.png": un nome con
    // l'ora distingue i chip e i file scaricati dopo
    const stamp = new Date().toISOString().slice(0, 19).replace(/[-:]/g, '').replace('T', '-')
    enqueueFiles(files.map((f, i) => {
      if (!/^image\.\w+$/i.test(f.name)) return f
      const ext = f.name.slice(f.name.lastIndexOf('.'))
      const n = files.length > 1 ? `-${i + 1}` : ''
      return new File([f], `screenshot-${stamp}${n}${ext}`, { type: f.type })
    }))
  }

  // Trascinare file dal PC sulla conversazione: diventano allegati come dal
  // selettore. Si reagisce solo a trascinamenti di FILE (non a testo o
  // immagini trascinate da dentro la pagina), e durante la risposta il
  // rilascio non fa niente, come il bottone della graffetta disabilitato.
  function dragHasFiles(e) {
    return Array.from(e.dataTransfer?.types || []).includes('Files')
  }
  function onDragEnter(e) {
    if (!dragHasFiles(e)) return
    e.preventDefault()
    dragDepth.current += 1
    setDragOver(true)
  }
  function onDragOver(e) {
    if (!dragHasFiles(e)) return
    // senza preventDefault il browser aprirebbe il file al rilascio
    e.preventDefault()
    e.dataTransfer.dropEffect = streaming ? 'none' : 'copy'
  }
  function onDragLeave(e) {
    if (!dragHasFiles(e)) return
    dragDepth.current = Math.max(0, dragDepth.current - 1)
    if (dragDepth.current === 0) setDragOver(false)
  }
  function onDrop(e) {
    if (!dragHasFiles(e)) return
    e.preventDefault()
    dragDepth.current = 0
    setDragOver(false)
    if (streaming) return
    // una cartella trascinata arriva come File vuoto senza tipo: si scarta
    // qui, invece di farla rifiutare dal server con un errore fuorviante
    const items = Array.from(e.dataTransfer.items || [])
    let files
    if (items.length) {
      files = items
        .filter((it) => it.kind === 'file' && !it.webkitGetAsEntry?.()?.isDirectory)
        .map((it) => it.getAsFile()).filter(Boolean)
      if (!files.length) { toast.error(t('composer.dropFolder')); return }
    } else {
      files = Array.from(e.dataTransfer.files || [])
    }
    enqueueFiles(files)
  }
  const dropProps = conv
    ? { onDragEnter, onDragOver, onDragLeave, onDrop } : {}

  function enqueueFiles(files) {
    const convId = activeIdRef.current
    if (!files.length || !convId) return
    const items = files.map((file) => ({
      key: `up-${++uploadSeq.current}`, convId, file,
      name: file.name, size: file.size,
    }))
    uploadQueue.current.push(...items)
    setUploads((u) => [...u, ...items.map(({ file, ...chip }) => chip)])
    pumpUploads()
  }

  // Svuota la coda un file alla volta: una richiesta per allegato, come
  // quando se ne poteva scegliere uno solo. Ne gira SEMPRE una copia sola,
  // così i file scelti mentre la coda scorre si accodano senza scavalcare.
  async function pumpUploads() {
    if (uploadRunning.current) return
    uploadRunning.current = true
    try {
      while (uploadQueue.current.length) {
        const it = uploadQueue.current.shift()
        setUploads((u) => u.map((x) => (x.key === it.key ? { ...x, active: true } : x)))
        try {
          const att = await api.uploadChatAttachment(it.convId, it.file)
          // la chat può essere cambiata mentre il file viaggiava: l'allegato
          // è sulla SUA conversazione, e lì deve comparire (non su questa)
          if (activeIdRef.current === it.convId) {
            setConv((c) => ({ ...c, attachments: [...(c.attachments || []), att] }))
          }
        } catch (err) {
          // il rifiuto per formato (415) è la spiegazione più utile che
          // l'utente riceva: va letta, non fatta sparire in un secondo.
          // Un file rifiutato non ferma quelli dopo di lui.
          toast.error(`${it.name}: ${err.message}`, { duration: 8000 })
        } finally {
          setUploads((u) => u.filter((x) => x.key !== it.key))
        }
      }
    } finally {
      uploadRunning.current = false
    }
  }

  async function removeAttachment(att) {
    try {
      await api.removeChatAttachment(activeId, att.id)
      setConv((c) => ({ ...c, attachments: c.attachments.filter((a) => a.id !== att.id) }))
    } catch (e) { toast.error(e.message) }
  }

  function send() {
    const content = input.trim()
    if (!content || streaming || !conv) return
    // allegati ancora in coda: partirebbe un turno senza i file scelti
    if (uploads.some((u) => u.convId === conv.id)) return
    if (!conv.model) { toast.error(t('error.pickModel')); return }
    // allegati con immagini in una chat anonimizzata: prima di partire si
    // chiede se leggerle con l'OCR (il popup richiama proceedSend)
    if (conv.anonymized && pending.some((a) => (a.n_images || 0) > 0)) {
      setAskOcr({ content })
      return
    }
    proceedSend(content, false)
  }

  function proceedSend(content, ocr) {
    setAskOcr(null)
    // "anonimizza e mostra prima di inviare": il turno viene solo PREPARATO
    // (staged) e si apre la revisione; l'invio vero parte dal modal
    if (conv.anonymized && reviewBefore) { stageSend(content, ocr); return }
    realSend(content, false, ocr)
  }

  // aggiorna i chip degli allegati con i descrittori post-anonimizzazione
  function applyStagedAttachments(payload) {
    if (!payload?.attachments?.length) return
    const byId = new Map(payload.attachments.map((a) => [a.id, a]))
    setConv((c) => (c ? {
      ...c,
      attachments: (c.attachments || []).map((a) => byId.get(a.id) || a),
    } : c))
  }

  // Gestore unico degli eventi SSE di un turno: lo usano l'invio normale
  // (realSend), la preparazione dell'anteprima (stageSend) e il riaggancio a
  // un turno già in corso dopo un refresh (attachLive) — il server rigioca
  // sempre gli eventi dall'inizio, quindi la grammatica è identica.
  // `attach` = si sta RICOSTRUENDO lo stato: la bolla utente non esiste
  // ancora e la crea l'evento `turn` (che porta il testo del messaggio).
  function makeTurnHandler({ convId, content = null, attach = false }) {
    let turnContent = content     // il testo da restituire alla casella se
    let turnAnonymized = null     // il turno fallisce prima di partire
    let tmpId = null
    return (ev) => {
      // eventi di una chat che non è più quella aperta: si ignorano (il
      // lettore viene comunque abortito al cambio di conversazione)
      if (activeIdRef.current !== convId) return
      if (ev.type === 'turn') {
        turnContent = ev.content
        turnAnonymized = !!ev.anonymized
        if (attach && !ev.preview) {
          // la bolla utente ottimistica che l'invio aveva messo e il refresh
          // ha buttato via: si ricrea identica (l'evento `start` più avanti
          // la sostituirà col messaggio persistito vero)
          tmpId = `tmp-${Date.now()}`
          setConv((c) => (c ? {
            ...c,
            attachments: (c.attachments || []).map((a) =>
              a.direction === 'in' && !a.message_id ? { ...a, message_id: tmpId } : a),
            folded: [...c.folded, { role: 'user', content: ev.content,
                                    id: tmpId, anonymized: ev.anonymized }],
          } : c))
        }
        return
      }
      if (ev.type === 'anon_start') {
        setAnon({ files: ev.files || [], progress: null })
        return
      }
      if (ev.type === 'anon_progress') { setAnon((s) => ({ ...(s || {}), progress: ev })); return }
      // il registro è appena nato e propone fusioni: il turno è fermo sul
      // server (niente è partito) finché non si risponde. Il testo viaggia
      // con lo stato: se si annulla, torna nella casella
      if (ev.type === 'merge_check') {
        setMergeCheck({ suggestions: ev.suggestions || [], content: turnContent })
        return
      }
      // anteprima pronta: niente è partito, si apre la revisione
      if (ev.type === 'staged') {
        setAnon(null)
        setStreaming(false)
        abortRef.current = null
        applyStagedAttachments(ev)
        setStaged({ ...ev, content: turnContent ?? ev.content })
        return
      }
      if (ev.type === 'anon_done') {
        setAnon(null)
        const byId = new Map((ev.attachments || []).map((a) => [a.id, a]))
        setConv((c) => (c ? {
          ...c,
          attachments: (c.attachments || []).map((a) => byId.get(a.id) || a),
        } : c))
        setLive({ role: 'assistant', content: '', entities: [], reasoning: '',
                  steps: [], error: null, anonymized: turnAnonymized })
        return
      }
      if (ev.type === 'error' && ev.fatal) { failTurn(ev.message, turnContent); return }
      if (ev.type === 'start' && attach && ev.user_message) {
        // il messaggio persistito prende il posto della bolla ottimistica —
        // o la elimina, se getChat l'aveva già consegnato nel thread
        // (riaggancio a risposta del modello già iniziata)
        setConv((c) => {
          if (!c) return c
          const um = ev.user_message
          const already = c.folded.some((m) => m.id === um.id)
          return {
            ...c,
            folded: c.folded
              .map((m) => (m.id === tmpId && !already ? um : m))
              .filter((m) => !(m.id === tmpId && already)),
            attachments: (c.attachments || []).map((a) =>
              (a.message_id === tmpId ? { ...a, message_id: um.id } : a)),
          }
        })
      }
      setLive((cur) => {
        const next = { ...(cur || { role: 'assistant', content: '', entities: [],
                                    reasoning: '', steps: [], error: null,
                                    anonymized: turnAnonymized }) }
        // Nei messaggi anonimizzati il decoder allega i soli valori incontrati
        // dentro codice/HTML/link. Arrivano insieme al testo anche durante lo
        // stream, così un placeholder completo non resta visibile fino al done.
        if (ev.code_values) {
          next.code_values = { ...(next.code_values || {}), ...ev.code_values }
        }
        if (ev.type === 'text') {
          const offset = next.content.length
          next.content += ev.delta
          next.entities = [...(next.entities || []), ...(ev.entities || []).map((entity) => ({
            ...entity, start: entity.start + offset, end: entity.end + offset,
          }))]
        }
        else if (ev.type === 'reasoning') next.reasoning += ev.delta
        else if (ev.type === 'tool_call') {
          next.steps = [...next.steps, {
            id: ev.id, kind: ev.kind, name: ev.name, code: ev.code,
            args: ev.display_args || ev.args, result: null,
          }]
        } else if (ev.type === 'tool_result') {
          next.steps = next.steps.map((s) =>
            s.id === ev.id ? { ...s, result: ev.result, attachments: ev.attachments } : s)
        } else if (ev.type === 'error') {
          next.error = ev.message
        } else if (ev.type === 'done') {
          // TAG rimasti dentro i blocchi di codice: servono al bottone
          // "copia con i valori reali", non al rendering
          if (ev.code_values) next.code_values = ev.code_values
          // il marcatore "turno interrotto" vale anche se la ricarica del
          // thread dovesse fallire: la bolla viva resta l'unica traccia
          if (ev.finish_reason) next.finish_reason = ev.finish_reason
        }
        return next
      })
      if (ev.type === 'done') finishTurn()
    }
  }

  // Riaggancio al turno in corso: il server rigioca gli eventi dall'inizio
  // (bolla utente compresa) e poi segue in diretta. Un errore di lettura si
  // ritenta una volta; se fallisce anche il retry (server riavviato: 404) si
  // torna allo stato persistito.
  function attachLive(id, retry = true) {
    // lo stato di un tentativo precedente si butta: il replay ricostruisce
    // tutto da solo, e sommare due replay duplicherebbe le bolle
    setLive(null)
    setAnon(null)
    setConv((c) => (c ? {
      ...c,
      folded: c.folded.filter((m) => !String(m.id).startsWith('tmp-')),
      attachments: (c.attachments || []).map((a) =>
        String(a.message_id || '').startsWith('tmp-')
          ? { ...a, message_id: null } : a),
    } : c))
    abortRef.current = attachChatStream(
      id, makeTurnHandler({ convId: id, attach: true }),
      () => {
        if (activeIdRef.current !== id) return
        if (retry) { attachLive(id, false); return }
        finishTurn()
      })
  }

  // anonimizza e mette in anteprima: NIENTE parte, niente bolla nel thread.
  // Gli eventi di avanzamento sono gli stessi dell'invio normale (il pannello
  // AnonProgress li racconta), più lo stage "preview" e l'evento finale
  // `staged` con tutti i pezzi da rivedere.
  function stageSend(content, ocr = false) {
    setTurnError('')
    setInput('')
    setStreaming(true)
    setAnon({ files: pending.map((a) => ({ id: a.id, filename: a.filename })), progress: null })
    scrollToBottom()
    abortRef.current = streamChatMessage(
      activeId, { content, preview: true, ocr },
      makeTurnHandler({ convId: activeId, content }),
      (err) => failTurn(err.message, content))
  }

  // l'utente ha rivisto (ed eventualmente ritoccato) l'anteprima: si invia
  // il turno già preparato, senza ri-anonimizzare
  function confirmStaged() {
    const content = staged.content
    setStaged(null)
    realSend(content, true)
  }

  async function cancelStaged() {
    const content = staged.content
    setStaged(null)
    try { await api.discardStaged(activeId) } catch { /* già scartata */ }
    // il testo torna nella casella: niente è stato inviato
    setInput((cur) => cur || content)
  }

  function realSend(content, fromStaged, ocr = false) {
    const anonymized = !!conv.anonymized

    setTurnError('')
    // bolla utente subito visibile; gli allegati non ancora inviati passano
    // sotto di essa (è quello che farà il server appena riceve il messaggio)
    const tmpId = `tmp-${Date.now()}`
    setConv((c) => ({
      ...c,
      attachments: (c.attachments || []).map((a) =>
        a.direction === 'in' && !a.message_id ? { ...a, message_id: tmpId } : a),
      folded: [...c.folded, { role: 'user', content, id: tmpId, anonymized }],
    }))
    setInput('')
    setStreaming(true)
    // da anteprima il turno è GIÀ anonimizzato: si passa dritti al modello
    if (anonymized && !fromStaged) setAnon({ files: pending.map((a) => ({ id: a.id, filename: a.filename })), progress: null })
    scrollToBottom()   // il proprio messaggio si vede sempre, ovunque fosse lo scroll

    abortRef.current = streamChatMessage(
      activeId, { content, from_staged: fromStaged, ocr },
      makeTurnHandler({ convId: activeId, content }),
      (err) => failTurn(err.message, content))
  }

  function finishTurn() {
    setStreaming(false)
    setStopping(false)
    setAnon(null)
    abortRef.current = null
    // ricarica dal server: thread e allegati canonici (artifact inclusi).
    // activeIdRef e non lo stato: finishTurn arriva anche da closure vecchie
    // (il riaggancio partito da loadConv) e deve colpire la chat giusta.
    const id = activeIdRef.current
    if (id) {
      api.getChat(id).then((c) => {
        if (activeIdRef.current !== id) return
        setConv({ ...c, folded: foldMessages(c.messages) })
        setLive(null)
      }).catch(() => setLive(null))
      refreshChats()
    } else {
      setLive(null)
    }
  }

  // Il turno non è partito: niente è stato inviato. Il messaggio scritto
  // torna nella casella (perderlo era il modo più rapido di far sembrare
  // l'app rotta) e il motivo resta a video finché non si riprova — la
  // ricarica del thread, da sola, cancellerebbe l'errore appena scritto.
  function failTurn(message, restore) {
    setStreaming(false)
    setStopping(false)
    setAnon(null)
    setLive(null)
    setMergeCheck(null)
    abortRef.current = null
    setTurnError(message || t('error.sendFailed'))
    toast.error(message || t('error.sendFailed'), { duration: 10000 })
    setInput((cur) => cur || restore || '')
    const id = activeIdRef.current
    if (id) {
      api.getChat(id)
        .then((c) => {
          if (activeIdRef.current !== id) return
          setConv({ ...c, folded: foldMessages(c.messages) })
        })
        .catch(() => {})
    }
  }

  async function stop() {
    // il turno si ferma SOLO così: chiudere la lettura non lo tocca più.
    // Il `done` (canceled) arriverà dallo stream e chiuderà lo stato.
    setStopping(true)
    try { await api.stopChat(activeId) } catch { /* */ }
  }

  // «continua» dal dialog delle fusioni: il turno fermo sul server riprende
  async function continueAfterMerge() {
    setMergeCheck(null)
    try {
      await api.continueChatMessage(activeId)
    } catch (e) {
      // il turno non c'è più (riavvio, stop): lo stream chiuderà da solo
      toast.error(e.message)
    }
  }

  // «annulla l'invio» dal dialog delle fusioni: niente è partito, quindi il
  // turno si chiude qui. L'abort del fetch non passa da onError (è voluto:
  // non è un guasto), perciò la chiusura la si fa a mano — altrimenti la
  // casella resterebbe bloccata in attesa di un turno che non tornerà.
  async function cancelAtMerge() {
    const content = mergeCheck?.content
    setMergeCheck(null)
    try { await api.stopChat(activeId) } catch { /* già finito */ }
    abortRef.current?.()
    failTurn(t('error.canceled'), content)
  }

  const keyMissing = status && !status.configured
  const sandboxOff = status && status.sandbox && !status.sandbox.available
  // il browser della ricerca web gira in un container: senza runtime Docker
  // (né istanza esterna configurata) la feature non può partire
  const webOffDocker = !!status?.browser && !status.browser.available
  const modelEntry = models.find((m) => m.id === conv?.model)
  // chat su un modello che l'admin ha poi tolto dalla white list: il server
  // rifiuta l'invio (403 model_not_allowed), qui lo si dice prima
  const modelNotAllowed = !!(allowedIds && conv?.model
                             && !allowedIds.includes(conv.model))
  // occupazione del contesto: prompt+completion dell'ULTIMA risposta (non i
  // totali della chat, che sommano le iterazioni). Si aggiorna a fine turno.
  const ctxUsed = [...(conv?.messages || [])].reverse()
    .find((m) => m.usage?.context_tokens)?.usage.context_tokens
  const ctxMax = modelEntry?.context_length
  const pending = (conv?.attachments || [])
    .filter((a) => a.direction === 'in' && !a.message_id)
  // la coda è globale alla pagina: i chip sono solo quelli di QUESTA chat
  const myUploads = uploads.filter((u) => u.convId === conv?.id)
  const isAnon = !!conv?.anonymized

  return (
    <div className="chat-page flex min-h-0 min-w-0 w-full flex-1 flex-col xl:flex-row">
      {/* file del progetto ancora da confermare: il modello non li vedrebbe */}
      <UnconfirmedFilesDialog open={!!askUnconfirmed}
                              onOpenChange={(o) => { if (!o) setAskUnconfirmed(null) }}
                              files={askUnconfirmed || []}
                              onProceed={() => { setAskUnconfirmed(null); createChat(false) }}
                              onSkipForever={skipUnconfirmedWarning} />
      {/* popup OCR pre-invio: gli allegati contengono immagini */}
      <Dialog open={!!askOcr} onOpenChange={(open) => { if (!open) setAskOcr(null) }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t('ocr.title')}</DialogTitle>
            <DialogDescription className="break-words">
              {t('ocr.found', {
                count: pending.reduce((s, a) => s + (a.n_images || 0), 0) })}
              {' '}{t('ocr.question')}
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2">
            <Button variant="outline"
                    onClick={() => proceedSend(askOcr.content, false)}>
              {t('ocr.textOnly')}
            </Button>
            <Button onClick={() => proceedSend(askOcr.content, true)}>
              {t('ocr.use')}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
      {/* fusioni da confermare: il turno è fermo sul server, niente è ancora
          partito verso il modello. Non si chiude a vuoto (né Escape né click
          fuori): l'unica uscita è «Continua» o «Annulla l'invio». */}
      <Dialog open={!!mergeCheck} onOpenChange={() => {}}>
        <DialogContent className="max-w-xl" hideClose
                       onEscapeKeyDown={(e) => e.preventDefault()}
                       onInteractOutside={(e) => e.preventDefault()}>
          <DialogHeader>
            <DialogTitle>{t('merge.title')}</DialogTitle>
            <DialogDescription className="break-words">
              <Trans i18nKey="merge.description" ns="chat" components={{ b: <b /> }} />
            </DialogDescription>
          </DialogHeader>
          <EntityReview registry={registry} suggestions={mergeCheck?.suggestions}
                        applies="now"
                        emptyLabel={t('review.noMerges', { ns: 'anon' })}
                        className="rounded-md border border-border"
                        onChanged={(r) => setMergeCheck(
                          (m) => ({ ...m, suggestions: r.suggestions }))} />
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={cancelAtMerge}>
              {t('merge.cancel')}
            </Button>
            <Button className="gap-2" onClick={continueAfterMerge}>
              <Send className="size-4" /> {t('merge.continue')}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
      {staged && (
        <ReviewModal items={staged.items} services={stagedServices}
                     registry={registry}
                     onJobStart={addJob}
                     onChange={(payload) => {
                       // le modifiche rispondono con lo stato completo
                       // (mappa condivisa: cambiano TUTTI i pezzi)
                       applyStagedAttachments(payload)
                       setStaged((s) => ({ ...s, ...payload }))
                     }}
                     onCancel={cancelStaged}
                     onConfirm={confirmStaged} />
      )}
      {/* elenco conversazioni: solo nelle chat di progetto (per le chat
          libere l'elenco sta nella sidebar principale) */}
      {projectId && (
        <ResponsiveSidebar title={t('title')} breakpoint={1280}
                           className="flex w-[240px] flex-none flex-col border-r border-border bg-card">
          <div className="flex flex-col gap-2 p-3 pt-14 xl:pt-3">
            <Link to={`/projects/${projectId}`}
                  className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground hover:underline">
              <FolderKanban className="size-3.5 shrink-0" />
              <span className="truncate" title={project?.name || t('backToProject')}>
                ← {project?.name || t('backToProject')}
              </span>
            </Link>
            <Button className="w-full gap-2" onClick={newChat}>
              <Plus /> {t('new')}
            </Button>
          </div>
          <ul className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
            {chats.map((c) => (
              <li key={c.id}>
                <button
                  className={cn(
                    'group flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm hover:bg-accent',
                    c.id === activeId && 'bg-secondary'
                  )}
                  onClick={() => loadConv(c.id)}
                >
                  {c.anonymized
                    ? <Lock className="size-3 shrink-0 text-emerald-600 dark:text-emerald-400" />
                    : <LockOpen className="size-3 shrink-0 text-amber-600 dark:text-amber-400" />}
                  <span className="min-w-0 flex-1 truncate">{c.title}</span>
                  <Trash2
                    className="touch-action size-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity hover:text-destructive group-hover:opacity-100"
                    onClick={(e) => removeChat(c.id, e)}
                  />
                </button>
              </li>
            ))}
            {chats.length === 0 && (
              <li className="px-2.5 py-6 text-center text-xs text-muted-foreground">
                {t('empty')}
              </li>
            )}
          </ul>
        </ResponsiveSidebar>
      )}

      {/* thread: tutta l'area accetta file trascinati dal PC */}
      <main className="relative flex min-h-0 min-w-0 flex-1 flex-col" {...dropProps}>
        {dragOver && conv && (
          <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center bg-background/80 backdrop-blur-sm">
            <div className={cn(
              'flex flex-col items-center gap-2 rounded-xl border-2 border-dashed px-8 py-6',
              streaming ? 'border-muted-foreground text-muted-foreground' : 'border-primary text-primary'
            )}>
              <FileUp className="size-8" />
              <p className="text-sm font-medium">
                {t(streaming ? 'composer.dropWait' : 'composer.dropHere')}
              </p>
            </div>
          </div>
        )}
        {!conv ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 text-center text-muted-foreground">
            {/* nel progetto è un invito a scegliere dall'aside; nella chat
                libera la conversazione sta arrivando dall'URL */}
            {projectId ? (
              <>
                <MessageSquarePlus className="size-10 opacity-40" />
                <p className="text-sm">{t('pickOne')}</p>
              </>
            ) : (
              <Loader2 className="size-6 animate-spin opacity-60" />
            )}
          </div>
        ) : (
          <>
            <div className="chat-heading">
            <header className="chat-toolbar flex shrink-0 flex-wrap items-center gap-2 border-b border-border px-3 py-2 sm:gap-3 sm:px-4 sm:py-2.5">
              <span data-tour="model" className="inline-flex min-w-0 max-w-full flex-1 sm:flex-none">
                <ModelSelector models={models} value={conv.model}
                               allowNonZdr={!!status?.allow_non_zdr}
                               allowedIds={allowedIds}
                               onChange={setModel} disabled={streaming} />
              </span>
              {/* la ricerca web sta DENTRO le opzioni del modello: non arriva
                  dal catalogo (la gestiamo noi), quindi la sua riga c'è
                  sempre, disabilitata quando Docker non c'è o l'admin ha
                  spento la feature */}
              <span data-tour="options" className="inline-flex">
                <ModelOptions model={modelEntry} value={conv.options}
                              onChange={setOptions} disabled={streaming}
                              web={{ active: conv.options?.web_search !== false,
                                     usable: !!status?.web_search,
                                     dockerOff: webOffDocker }} />
              </span>
              {/* le categorie coperte hanno senso solo dove si anonimizza;
                  nelle chat di progetto si gestiscono DAL progetto */}
              {isAnon && !conv.project_id && (
                <span data-tour="anon-tags" className="inline-flex">
                  <AnonTags convId={conv.id} tags={tags} groups={tagGroups} value={conv.anon_options}
                            onChange={setAnonTags} disabled={streaming} />
                </span>
              )}
              <div className="chat-stats ml-auto flex shrink-0 items-center gap-2 sm:gap-3">
                {/* Contesto e costo condividono lo stile dei parametri;
                    il dettaglio completo resta disponibile al click. */}
                {ctxUsed > 0 && ctxMax > 0 && (() => {
                  const pct = Math.min(100,
                                       Math.round((ctxUsed / ctxMax) * 100))
                  return (
                    <InfoPill className={cn(
                                pct >= 95 ? 'text-destructive'
                                  : pct >= 80 ? 'text-amber-700 dark:text-amber-400'
                                  : 'text-foreground')}
                              detail={t('banner.contextTitle', {
                                used: fmtInt(ctxUsed), max: fmtInt(ctxMax),
                                pct })}>
                      {compact ? `${pct}%` : t('banner.contextPct', { pct })}
                    </InfoPill>
                  )
                })()}
                {conv.usage_total?.cost > 0 && (
                  <InfoPill className="text-foreground"
                            detail={t('banner.costTitle', {
                              cost: conv.usage_total.cost.toFixed(6),
                              prompt: fmtInt(conv.usage_total.prompt_tokens),
                              completion: fmtInt(conv.usage_total.completion_tokens),
                            })}>
                    {fmtCost(conv.usage_total.cost)}
                  </InfoPill>
                )}
                <StatusErrors problems={[
                  ...(sandboxOff ? [{ key: 'sandbox', Icon: Terminal,
                                      title: t('banner.sandboxOff'),
                                      detail: t('banner.sandboxOffHelp') }] : []),
                  ...(webOffDocker ? [{ key: 'web', Icon: Globe,
                                        title: t('banner.webOff'),
                                        detail: t('banner.webOffHelp') }] : []),
                ]} />
              </div>
            </header>

            {keyMissing && (
              <div className="flex items-center gap-2 border-b border-border bg-destructive/10 px-4 py-2 text-sm text-destructive">
                <AlertCircle className="size-4" />
                {t('banner.keyMissing')}
              </div>
            )}

            {modelNotAllowed && (
              <div className="flex items-start gap-2 border-b border-border bg-destructive/10 px-4 py-2 text-sm text-destructive">
                <Lock className="mt-0.5 size-4 shrink-0" />
                <span className="flex-1">
                  <Trans i18nKey="banner.modelNotAllowed" ns="chat"
                         values={{ model: modelEntry?.name || conv.model }}
                         components={{ b: <b /> }} />
                </span>
              </div>
            )}

            <PrivacyNotice model={modelEntry} isAdmin={user?.role === 'admin'}
                           allowed={!!status?.allow_non_zdr}
                           relaxed={!!conv.options?.allow_non_zdr}
                           onChange={(on) => setOptions({
                             ...(conv.options || {}), allow_non_zdr: on })} />

            {isAnon && (
              <div className="chat-anon-banner flex shrink-0 flex-wrap items-center gap-2 border-b border-border bg-emerald-500/10 px-4 py-2 text-sm text-emerald-700 dark:text-emerald-400">
                {/* scelta dell'utente: invio diretto (default) o revisione
                    dell'anonimizzazione prima di far partire qualsiasi cosa */}
                <label className={cn(
                  'inline-flex shrink-0 cursor-pointer select-none items-center gap-1.5 text-xs',
                  streaming && 'pointer-events-none opacity-50')}
                       title={t('banner.reviewHint')} data-tour="review">
                  <input type="checkbox" className="accent-emerald-600"
                         checked={reviewBefore} disabled={streaming}
                         onChange={(e) => {
                           setReviewBefore(e.target.checked)
                           localStorage.setItem('chat-review-before-send',
                             e.target.checked ? '1' : '0')
                         }} />
                  {t('banner.review')}
                </label>
              </div>
            )}

            </div>

            {/* Le fusioni NON si gestiscono più qui: l'unico posto in cui si
                decidono è la revisione pre-invio (ultimo step del modal), più
                il dialog che ferma il turno quando il registro nasce. */}

            <div className="chat-thread relative min-h-0 flex-1">
              <div ref={scrollRef} onScroll={onThreadScroll}
                   className="h-full overflow-y-auto">
                <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4">
                  {conv.folded.map((m, i) => (
                    <Message key={m.id || i} msg={m} convId={conv.id}
                             attachments={conv.attachments} />
                  ))}
                  {anon && <AnonProgress state={anon} />}
                  {live && <Message msg={live} convId={conv.id} streaming={streaming}
                                    attachments={conv.attachments} />}
                </div>
              </div>
              {/* con l'autoscroll sospeso serve un modo per rientrare in coda
                  senza scrollare a mano una risposta lunga */}
              {!atBottom && (
                <button type="button" onClick={() => scrollToBottom()}
                        className="absolute bottom-4 left-1/2 inline-flex -translate-x-1/2 items-center gap-1.5
                                   rounded-full border border-border bg-card px-3 py-1.5 text-xs
                                   shadow-md hover:bg-accent">
                  <ChevronDown className="size-3.5" />
                  {t(streaming ? 'thread.follow' : 'thread.toBottom')}
                </button>
              )}
            </div>

            {/* allegati caricati e non ancora inviati: partono col prossimo
                messaggio (poi restano sotto la sua bolla) */}
            {(pending.length > 0 || myUploads.length > 0) && (
              <div className="chat-pending flex flex-col gap-2 overflow-y-auto border-t border-border px-3 py-2 sm:px-4">
                <div className="text-xs text-muted-foreground">
                  {t('composer.pendingTitle')}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {pending.map((a) => (
                    <AttachmentChip key={a.id} convId={conv.id} att={a}
                                    pending busy={streaming}
                                    onRemove={removeAttachment} />
                  ))}
                  {/* chip in attesa: il file esiste per l'utente da quando lo
                      sceglie, non da quando il server risponde. Chi è dietro
                      nella coda si vede subito, con l'orologio invece dello
                      spinner */}
                  {myUploads.map((u) => (
                    <div key={u.key}
                         className="inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-md border border-dashed border-border bg-card px-2 py-1.5 text-xs text-muted-foreground">
                      {u.active
                        ? <Loader2 className="size-3 shrink-0 animate-spin text-primary" />
                        : <Clock className="size-3 shrink-0" />}
                      <span className="min-w-0 max-w-[220px] truncate text-card-foreground"
                            title={u.name}>{u.name}</span>
                      <span className="shrink-0">
                        {t(u.active ? 'composer.uploading' : 'composer.queued',
                           { size: fmtBytes(u.size) })}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Un invio che non parte deve DIRLO e restare detto: la ricarica
                del thread cancellerebbe un errore scritto sulla bolla. */}
            {turnError && (
              <div className="flex items-start gap-2 border-t border-border bg-destructive/10 px-4 py-2 text-sm text-destructive">
                <AlertCircle className="mt-0.5 size-4 shrink-0" />
                <span className="flex-1">{turnError}</span>
                <button type="button" className="shrink-0 text-xs underline-offset-2 hover:underline"
                        onClick={() => setTurnError('')}>
                  {t('actions.close', { ns: 'common' })}
                </button>
              </div>
            )}

            {/* composer */}
            <div className="chat-composer shrink-0 border-t border-border p-3">
              <div className="mx-auto flex w-full max-w-3xl items-end gap-2" data-tour="composer">
                <Button variant="outline" size="icon" aria-label={t('composer.attach')} title={t('composer.attach')}
                        disabled={streaming}
                        onClick={() => fileRef.current?.click()}>
                  {myUploads.length > 0 ? <Loader2 className="animate-spin" /> : <Paperclip />}
                </Button>
                <input ref={fileRef} type="file" multiple className="hidden"
                       onChange={onPickFiles} />
                <textarea
                  ref={taRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onPaste={onPaste}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
                  }}
                  rows={1}
                  placeholder={t(compact ? 'composer.placeholderMobile' : 'composer.placeholder')}
                  disabled={streaming}
                  aria-label={t('composer.placeholder')}
                  className="min-h-[44px] min-w-0 flex-1 resize-none rounded-md border border-input bg-card px-3 py-2 text-sm outline-none focus-visible:border-ring disabled:opacity-60"
                />
                {streaming ? (
                  <Button variant="destructive" size="icon"
                          title={t(stopping ? 'composer.stopping' : 'composer.stop')}
                          disabled={stopLocked || stopping} onClick={stop}>
                    <Square />
                  </Button>
                ) : (
                  <Button size="icon"
                          aria-label={t('composer.send')}
                          title={t(myUploads.length > 0 ? 'composer.waitUploads' : 'composer.send')}
                          disabled={!input.trim() || keyMissing || myUploads.length > 0}
                          onClick={send}>
                    <Send />
                  </Button>
                )}
              </div>
              {/* disclosure AI (art. 50(1) AI Act): visibile in ogni
                  conversazione, qualunque sia il percorso di ingresso */}
              <p className="mx-auto mt-2 w-full max-w-3xl text-center text-xs text-muted-foreground">
                {t('composer.aiDisclosure')}
              </p>
            </div>
          </>
        )}
      </main>
    </div>
  )
}
