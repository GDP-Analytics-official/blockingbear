import React, { useState } from 'react'
import { ChartColumn, Download, FileSpreadsheet, Table2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'

// Palette categorica validata (dataviz skill) sulla superficie bianca delle
// card: l'ORDINE è il meccanismo di sicurezza per il daltonismo, non estetica.
// Il colore segue l'entità (utente o modello) con assegnazione stabile, mai
// la sua posizione in classifica; oltre gli 8 slot NIENTE colori inventati:
// quelle serie confluiscono in un'unica voce grigia.
export const PALETTE = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100',
                        '#e87ba4', '#008300', '#4a3aa7', '#e34948']
export const OTHER_COLOR = '#898781'

// Il punto decimale NON segue la lingua: sono importi in dollari letti da
// OpenRouter e si scrivono come li scrive OpenRouter ("$12.34"). Un "12,34 $"
// accanto a un cruscotto che parla di USD confonderebbe più di quanto aiuti,
// e gli stessi numeri finiscono negli export.
export function fmtUsd(v) {
  const n = Number(v) || 0
  // sotto il centesimo: 2 cifre significative, mai un "$0.0000" che appiattisce
  // l'asse quando i costi sono micro (un turno costa anche 8e-6 USD)
  if (n !== 0 && Math.abs(n) < 0.01) return `$${Number(n.toPrecision(2))}`
  if (Math.abs(n) < 1) return `$${n.toFixed(3)}`
  return `$${n.toFixed(2)}`
}

// «mld»/«mln» sono parole: arrivano dal catalogo (`t` del namespace costs).
// La «k» no, è la stessa ovunque.
export function fmtInt(v, t) {
  const n = Number(v) || 0
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} ${t('shared.billions')}`
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} ${t('shared.millions')}`
  if (n >= 1e4) return `${Math.round(n / 1e3)} k`
  return String(Math.round(n))
}

export function fmtDay(iso, locale) {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso
    : d.toLocaleDateString(locale, { day: '2-digit', month: '2-digit' })
}

// il nome utente dietro una chiave: dal join del backend, o dal pattern
export function keyOwnerName(apiKeyId, keyIndex) {
  return keyIndex.get(apiKeyId)
      || String(apiKeyId || '?').replace(/^blockingbear-/, '')
}

export function StatTile({ label, value, hint }) {
  return (
    <div className="flex min-w-40 flex-1 flex-col gap-1 rounded-xl border border-border bg-card p-4 shadow-sm">
      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className="text-2xl font-semibold tracking-tight">{value}</span>
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
    </div>
  )
}

function Legend({ series }) {
  if (!series.length) return null
  return (
    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
      {series.map((s) => (
        <span key={s.name} className="inline-flex items-center gap-1.5">
          <span className="inline-block size-2.5 rounded-full" style={{ background: s.color }} />
          {s.name}
        </span>
      ))}
    </div>
  )
}

function roundedTopRect(x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h)
  return `M${x},${y + h} L${x},${y + rr} Q${x},${y} ${x + rr},${y}`
       + ` L${x + w - rr},${y} Q${x + w},${y} ${x + w},${y + rr}`
       + ` L${x + w},${y + h} Z`
}

function TipRows({ title, segments, total }) {
  return (
    <>
      <div className="mb-1 font-medium">{title}</div>
      {segments.filter((s) => s.value > 0).map((s) => (
        <div key={s.name} className="flex items-center gap-1.5">
          <span className="inline-block size-2 rounded-full" style={{ background: s.color }} />
          <span className="text-muted-foreground">{s.name}</span>
          <span className="ml-auto pl-3 font-medium">{fmtUsd(s.value)}</span>
        </div>
      ))}
      <div className="mt-1 border-t border-border pt-1 text-right font-semibold">
        {fmtUsd(total)}
      </div>
    </>
  )
}

// Colonne verticali impilate (SVG a mano, niente dipendenze): serve sia la
// "spesa per giorno per utente" (categorie = giorni) sia la "spesa per utente
// per modello" (categorie = utenti). Tooltip col dettaglio, legenda sotto.
// categories: [{key, label, segments: [{name, color, value}], total}]
export function StackedColumns({ categories, series, ariaLabel }) {
  const [tip, setTip] = useState(null)     // {i, leftPct}
  const { t } = useTranslation('costs')
  const W = 800; const H = 240
  const M = { top: 10, right: 8, bottom: 24, left: 48 }
  const plotW = W - M.left - M.right
  const plotH = H - M.top - M.bottom
  const maxTotal = Math.max(...categories.map((d) => d.total), 1e-9)
  const slot = plotW / Math.max(categories.length, 1)
  const barW = Math.min(slot * 0.72, 44)
  const yTicks = 4
  const tickStep = Math.ceil(categories.length / 8)

  if (!categories.length || maxTotal <= 1e-9) {
    return <p className="py-10 text-center text-sm text-muted-foreground">
      {t('shared.noSpend')}
    </p>
  }

  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img"
           aria-label={ariaLabel}>
        {Array.from({ length: yTicks + 1 }, (_, t) => {
          const y = M.top + plotH - (t / yTicks) * plotH
          const val = (t / yTicks) * maxTotal
          return (
            <g key={t}>
              <line x1={M.left} x2={W - M.right} y1={y} y2={y}
                    stroke="var(--border)" strokeWidth={t === 0 ? 1.2 : 0.8} />
              <text x={M.left - 6} y={y + 3.5} textAnchor="end" fontSize="10.5"
                    fill="var(--muted-foreground)">{fmtUsd(val)}</text>
            </g>
          )
        })}
        {categories.map((d, i) => {
          const x = M.left + i * slot + (slot - barW) / 2
          let yCursor = M.top + plotH
          const rects = []
          d.segments.forEach((seg, si) => {
            if (seg.value <= 0) return
            const h = (seg.value / maxTotal) * plotH
            yCursor -= h
            const isTop = si === d.segments.length - 1
              || d.segments.slice(si + 1).every((s) => s.value <= 0)
            const gh = Math.max(h - 2, 0.75)          // 2px di aria tra segmenti
            rects.push(isTop
              ? <path key={seg.name} d={roundedTopRect(x, yCursor, barW, gh, 3)}
                      fill={seg.color} />
              : <rect key={seg.name} x={x} y={yCursor} width={barW} height={gh}
                      fill={seg.color} />)
          })
          return (
            <g key={d.key}>
              {rects}
              {i % tickStep === 0 && (
                <text x={M.left + i * slot + slot / 2} y={H - 8} textAnchor="middle"
                      fontSize="10.5" fill="var(--muted-foreground)">{d.label}</text>
              )}
              <rect x={M.left + i * slot} y={M.top} width={slot} height={plotH}
                    fill="transparent"
                    onMouseEnter={() => setTip({ i, leftPct: (M.left + i * slot + slot / 2) / W * 100 })}
                    onMouseLeave={() => setTip(null)} />
            </g>
          )
        })}
      </svg>
      {tip && (
        <div className="pointer-events-none absolute top-1 z-10 -translate-x-1/2 rounded-md border border-border bg-popover px-3 py-2 text-xs shadow-md"
             style={{ left: `${tip.leftPct}%` }}>
          <TipRows title={categories[tip.i].label}
                   segments={categories[tip.i].segments}
                   total={categories[tip.i].total} />
        </div>
      )}
      <Legend series={series} />
    </div>
  )
}

// Barre orizzontali impilate: la "spesa per modello", spaccata per utente.
// rows: [{key, label, segments, total, hint}]
export function StackedBars({ rows, series }) {
  const [tip, setTip] = useState(null)     // indice riga
  const { t } = useTranslation('costs')
  if (!rows.length) {
    return <p className="py-10 text-center text-sm text-muted-foreground">
      {t('shared.noSpend')}
    </p>
  }
  const max = Math.max(...rows.map((r) => r.total), 1e-9)
  return (
    <div className="relative">
      <div className="flex flex-col gap-2">
        {rows.map((r, i) => (
          <div key={r.key} className="flex items-center gap-3 text-sm"
               onMouseEnter={() => setTip(i)} onMouseLeave={() => setTip(null)}>
            <span className="w-56 shrink-0 truncate text-right text-muted-foreground"
                  title={r.label}>{r.label}</span>
            <div className="flex h-4 flex-1 items-stretch gap-[2px]">
              {r.segments.filter((s) => s.value > 0).map((s, si, arr) => (
                <span key={s.name}
                      className={si === arr.length - 1 ? 'rounded-r-[4px]' : ''}
                      style={{ background: s.color,
                               width: `${Math.max(s.value / max * 100, 0.5)}%` }} />
              ))}
              <span className="flex items-center gap-2 whitespace-nowrap pl-2">
                <span className="font-medium">{fmtUsd(r.total)}</span>
                {r.hint && <span className="text-xs text-muted-foreground">{r.hint}</span>}
              </span>
            </div>
          </div>
        ))}
      </div>
      {tip != null && (
        <div className="pointer-events-none absolute left-60 z-10 rounded-md border border-border bg-popover px-3 py-2 text-xs shadow-md"
             style={{ top: `${(tip + 1) / rows.length * 100}%` }}>
          <TipRows title={rows[tip].label} segments={rows[tip].segments}
                   total={rows[tip].total} />
        </div>
      )}
      <Legend series={series} />
    </div>
  )
}

// --- Export ------------------------------------------------------------------

function download(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

function exportCsv(name, headers, rows) {
  const esc = (v) => {
    const s = String(v ?? '')
    return /[",;\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  // ; come separatore e BOM: è quello che Excel in italiano si aspetta
  const text = [headers, ...rows].map((r) => r.map(esc).join(';')).join('\r\n')
  download(new Blob(['﻿' + text], { type: 'text/csv;charset=utf-8' }),
           `${name}.csv`)
}

async function exportXlsx(name, headers, rows, sheetName) {
  // import dinamico: SheetJS pesa, la pagina non lo paga finché non si esporta
  const XLSX = await import('xlsx')
  const ws = XLSX.utils.aoa_to_sheet([headers, ...rows])
  const wb = XLSX.utils.book_new()
  XLSX.utils.book_append_sheet(wb, ws, sheetName)
  XLSX.writeFile(wb, `${name}.xlsx`)
}

// Card di un grafico con vista commutabile grafico <-> tabella e export
// CSV/XLSX degli STESSI dati della tabella (numeri grezzi, non formattati).
// table: {headers: [...], rows: [[...], ...]}
export function ChartCard({ title, subtitle, table, exportName, children }) {
  const [view, setView] = useState('chart')
  const { t } = useTranslation('costs')
  return (
    <section className="rounded-xl border border-border bg-card p-5 shadow-sm">
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-sm font-semibold">{title}</h2>
        {subtitle && <span className="text-xs text-muted-foreground">{subtitle}</span>}
        <div className="ml-auto flex items-center gap-1">
          <Button variant="ghost" size="icon-sm"
                  title={t(view === 'chart' ? 'shared.showTable' : 'shared.showChart')}
                  aria-label={t(view === 'chart' ? 'shared.showTable' : 'shared.showChart')}
                  className="text-muted-foreground"
                  onClick={() => setView(view === 'chart' ? 'table' : 'chart')}>
            {view === 'chart' ? <Table2 /> : <ChartColumn />}
          </Button>
          <Button variant="ghost" size="icon-sm" title={t('shared.exportCsv')}
                  aria-label={t('shared.exportCsvAria', { title })}
                  className="text-muted-foreground"
                  onClick={() => exportCsv(exportName, table.headers, table.rows)}>
            <Download />
          </Button>
          <Button variant="ghost" size="icon-sm" title={t('shared.exportXlsx')}
                  aria-label={t('shared.exportXlsxAria', { title })}
                  className="text-muted-foreground"
                  onClick={() => exportXlsx(exportName, table.headers, table.rows,
                                            t('shared.sheetName'))}>
            <FileSpreadsheet />
          </Button>
        </div>
      </div>
      {view === 'chart' ? children : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-xs uppercase tracking-wide text-muted-foreground">
                {table.headers.map((h, i) => (
                  <th key={h} className={`py-2 pr-4 font-medium ${i ? 'text-right' : 'text-left'}`}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((r, ri) => (
                <tr key={ri} className="border-b border-border/60 last:border-0">
                  {r.map((v, ci) => (
                    <td key={ci}
                        className={`py-1.5 pr-4 ${ci ? 'text-right tabular-nums' : 'text-left'}`}>
                      {typeof v === 'number' ? fmtUsd(v) : v}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
