import React, { useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { cn } from '@/lib/utils'
import {
  hasRestorablePlaceholder,
  restorePlaceholders,
  splitRestorablePlaceholders,
} from '@/lib/placeholders.js'

// Renderer markdown minimale e senza dipendenze: copre ciò che un LLM
// produce in chat (paragrafi, titoli, liste, tabelle GFM, blocchi di codice,
// grassetto, corsivo, codice inline, link). Non è un parser completo —
// niente HTML grezzo o blocchi annidati — ma evita di trascinare
// react-markdown + remark nel bundle: qui il testo passa PRIMA per la
// tokenizzazione delle entità PII (sotto), e su react-markdown la stessa
// cosa richiederebbe un plugin rehype che spezza i nodi di testo. I blocchi
// ``` non vengono mai interpretati come markdown (si mostrano verbatim),
// così il codice resta leggibile.

// spezza il testo in blocchi: fence ```...``` oppure testo normale
function splitBlocks(src) {
  const blocks = []
  const lines = src.replace(/\r\n/g, '\n').split('\n')
  let buf = []
  let i = 0
  const flush = () => {
    if (buf.length) { blocks.push({ type: 'text', content: buf.join('\n') }); buf = [] }
  }
  while (i < lines.length) {
    const m = lines[i].match(/^```(\w*)\s*$/)
    if (m) {
      flush()
      const lang = m[1]
      const code = []
      i++
      while (i < lines.length && !/^```\s*$/.test(lines[i])) { code.push(lines[i]); i++ }
      i++ // salta la fence di chiusura
      blocks.push({ type: 'code', lang, content: code.join('\n') })
    } else {
      buf.push(lines[i]); i++
    }
  }
  flush()
  return blocks
}

// Gli span PII vengono sostituiti con token privati PRIMA del parsing: in
// questo modo un valore reale contenente `*`, HTML o sintassi link rimane un
// nodo testuale React e non può cambiare il Markdown circostante.
function tokenizeEntities(text, entities) {
  if (!entities?.length) return { text, lookup: new Map() }
  const lookup = new Map()
  const out = []
  let pos = 0
  entities.slice().sort((a, b) => a.start - b.start).forEach((entity, i) => {
    if (entity.start < pos || entity.end < entity.start || entity.end > text.length) return
    const token = `\uE000${i}\uE001`
    out.push(text.slice(pos, entity.start), token)
    lookup.set(token, { ...entity, value: text.slice(entity.start, entity.end) })
    pos = entity.end
  })
  out.push(text.slice(pos))
  return { text: out.join(''), lookup }
}

// URL "nudi" nel testo: http(s) o www.; \b e il lookbehind evitano di
// agganciare il mezzo di una parola ("pippowww.com"). Il char-class esclude
// i delimitatori dei token PII, così un URL adiacente a un'entità non
// ingloba il token.
const BARE_URL_RE = /\bhttps?:\/\/[^\s\uE000\uE001]+|(?<![\w.])www\.[^\s\uE000\uE001]+/

// La punteggiatura finale appartiene alla frase, non al link ("vedi
// https://x.it." -> il punto resta testo); una ")" chiusa senza la
// corrispettiva aperta dentro l'URL è la parentesi del discorso.
function trimBareUrl(raw) {
  let url = raw
  for (;;) {
    if (/[.,;:!?\u2026'"\u00BB\u203A]$/.test(url)) { url = url.slice(0, -1); continue }
    if (url.endsWith(')') &&
        (url.match(/\(/g) || []).length < (url.match(/\)/g) || []).length) {
      url = url.slice(0, -1); continue
    }
    return url
  }
}

// href per un testo che È un URL (nudo), null altrimenti
function urlHref(text) {
  if (!new RegExp(`^(?:${BARE_URL_RE.source})$`).test(text)) return null
  return text.startsWith('www.') ? `https://${text}` : text
}

// Testo semplice con i soli URL resi cliccabili: niente markdown (la bolla
// utente mostra quello che l'utente ha scritto, verbatim) e colore ereditato,
// perché la bolla ha il proprio foreground (bianco su primary).
export function Linkify({ text }) {
  if (!text) return null
  const re = new RegExp(BARE_URL_RE.source, 'g')
  const nodes = []
  let last = 0
  let m
  let k = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index))
    const url = trimBareUrl(m[0])
    nodes.push(
      <a key={k++} href={url.startsWith('www.') ? `https://${url}` : url}
         target="_blank" rel="noreferrer"
         className="underline underline-offset-2 hover:opacity-80">
        {url}
      </a>
    )
    last = m.index + url.length
  }
  if (last < text.length) nodes.push(text.slice(last))
  return nodes
}

// formattazione inline: `code`, **bold**, *italic*, [testo](url), URL nudi,
// entità PII. `t` viaggia come parametro fin qui: queste sono funzioni pure,
// non componenti, e non possono chiamare useTranslation.
function renderCodeText(text, keyBase, values, t) {
  return splitRestorablePlaceholders(text, values).map((part, i) =>
    part.placeholder ? (
      <mark key={`${keyBase}-${i}`}
            title={t('code.restoredHelp', { placeholder: part.placeholder })}
            className="rounded bg-amber-200/80 px-0.5 text-inherit dark:bg-amber-500/30">
        {part.value}
      </mark>
    ) : part.text)
}

function renderInline(text, keyBase, entityLookup, codeValues, t) {
  const nodes = []
  const re = new RegExp(
    `(\uE000\\d+\uE001)|(\`[^\`]+\`)|(\\*\\*[^*]+\\*\\*)|(\\*[^*]+\\*)|(\\[[^\\]]+\\]\\([^)]+\\))|(${BARE_URL_RE.source})`,
    'g')
  let last = 0
  let m
  let k = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index))
    const tok = m[0]
    const key = `${keyBase}-i${k++}`
    const entity = entityLookup?.get(tok)
    if (entity) {
      // un valore ripristinato che è un URL resta evidenziato ma diventa
      // anche cliccabile: il link punta al valore vero, non al segnaposto
      const href = urlHref(entity.value.trim())
      const mark = (
        <mark key={key}
              title={t('restoredFrom', { placeholder: entity.placeholder })}
              className="rounded bg-amber-200/80 px-0.5 text-inherit dark:bg-amber-500/30">
          {entity.value}
        </mark>
      )
      nodes.push(href
        ? <a key={key} href={href} target="_blank" rel="noreferrer"
             className="underline underline-offset-2">{mark}</a>
        : mark)
    } else if (tok.startsWith('`')) {
      // Il Markdown ha già classificato il segmento come codice: ripristinare
      // ora produce soltanto nodi testuali React. Un valore con backtick/HTML
      // non può uscire dal <code> né cambiare il documento circostante.
      nodes.push(<code key={key} className="font-mono">
        {renderCodeText(tok.slice(1, -1), `${key}-c`, codeValues, t)}
      </code>)
    } else if (tok.startsWith('**')) {
      // ricorsione: dentro il grassetto possono esserci link o codice
      nodes.push(<strong key={key}>{renderInline(tok.slice(2, -2), `${key}-b`, entityLookup, codeValues, t)}</strong>)
    } else if (tok.startsWith('*')) {
      nodes.push(<em key={key}>{renderInline(tok.slice(1, -1), `${key}-e`, entityLookup, codeValues, t)}</em>)
    } else if (tok.startsWith('[')) {
      const mm = tok.match(/^\[([^\]]+)\]\(([^)]+)\)$/)
      nodes.push(
        <a key={key} href={mm[2]} target="_blank" rel="noreferrer"
           className="text-primary underline underline-offset-2">
          {renderInline(mm[1], `${key}-a`, entityLookup, codeValues, t)}
        </a>
      )
    } else {
      // URL nudo: cliccabile; l'eventuale punteggiatura finale resta testo
      const url = trimBareUrl(tok)
      nodes.push(
        <a key={key} href={url.startsWith('www.') ? `https://${url}` : url}
           target="_blank" rel="noreferrer"
           className="text-primary underline underline-offset-2">
          {url}
        </a>
      )
      if (url.length < tok.length) nodes.push(tok.slice(url.length))
    }
    last = m.index + tok.length
  }
  if (last < text.length) nodes.push(text.slice(last))
  return nodes
}

// ---- tabelle GFM -------------------------------------------------------
// Riga di intestazione, riga di separazione (|---|:--:|---:|), poi il corpo
// fino alla prima riga vuota. La separazione è obbligatoria e deve avere lo
// stesso numero di celle dell'intestazione: così un paragrafo che contiene
// un "|" resta un paragrafo. Le entità PII sono già token opachi a questo
// punto, quindi un valore reale che contiene "|" non può sfasare le colonne.
const DELIM_RE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/

function splitRow(line) {
  const cells = []
  let cur = ''
  for (let i = 0; i < line.length; i += 1) {
    if (line[i] === '\\' && line[i + 1] === '|') { cur += '|'; i += 1 }   // pipe sfuggita
    else if (line[i] === '|') { cells.push(cur); cur = '' }
    else cur += line[i]
  }
  cells.push(cur)
  if (cells.length > 1 && cells[0].trim() === '') cells.shift()
  if (cells.length > 1 && cells[cells.length - 1].trim() === '') cells.pop()
  return cells.map((c) => c.trim())
}

function alignOf(cell) {
  const left = cell.startsWith(':')
  const right = cell.endsWith(':')
  if (left && right) return 'text-center'
  if (right) return 'text-right'
  return ''
}

function matchTable(lines, i) {
  if (!lines[i].includes('|')) return null
  const delim = lines[i + 1]
  if (!delim || !DELIM_RE.test(delim)) return null
  const header = splitRow(lines[i])
  const aligns = splitRow(delim).map(alignOf)
  if (!header.length || aligns.length !== header.length) return null
  const rows = []
  let j = i + 2
  while (j < lines.length && lines[j].trim() !== '' && lines[j].includes('|')) {
    rows.push(splitRow(lines[j])); j += 1
  }
  return { header, aligns, rows, end: j - 1 }
}

function renderTable({ header, aligns, rows }, key, entityLookup, codeValues, t) {
  return (
    // min-w-0: dentro la colonna flex della bolla, senza questo il contenitore
    // si allarga col contenuto invece di scrollare
    <div key={key} className="min-w-0 max-w-full overflow-x-auto rounded-md border border-border">
      <table className="w-full border-collapse text-left text-[13px]">
        {/* accent e non muted: styles.css (CSS del viewer) ridefinisce --muted
            con un grigio da TESTO, e come fondo risulterebbe pesante */}
        <thead className="bg-accent">
          <tr>
            {header.map((c, ci) => (
              <th key={ci} className={cn('px-2.5 py-1.5 align-top font-semibold', aligns[ci])}>
                {renderInline(c, `${key}-h${ci}`, entityLookup, codeValues, t)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, ri) => (
            <tr key={ri} className="border-t border-border/60">
              {/* si itera sull'intestazione: le righe sbilanciate non sfasano
                  la griglia (celle mancanti vuote, quelle in più ignorate) */}
              {header.map((_, ci) => (
                <td key={ci} className={cn('px-2.5 py-1.5 align-top', aligns[ci])}>
                  {renderInline(r[ci] || '', `${key}-r${ri}c${ci}`, entityLookup, codeValues, t)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// un blocco di testo -> titoli / liste / tabelle / paragrafi
function renderTextBlock(content, keyBase, entityLookup, codeValues, t) {
  const lines = content.split('\n')
  const out = []
  let list = null // {ordered, items}
  let para = []
  let k = 0
  const flushPara = () => {
    if (para.length) {
      out.push(<p key={`${keyBase}-p${k++}`} className="whitespace-pre-wrap leading-relaxed">
        {renderInline(para.join('\n'), `${keyBase}-p${k}`, entityLookup, codeValues, t)}
      </p>)
      para = []
    }
  }
  const flushList = () => {
    if (list) {
      const Tag = list.ordered ? 'ol' : 'ul'
      out.push(
        <Tag key={`${keyBase}-l${k++}`}
             className={list.ordered ? 'list-decimal pl-5 space-y-0.5' : 'list-disc pl-5 space-y-0.5'}>
          {list.items.map((it, idx) => (
            <li key={idx}>{renderInline(it, `${keyBase}-l${k}-${idx}`, entityLookup, codeValues, t)}</li>
          ))}
        </Tag>
      )
      list = null
    }
  }
  for (let li = 0; li < lines.length; li += 1) {
    const raw = lines[li]
    const table = matchTable(lines, li)
    const heading = raw.match(/^(#{1,4})\s+(.*)$/)
    const bullet = raw.match(/^\s*[-*]\s+(.*)$/)
    const numbered = raw.match(/^\s*\d+\.\s+(.*)$/)
    if (table) {
      flushPara(); flushList()
      out.push(renderTable(table, `${keyBase}-t${k++}`, entityLookup, codeValues, t))
      li = table.end
    } else if (heading) {
      flushPara(); flushList()
      const level = heading[1].length
      const sz = level <= 1 ? 'text-lg' : level === 2 ? 'text-base' : 'text-sm'
      out.push(<div key={`${keyBase}-h${k++}`} className={`mt-1 font-semibold ${sz}`}>
        {renderInline(heading[2], `${keyBase}-h${k}`, entityLookup, codeValues, t)}
      </div>)
    } else if (bullet || numbered) {
      flushPara()
      const ordered = !!numbered
      if (!list || list.ordered !== ordered) { flushList(); list = { ordered, items: [] } }
      list.items.push((bullet || numbered)[1])
    } else if (raw.trim() === '') {
      flushPara(); flushList()
    } else {
      flushList(); para.push(raw)
    }
  }
  flushPara(); flushList()
  return out
}

// Il blocco conserva il testo canonico internamente, ma a video i placeholder
// noti diventano nodi testuali evidenziati. Le due copie restano intenzionali:
// quella canonica è sempre sicura da rimandare al modello; quella coi valori è
// una sostituzione letterale e può richiedere escaping nel linguaggio scelto.
function CodeBlock({ content, values }) {
  const [copied, setCopied] = useState(null)
  const { t } = useTranslation('chat')
  const restorable = hasRestorablePlaceholder(content, values)

  async function copy(real) {
    const text = real
      ? restorePlaceholders(content, values)
      : content
    try {
      await navigator.clipboard.writeText(text)
      setCopied(real ? 'real' : 'raw')
      setTimeout(() => setCopied(null), 1800)
    } catch { /* clipboard negata: nessun danno */ }
  }

  return (
    <div className="group relative">
      <pre className="overflow-x-auto rounded-md bg-muted p-3 text-xs">
        {/* styles.css ha una regola globale (fuori dai layer) su `code`:
            qui la si neutralizza, il fondo lo dà già il <pre> */}
        <code className="font-mono" style={{ background: 'transparent', padding: 0 }}>
          {renderCodeText(content, 'fenced', values, t)}
        </code>
      </pre>
      <div className="absolute right-1.5 top-1.5 flex gap-1 opacity-0 transition-opacity
                      focus-within:opacity-100 group-hover:opacity-100">
        {restorable && (
          <button type="button" onClick={() => copy(true)}
                  title={t('code.copyRealHelp')}
                  className="inline-flex items-center gap-1 rounded border border-border
                             bg-background/90 px-1.5 py-0.5 text-[11px] hover:bg-accent">
            {copied === 'real' ? <Check className="size-3" /> : <Copy className="size-3" />}
            {t(copied === 'real' ? 'code.copied' : 'code.copyReal')}
          </button>
        )}
        <button type="button" onClick={() => copy(false)}
                title={t(restorable ? 'code.copyRawHelp' : 'code.copyHelp')}
                className="inline-flex items-center gap-1 rounded border border-border
                           bg-background/90 px-1.5 py-0.5 text-[11px] hover:bg-accent">
          {copied === 'raw' ? <Check className="size-3" /> : <Copy className="size-3" />}
          {t(copied === 'raw' ? 'code.copied'
                              : restorable ? 'code.copyRaw' : 'code.copy')}
        </button>
      </div>
    </div>
  )
}

export default function Markdown({ text, entities, codeValues }) {
  const { t } = useTranslation('chat')
  if (!text) return null
  const tokenized = tokenizeEntities(text, entities)
  const blocks = splitBlocks(tokenized.text)
  return (
    <div className="flex min-w-0 flex-col gap-2 text-sm">
      {blocks.map((b, i) =>
        b.type === 'code' ? (
          <CodeBlock key={i} content={b.content} values={codeValues} />
        ) : (
          <div key={i} className="flex min-w-0 flex-col gap-2">
            {renderTextBlock(b.content, `b${i}`, tokenized.lookup, codeValues, t)}
          </div>
        )
      )}
    </div>
  )
}
