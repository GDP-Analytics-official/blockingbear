import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Trans, useTranslation } from 'react-i18next'
import { useMediaQuery } from '@/lib/useMediaQuery'
import { hasFixedLayout, pairFixedBoxes, readPagePosition, restorePagePosition } from '@/lib/previewLayout'
import { loadPreviewPage } from '@/lib/previewRequests'
import { usePreviewVisibility } from '@/lib/usePreviewVisibility'

// I formati in cui la preview coincide geometricamente col file esportato:
// solo qui si può SIGILLARE un'area (il rettangolo nero che rimuove davvero
// il contenuto sottostante). Negli altri formati la preview è una
// conversione LibreOffice con impaginazione propria.
const SEALABLE_EXT_RE = /\.(pdf|png|jpe?g|bmp|gif|tiff?|webp)$/i

// quanti box di entità ci sono in tutto un lato ({pagina: [box]})
function countBoxes(byPage) {
  return Object.values(byPage || {})
    .reduce((tot, list) => tot + (list?.length || 0), 0)
}

/* ANCORE per lo scroll sincronizzato. Nei formati convertiti (docx, txt,
   xlsx...) il lato anonimizzato rifluisce: un segnaposto è più corto o più
   lungo del valore che sostituisce e da lì in giù le righe si spostano, a
   volte cambia anche il numero di pagine. Una proporzione globale tra le
   due colonne non basta. Il backend però dà già le coppie: il box del
   VALORE a sinistra e quello del SEGNAPOSTO a destra portano lo stesso
   `ph`, quindi la i-esima occorrenza di ogni segnaposto sui due lati è lo
   stesso punto del documento. Ogni coppia è un'ancora {o, a}, con `n` la
   pagina, `y` la posizione verticale come frazione dell'altezza pagina e
   `keys` le chiavi "pagina:indice" dei box che la compongono (le stesse
   coppie servono a evidenziare il box gemello, vedi hotTwins).

   Un'occorrenza può avere PIÙ box: un valore su più righe, o a cavallo di
   due pagine, ne ha uno per riga. Il backend numera le occorrenze (`occ`,
   uguale per tutti i box dello stesso match) e qui si raggruppa su quello;
   un box senza numero (box OCR delle immagini, descrittori salvati prima
   di questa numerazione) è un'occorrenza a sé. */
function pairAnchors(boxesO, boxesA, sizesO, sizesA) {
  const flat = (byPage, sizes) => {
    const groups = new Map()
    Object.entries(byPage || {}).forEach(([p, list]) => {
      const n = Number(p)
      const size = sizes?.[n]
      if (!size) return
      ;(list || []).forEach((b, i) => {
        if (b.sealed || !b.ph) return
        const key = `${n}:${i}`
        const y = (b.y0 + b.y1) / 2 / size.height
        const gid = b.occ != null ? `o${b.occ}` : key
        const g = groups.get(gid)
        if (!g) {
          groups.set(gid, { n, y, ph: b.ph, keys: [key] })
        } else {
          g.keys.push(key)
          // l'ancora è il box più in alto dell'occorrenza
          if (n < g.n || (n === g.n && y < g.y)) { g.n = n; g.y = y }
        }
      })
    })
    return [...groups.values()].sort((p, q) => p.n - q.n || p.y - q.y)
  }
  const byPh = new Map()
  flat(boxesA, sizesA).forEach((b) => {
    if (!byPh.has(b.ph)) byPh.set(b.ph, [])
    byPh.get(b.ph).push(b)
  })
  const used = new Map()
  const pairs = []
  flat(boxesO, sizesO).forEach((o) => {
    const list = byPh.get(o.ph)
    const i = used.get(o.ph) || 0
    if (!list || i >= list.length) return
    used.set(o.ph, i + 1)
    pairs.push({ o, a: list[i] })
  })
  return pairs
}

/* Le ancore come punti (ySrc, yDst) in coordinate di scroll, letti dal DOM
   adesso (le pagine caricano in tempi diversi e l'altezza vera si sa solo
   così), in ordine crescente su entrambi gli assi: le coppie fuori ordine
   si saltano. Il primo punto è (0, 0): prima della prima entità i due lati
   sono identici. */
function anchorPoints(src, dst, anchors, reverse) {
  const abs = (cont, top, pages, b) => {
    const el = pages[b.n]
    if (!el) return null
    const r = el.getBoundingClientRect()
    return r.top - top + cont.scrollTop + b.y * r.height
  }
  const sTop = src.getBoundingClientRect().top
  const dTop = dst.getBoundingClientRect().top
  const pts = [[0, 0]]
  for (const { o, a } of anchors) {
    const ys = abs(src, sTop, src.children, reverse ? a : o)
    const yd = abs(dst, dTop, dst.children, reverse ? o : a)
    if (ys == null || yd == null) continue
    const [ps, pd] = pts[pts.length - 1]
    if (ys <= ps || yd <= pd) continue
    pts.push([ys, yd])
  }
  return pts
}

/* Posizione su `dst` del punto `y` di `src`, INTERPOLATA tra un'ancora e la
   successiva: continua, quindi adatta a guidare lo scroll senza salti. Dopo
   l'ultima ancora il contenuto dei due lati è identico e lo scostamento
   resta quello dell'ultima ancora (pendenza 1). */
function interpMap(pts, y) {
  let i = 1
  while (i < pts.length && pts[i][0] < y) i++
  const [x0, y0] = pts[i - 1]
  const [x1, y1] = i < pts.length ? pts[i] : [x0 + 1, y0 + 1]
  return y0 + (y - x0) * (y1 - y0) / Math.max(1, x1 - x0)
}

// la riga di lettura: la sincronizzazione è esatta a questa altezza del
// viewport (chi legge sta un po' sotto il bordo superiore, non sul bordo)
const READ_LINE = 0.3

/* scrollTop da dare a `dst` perché mostri sulla riga di lettura lo stesso
   punto del documento che `src` ha lì. Perché il lato più corto possa
   seguire fino in fondo ogni colonna ha uno spazio vuoto in coda alto
   quanto il viewport (.pages-tail): senza, con una pagina in meno a
   destra, entrambe si fermerebbero al proprio fondo e l'ultimo tratto
   resterebbe sfalsato. Senza ancore i due lati scorrono insieme. */
function anchorScroll(src, dst, anchors, reverse) {
  const maxD = Math.max(0, dst.scrollHeight - dst.clientHeight)
  const ref = src.clientHeight * READ_LINE
  const pts = anchorPoints(src, dst, anchors, reverse)
  const t = interpMap(pts, src.scrollTop + ref)
  return Math.min(maxD, Math.max(0, t - ref))
}

/* Una pagina renderizzata + overlay dei box (coordinate PDF -> percentuali).
   Nei popover si mostrano i placeholder così come li produce il motore
   ([FULLNAME_1], ...): nessuna traduzione mantenuta a mano, così un
   aggiornamento del modello (nuovi tag inclusi) non richiede modifiche qui.

   Sul lato ANONIMIZZATO il passaggio del mouse su un segnaposto apre un
   popover con il valore mappato e i bottoni di de-anonimizzazione (singolo
   segnaposto o intera categoria). Su entrambi i lati si può TRASCINARE un
   rettangolo sul testo in chiaro: il testo viene estratto lato server e può
   essere anonimizzato ovunque come [CUSTOM_n] — o come [TAG_n] col tag scritto
   nel popup, se si spunta anche «anonimizza in futuro». */
function Page({ docId, source, n, size, boxes, rev, tooltipFor, highlight,
                mapping, onDeanonymize, onAnonymizeText, columns,
                onColumnAction, services, sealMode, nPages, onSealArea,
                onRemoveSeal, onReprocessOcr, hotKeys, onHot, touchSelect, zoom = 100, visible = true }) {
  const [url, setUrl] = useState(null)
  const [err, setErr] = useState('')
  const [sel, setSel] = useState(null)          // {x0,y0,x1,y1} frazioni 0..1
  const [selInfo, setSelInfo] = useState(null)  // {busy} | {text} | {error}
  // popover di colonna (click su un'intestazione A/B/C): {col, sheet, x, y,
  // busy} -> + {info} | {error} quando column-info risponde
  const [colPop, setColPop] = useState(null)
  const pageRef = useRef(null)
  const nearby = usePreviewVisibility(pageRef, visible)
  const dragRef = useRef(null)
  const dragCleanup = useRef(null)
  const [activeBox, setActiveBox] = useState(null)
  useEffect(() => () => dragCleanup.current?.(), [])
  const { t } = useTranslation('viewer')

  useEffect(() => {
    let alive = true
    let objUrl = null
    setUrl(null)
    setErr('')
    if (!nearby || !visible) return undefined
    const controller = new AbortController()
    loadPreviewPage((signal) => services.fetchPagePng(docId, source, n, rev, signal), controller.signal)
      .then((u) => { objUrl = u; if (alive) setUrl(u); else URL.revokeObjectURL(u) })
      .catch((e) => alive && setErr(e.status === 410
        ? t('page.originalGone') : e.message))
    return () => {
      alive = false
      controller.abort()
      if (objUrl) URL.revokeObjectURL(objUrl)
    }
  }, [docId, source, n, rev, nearby, visible])   // eslint-disable-line react-hooks/exhaustive-deps

  // il documento è cambiato (ri-redazione): via selezione e popover pendenti
  useEffect(() => { setSel(null); setSelInfo(null); setColPop(null); setActiveBox(null) }, [rev])

  function relPoint(e) {
    const r = pageRef.current.getBoundingClientRect()
    return {
      x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
      y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
    }
  }

  // click su un'intestazione di colonna: popover con i conteggi (column-info
  // legge il file COMPLETO, non la preview troncata) e le due azioni
  async function openColPop(c) {
    setSel(null)
    setSelInfo(null)
    setColPop({ col: c.col, sheet: columns.sheet, busy: true,
                x: c.x0 / size.width, y: c.y1 / size.height })
    try {
      const info = await services.columnInfo(docId, columns.sheet, c.col)
      setColPop((p) => (p && p.col === c.col ? { ...p, busy: false, info } : p))
    } catch (e2) {
      setColPop((p) => (p && p.col === c.col
        ? { ...p, busy: false, error: e2.message } : p))
    }
  }

  function onPointerDown(e) {
    if (e.button !== 0 || !e.isPrimary || !url || err) return
    if (e.pointerType === 'touch' && !touchSelect) return
    if (e.target.closest('.box') || e.target.closest('.selpop') ||
        e.target.closest('.colstrip') || e.target.closest('.colpop')) return
    e.preventDefault()
    dragCleanup.current?.()
    const pointerId = e.pointerId
    e.currentTarget.setPointerCapture(pointerId)
    setActiveBox(null)
    const start = relPoint(e)
    dragRef.current = { start, moved: false }
    setSel(null)
    setSelInfo(null)
    setColPop(null)
    const rect = (p) => ({
      x0: Math.min(start.x, p.x), y0: Math.min(start.y, p.y),
      x1: Math.max(start.x, p.x), y1: Math.max(start.y, p.y),
    })
    const move = (ev) => {
      if (ev.pointerId !== pointerId) return
      const d = dragRef.current
      if (!d) return
      const p = relPoint(ev)
      if (Math.abs(p.x - start.x) > 0.005 || Math.abs(p.y - start.y) > 0.005) d.moved = true
      if (d.moved) setSel(rect(p))
    }
    const up = async (ev) => {
      if (ev.pointerId !== pointerId) return
      dragCleanup.current?.()
      const d = dragRef.current
      dragRef.current = null
      if (!d?.moved) { setSel(null); return }
      const r = rect(relPoint(ev))
      setSel(r)
      // modalità sigillo: niente estrazione di testo, il popover chiede
      // conferma della rimozione dell'area (il rettangolo vale così com'è)
      if (sealMode) { setSelInfo({ seal: true }); return }
      setSelInfo({ busy: true })
      try {
        const res = await services.extractText(docId, source, n,
          [r.x0 * size.width, r.y0 * size.height, r.x1 * size.width, r.y1 * size.height])
        // res.ocr: il layer testuale era vuoto e il testo arriva dall'OCR
        // (dalla cache dell'allegato, o dal ritaglio se la cache non copre
        // l'area). res.ocr_redactable === false: la redazione non saprebbe
        // coprire il valore nei pixel — senza cache (res.ocr_cache false)
        // serve la rielaborazione con OCR; con la cache (res.ocr_cache true)
        // il ritaglio ha letto qualcosa che la cache non ha, e l'utente deve
        // allargare la selezione (parole/riga intera)
        if (res.text) {
          const unredactable = !!res.ocr && res.ocr_redactable === false
          setSelInfo({ text: res.text, ocr: !!res.ocr,
                       noCache: unredactable && !res.ocr_cache,
                       notInCache: unredactable && !!res.ocr_cache })
        } else {
          setSelInfo({ error: t(res.ocr_tried ? 'selection.noTextOcr'
                                              : 'selection.noText') })
        }
      } catch (e2) {
        setSelInfo({ error: e2.message })
      }
    }
    const cancel = () => {
      dragRef.current = null
      setSel(null)
      setSelInfo(null)
      dragCleanup.current?.()
    }
    dragCleanup.current = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', cancel)
      dragCleanup.current = null
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', cancel)
  }

  const pct = (v, tot) => `${(v / tot) * 100}%`
  const fr = (v) => `${v * 100}%`
  return (
    <div className={'page' + (touchSelect ? ' page-select' : '')} ref={pageRef}
         data-page-index={n} data-source={source}
         style={{ width: `${zoom}%`, aspectRatio: `${size.width} / ${size.height}` }} onPointerDown={onPointerDown}
         onClick={(e) => { if (!e.target.closest('.box')) setActiveBox(null) }}>
      {err ? <div className="page-missing" role="status">{err}</div>
           : url ? <img src={url} alt={t('page.alt', { n: n + 1 })} draggable={false}
                        onError={() => setErr(t('page.loadError'))} />
                 : <div className="page-loading" />}
      {url && !err && (boxes || []).map((b, i) => (
        <div
          key={i}
          className={'box' + (b.sealed ? ' box-sealed'
                     : highlight ? ' box-yellow' : ' box-outline')
                     + (hotKeys?.has(`${n}:${i}`) ? ' box-twin' : '')
                     + (activeBox === i ? ' box-active' : '')}
          tabIndex={0} role="button" aria-label={tooltipFor(b)}
          aria-expanded={activeBox === i}
          onClick={(e) => { if (!e.target.closest('.boxpop')) setActiveBox((v) => v === i ? null : i) }}
          onKeyDown={(e) => {
            if (e.target !== e.currentTarget) return
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setActiveBox((v) => v === i ? null : i) }
            if (e.key === 'Escape') setActiveBox(null)
          }}
          onMouseEnter={() => onHot?.({ side: source, key: `${n}:${i}` })}
          onMouseLeave={() => onHot?.(null)}
          style={{
            left: pct(b.x0, size.width),
            top: pct(b.y0, size.height),
            width: pct(b.x1 - b.x0, size.width),
            height: pct(b.y1 - b.y0, size.height),
          }}
          title={(onDeanonymize || b.sealed) ? undefined : tooltipFor(b)}
        >
          {!onDeanonymize && !b.sealed && <div className="boxpop">{tooltipFor(b)}</div>}
          {b.sealed ? (onRemoveSeal && (
            <div className="boxpop" style={b.x0 / size.width > 0.5 ? { left: 'auto', right: 0 } : undefined} onPointerDown={(e) => e.stopPropagation()}>
              <div className="boxpop-map">
                <b>{t('box.sealed')}</b>
                <div className="boxpop-ocr">{t('box.sealedHelp')}</div>
              </div>
              <div className="pop-actions">
                <button className="ghost small"
                        onClick={() => onRemoveSeal(b.sealed)}>
                  {t('box.removeSeal')}
                </button>
              </div>
            </div>
          )) : onDeanonymize && (
            <div className="boxpop" style={b.x0 / size.width > 0.5 ? { left: 'auto', right: 0 } : undefined} onPointerDown={(e) => e.stopPropagation()}>
              <div className="boxpop-map">
                <code>{b.ph}</code> = {mapping?.[b.ph] ?? '?'}
                {b.ocr && <div className="boxpop-ocr">{t('box.ocr')}</div>}
              </div>
              <div className="pop-actions">
                <button className="ghost small"
                        onClick={() => onDeanonymize({ placeholder: b.ph })}>
                  {t('box.deanonOne', { placeholder: b.ph })}
                </button>
                <button className="ghost small"
                        onClick={() => onDeanonymize({ label: b.label })}>
                  {t('box.deanonAll', { label: b.label })}
                </button>
              </div>
            </div>
          )}
        </div>
      ))}
      {url && !err && columns && onColumnAction && columns.cols.map((c) => (
        <div key={c.col}
             className={'colstrip' + (colPop?.col === c.col ? ' on' : '')}
             style={{
               left: pct(c.x0, size.width),
               top: pct(c.y0, size.height),
               width: pct(c.x1 - c.x0, size.width),
               height: pct(c.y1 - c.y0, size.height),
             }}
             title={t('column.stripTitle', { col: c.col, sheet: columns.sheet })}
             onPointerDown={(e) => e.stopPropagation()}
             onClick={() => openColPop(c)} />
      ))}
      {colPop && (
        <div className="selpop colpop"
             style={{
               top: fr(colPop.y),
               // vicino al bordo destro il popover si aggancia a destra, per
               // non farsi tagliare dallo scroll della colonna
               ...(colPop.x > 0.65 ? { right: fr(1 - colPop.x) } : { left: fr(colPop.x) }),
             }}
             onPointerDown={(e) => e.stopPropagation()}>
          <div className="colpop-title">
            <b>{t('column.popTitle', { col: colPop.col })}</b> — {colPop.sheet}
            {colPop.info?.header
              ? t('column.headerQuoted', { header: colPop.info.header }) : null}
          </div>
          {colPop.busy && (
            <span className="muted">
              <span className="spinner" />{t('column.analysing')}
            </span>
          )}
          {colPop.error && <span className="error">{colPop.error}</span>}
          {colPop.info && (
            <>
              <div className="muted">
                {t('column.counts', { values: colPop.info.values,
                                      mapped: colPop.info.mapped })}
              </div>
              {colPop.info.busy && <div className="error">{t('column.busy')}</div>}
              <div className="pop-actions">
                <button className="primary small" disabled={colPop.info.busy}
                        onClick={() => {
                          const a = { mode: 'anon', sheet: colPop.sheet,
                                      col: colPop.col, info: colPop.info }
                          setColPop(null)
                          onColumnAction(a)
                        }}>
                  {t('column.anonymize')}
                </button>
                <button className="ghost small"
                        disabled={colPop.info.busy || !colPop.info.mapped}
                        onClick={() => {
                          const a = { mode: 'deanon', sheet: colPop.sheet,
                                      col: colPop.col, info: colPop.info }
                          setColPop(null)
                          onColumnAction(a)
                        }}>
                  {t('column.deanonymize')}
                </button>
                <button className="ghost small" onClick={() => setColPop(null)}>
                  {t('actions.cancel', { ns: 'common' })}
                </button>
              </div>
            </>
          )}
        </div>
      )}
      {sel && (
        <div className={'selbox' + (sealMode ? ' selbox-seal' : '')}
             style={{ left: fr(sel.x0), top: fr(sel.y0),
                      width: fr(sel.x1 - sel.x0), height: fr(sel.y1 - sel.y0) }} />
      )}
      {sel && selInfo && (
        <div className="selpop" style={{ left: fr(sel.x0), top: fr(sel.y1) }}
             onPointerDown={(e) => e.stopPropagation()}>
          {selInfo.busy && (
            <span className="muted">
              <span className="spinner" />{t('selection.extracting')}
            </span>
          )}
          {selInfo.error && <span className="error">{selInfo.error}</span>}
          {selInfo.seal && (
            <>
              <div className="selpop-caption">{t('seal.caption')}</div>
              <div className="seal-note">
                <Trans i18nKey="seal.note" ns="viewer" components={{ b: <b /> }} />
              </div>
              {nPages > 1 && (
                <label className="selpop-save">
                  <input type="checkbox" checked={!!selInfo.all}
                         onChange={(e) => setSelInfo((s) => ({ ...s, all: e.target.checked }))} />
                  {t('seal.allPages')}
                </label>
              )}
              <div className="pop-actions">
                <button className="danger small"
                        onClick={() => onSealArea(n,
                          [sel.x0 * size.width, sel.y0 * size.height,
                           sel.x1 * size.width, sel.y1 * size.height],
                          !!selInfo.all)}>
                  {t(selInfo.all ? 'seal.applyAll' : 'seal.apply')}
                </button>
                <button className="ghost small"
                        onClick={() => { setSel(null); setSelInfo(null) }}>
                  {t('actions.cancel', { ns: 'common' })}
                </button>
              </div>
            </>
          )}
          {selInfo.text != null && (
            <>
              <div className="selpop-caption">{t('selection.caption')}</div>
              {selInfo.ocr && (
                <div className="selpop-ocr">{t('selection.ocr')}</div>
              )}
              <textarea
                className="selpop-edit"
                rows={Math.min(5, Math.max(2, Math.ceil(selInfo.text.length / 45)))}
                value={selInfo.text}
                onChange={(e) => setSelInfo((s) => ({ ...s, text: e.target.value }))}
              />
              {selInfo.notInCache && (
                /* la cache OCR c'è ma non contiene questa lettura: la
                   redazione nei pixel non la coprirebbe */
                <div className="seal-note">{t('selection.notInOcrCache')}</div>
              )}
              {selInfo.noCache ? (
                /* file elaborato SENZA OCR: il termine non sarebbe coperto
                   nei pixel — la via giusta è rielaborare con l'OCR attivo */
                <>
                  <div className="seal-note">
                    <Trans i18nKey="selection.noOcrCache" ns="viewer"
                           components={{ b: <b /> }} />
                    {t(onReprocessOcr ? 'selection.noOcrReprocess'
                                      : 'selection.noOcrResend')}
                  </div>
                  <div className="pop-actions">
                    {onReprocessOcr && (
                      <button className="primary small"
                              onClick={() => {
                                onReprocessOcr()
                                setSel(null)
                                setSelInfo(null)
                              }}>
                        {t('selection.reprocess')}
                      </button>
                    )}
                    <button className="ghost small"
                            onClick={() => { setSel(null); setSelInfo(null) }}>
                      {t('actions.cancel', { ns: 'common' })}
                    </button>
                  </div>
                </>
              ) : (
                <>
                  {/* il termine finisce nella lista PERSONALE di chi clicca
                      (non in quella globale dell'amministratore): l'etichetta
                      deve dirlo, o «in futuro» si legge come «per tutti» */}
                  <label className="selpop-save">
                    <input type="checkbox" checked={!!selInfo.save}
                           onChange={(e) => setSelInfo((s) => ({ ...s, save: e.target.checked }))} />
                    {t('selection.saveTerm')}
                  </label>
                  {selInfo.save && (
                    <>
                      <label className="selpop-tag">
                        {t('selection.withTag')}
                        <input value={selInfo.tag ?? 'CUSTOM'} spellCheck={false}
                               onChange={(e) => setSelInfo((s) => ({ ...s, tag: e.target.value }))} />
                      </label>
                      <div className="selpop-hint">{t('selection.tagHint')}</div>
                    </>
                  )}
                  <div className="pop-actions">
                    <button className="primary small"
                            disabled={!selInfo.text.trim()
                              || (selInfo.save && !(selInfo.tag ?? 'CUSTOM').trim())}
                            onClick={() => onAnonymizeText(selInfo.text.trim(),
                              !!selInfo.save, (selInfo.tag ?? 'CUSTOM').trim())}>
                      {t('selection.anonymize')}
                    </button>
                    <button className="ghost small"
                            onClick={() => { setSel(null); setSelInfo(null) }}>
                      {t('actions.cancel', { ns: 'common' })}
                    </button>
                  </div>
                </>
              )}
            </>
          )}
        </div>
      )}
      <div className="page-num">{n + 1}</div>
    </div>
  )
}

/* Dialog di conferma in-app (niente window.confirm): stesso linguaggio visivo
   dei modali esistenti, chiusura con click fuori o Esc. */
function ConfirmDialog({ title, children, confirmLabel, onConfirm, onCancel }) {
  const { t } = useTranslation('common')
  const opener = useRef(document.activeElement)
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onCancel() }}>
      <DialogContent hideClose aria-describedby={undefined} className="max-w-md"
                     onCloseAutoFocus={(e) => { e.preventDefault(); opener.current?.focus() }}>
        <DialogTitle>{title}</DialogTitle>
        <div className="confirm-body">{children}</div>
        <div className="confirm-actions">
          <Button variant="outline" onClick={onCancel}>{t('actions.cancel')}</Button>
          <Button variant="destructive" onClick={onConfirm}>{confirmLabel}</Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

/* `services`: gli endpoint dietro al viewer, OBBLIGATORI e iniettati dal
   contenitore — quelli dei file di progetto (ProjectFilePage) o quelli
   dell'anteprima pre-invio della chat (stessi contratti, sul turno in
   anteprima). Un servizio assente = funzione non disponibile in quel
   contesto. Titolo e download stanno nel contenitore.

   `services.columnJobs`: l'anonimizzazione di colonna di QUESTO contesto va
   in coda come job (file di progetto: la risposta è il descrittore del job)
   oppure è sincrona (anteprima della chat: la risposta è il nuovo stato,
   come per ogni altra modifica). Cambia il testo della conferma e chi riceve
   la risposta. */
// Avvertenze informative del pezzo (residui, valori lasciati in chiaro,
// sigilli, preview troncata, colonne tabellari). Esportata perché in
// modalità tab controllata il contenitore (ReviewModal) le mostra nel
// proprio footer dietro un'icona; `t` è quella del namespace 'viewer'.
export function docWarnings(doc, t) {
  const warn = []
  if (doc.report?.residual?.length) {
    warn.push(t('warn.residual', { count: doc.report.residual.length,
                                   values: doc.report.residual.join(', ') }))
  }
  if (doc.report?.skipped?.length) {
    warn.push(t('warn.skipped', { count: doc.report.skipped.length,
                                  values: doc.report.skipped.join(', ') }))
  }
  if (doc.sealed?.length) {
    warn.push(t('warn.sealed', { count: doc.sealed.length }))
  }
  if (doc.report?.preview_truncated) {
    warn.push(t('warn.truncated'))
  }
  if (doc.report?.table_columns?.length) {
    const cols = doc.report.table_columns
      .map((c) => t('warn.tableColumn', { column: c.column, sheet: c.sheet,
                                          values: c.values, tag: c.tag }))
      .join(', ')
    warn.push(t('warn.tableColumns', { columns: cols }))
  }
  return warn
}

// `tab` (opzionale): tab CONTROLLATA dal contenitore — il ReviewModal la
// mette nel proprio footer per non spendere una riga in testa al viewer
// (e lì porta anche le avvertenze, vedi docWarnings); senza la prop, il
// viewer mostra tab in alto e avvertenze inline come sempre.
export default function DocumentViewer({ doc, onChange, onJobStart,
                                         services, tab: tabProp }) {
  const [tabState, setTab] = useState('preview')
  const [mobileSide, setMobileSide] = useState('original')
  const [touchSelect, setTouchSelect] = useState(false)
  const [zoom, setZoom] = useState(100)
  const tab = tabProp ?? tabState
  const [applying, setApplying] = useState(false)
  const [editErr, setEditErr] = useState('')
  const [confirm, setConfirm] = useState(null)   // {title, body, label, action}
  // layout delle colonne cliccabili (solo xlsx): {original: {pagina: {sheet,
  // cols}}, anonymized: {...}} — se l'endpoint fallisce niente overlay e basta
  const [colLayout, setColLayout] = useState(null)
  const leftRef = useRef(null)
  const rightRef = useRef(null)
  const pairedRef = useRef(null)
  const pagePosition = useRef(null)
  const pageViewport = useRef(null)
  const syncing = useRef(false)
  const desktop = useMediaQuery('(min-width: 1024px)')
  const fixedLayout = hasFixedLayout(doc)
  const paired = desktop && fixedLayout
  // box sotto il mouse: {side: 'original'|'anonymized', key: 'pagina:indice'}
  const [hot, setHot] = useState(null)
  // `ext` (quando c'è) è l'estensione EFFETTIVA, post-conversione: un .xls
  // caricato in chat viaggia come .xlsx e le colonne ce le ha
  const isXlsx = doc.ext ? doc.ext === '.xlsx' : /\.xlsx$/i.test(doc.filename)
  const columnJobs = !!services.columnJobs
  // modalità SIGILLO (solo PDF e immagini, e solo dove il servizio esiste):
  // il trascinamento sul lato anonimizzato disegna il rettangolo nero che
  // rimuove il contenuto, invece di estrarre testo
  const canSeal = !!services.sealArea && SEALABLE_EXT_RE.test(doc.filename)
  const [sealMode, setSealMode] = useState(false)
  // il cartellino «niente da anonimizzare» si può chiudere; torna sul pezzo
  // successivo (è un'informazione su QUESTO pezzo, non una notifica globale)
  const [emptyNoteClosed, setEmptyNoteClosed] = useState(false)
  const { t } = useTranslation('viewer')

  useEffect(() => {
    setTab('preview'); setEditErr(''); setConfirm(null); setSealMode(false)
    setEmptyNoteClosed(false); setHot(null)
    setMobileSide('original'); setTouchSelect(false); setZoom(100)
  }, [doc.id])

  useEffect(() => {
    if (!isXlsx || !services.getColumns) { setColLayout(null); return undefined }
    let alive = true
    services.getColumns(doc.id)
      .then((l) => { if (alive) setColLayout(l) })
      .catch(() => { if (alive) setColLayout(null) })
    return () => { alive = false }
  }, [doc.id, doc.rev, isXlsx])

  // scroll sincronizzato tra le due colonne, per ancore (vedi pairAnchors):
  // `reverse` quando a guidare è il lato anonimizzato
  const anchors = useMemo(
    () => fixedLayout
      ? pairFixedBoxes(doc.original_boxes, doc.anonymized_boxes, doc.page_sizes.original)
      : pairAnchors(doc.original_boxes, doc.anonymized_boxes,
                    doc.page_sizes.original, doc.page_sizes.anonymized),
    [doc.original_boxes, doc.anonymized_boxes, doc.page_sizes, fixedLayout])
  // gemelli di ogni box: per lato, chiave del box -> chiavi dei box
  // corrispondenti sull'altro lato (di solito uno; più d'uno se il valore
  // era spezzato su due righe)
  const twins = useMemo(() => {
    const m = { original: new Map(), anonymized: new Map() }
    for (const { o, a } of anchors) {
      o.keys.forEach((k) => m.original.set(k, [...(m.original.get(k) || []), ...a.keys]))
      a.keys.forEach((k) => m.anonymized.set(k, [...(m.anonymized.get(k) || []), ...o.keys]))
    }
    return m
  }, [anchors])
  // le chiavi da evidenziare, per lato: il gemello del box sotto il mouse
  const hotTwins = useMemo(() => {
    const none = { original: null, anonymized: null }
    if (!hot) return none
    const other = hot.side === 'original' ? 'anonymized' : 'original'
    const keys = twins[hot.side].get(hot.key)
    return keys ? { ...none, [other]: new Set(keys) } : none
  }, [hot, twins])

  function rememberPosition(container, resized = false) {
    const previous = pageViewport.current
    // A resize can clamp scrollTop and emit scroll before ResizeObserver
    // restores our reading position. Do not mistake that for user scrolling.
    if (!resized && previous && pagePosition.current?.docId === doc.id
        && (previous.container !== container || previous.width !== container?.clientWidth
          || previous.height !== container?.clientHeight)) return
    const position = readPagePosition(container)
    if (position) {
      pagePosition.current = { docId: doc.id, ...position }
      pageViewport.current = { container, width: container.clientWidth, height: container.clientHeight }
    }
  }

  // Fixed documents share one desktop scroll surface. Keep the same document
  // position when its width changes or the mobile single-pane layout takes over.
  useLayoutEffect(() => {
    if (!fixedLayout || tab !== 'preview') return undefined
    const container = paired ? pairedRef.current
      : mobileSide === 'original' ? leftRef.current : rightRef.current
    if (!container) return undefined
    const restore = () => {
      if (pagePosition.current?.docId === doc.id) {
        restorePagePosition(container, pagePosition.current)
      }
      rememberPosition(container, true)
    }
    restore()
    const observer = new ResizeObserver(restore)
    observer.observe(container)
    return () => observer.disconnect()
  }, [doc.id, doc.rev, fixedLayout, paired, mobileSide, tab, zoom])

  function onScroll(src, dst, reverse) {
    if (fixedLayout) {
      rememberPosition(src.current)
      return
    }
    if (syncing.current || !src.current || !dst.current
        || !src.current.clientWidth || !dst.current.clientWidth) return
    syncing.current = true
    dst.current.scrollTop = anchorScroll(src.current, dst.current, anchors, reverse)
    requestAnimationFrame(() => { syncing.current = false })
  }

  // modifiche alla mappa: il server ri-redige dall'originale e risponde con il
  // descrittore aggiornato, che risale alla pagina del documento (onChange)
  async function applyEdit(fn) {
    setApplying(true)
    setEditErr('')
    try {
      onChange?.(await fn())
    } catch (e) {
      setEditErr(e.message)
    } finally {
      setApplying(false)
    }
  }

  function deanonymize(body) {
    const phs = body.placeholder
      ? [body.placeholder]
      : Object.keys(doc.mapping).filter((ph) => ph.replace(/^\[|\]$/g, '').replace(/_\d+$/, '') === body.label)
    setConfirm({
      title: body.placeholder
        ? t('deanon.titleOne', { placeholder: body.placeholder })
        : t('deanon.titleAll', { label: body.label }),
      label: t('deanon.action'),
      body: (
        <>
          <p>
            {body.placeholder
              ? <Trans i18nKey="deanon.bodyOne" ns="viewer" components={{ b: <b /> }} />
              : <Trans i18nKey="deanon.bodyAll" ns="viewer"
                       values={{ count: phs.length, label: body.label }}
                       components={{ b: <b />, code: <code /> }} />}
          </p>
          <ul className="confirm-list">
            {phs.slice(0, 6).map((ph) => (
              <li key={ph}><code>{ph}</code> = {doc.mapping[ph]}</li>
            ))}
            {phs.length > 6 && (
              <li className="muted">{t('deanon.andMore', { count: phs.length - 6 })}</li>
            )}
          </ul>
        </>
      ),
      action: () => applyEdit(() => services.deanonymize(doc.id, body)),
    })
  }

  const anonymizeText = (text, saveTerm, termTag) =>
    applyEdit(() => services.anonymizeText(doc.id, text, saveTerm, termTag))

  // sigilli: il popover di conferma sta nella Page (come la selezione testo);
  // la rimozione è reversibile (si ri-redige dall'originale), niente dialog
  const sealArea = (page, rect, allPages) =>
    applyEdit(() => services.sealArea(doc.id, page, rect, allPages))
  const removeSeal = (n) => applyEdit(() => services.removeSeal(doc.id, n))

  // rielaborazione con OCR (file elaborato senza, ma la selezione ha letto
  // testo in un'immagine): parte come JOB in coda, stesso flusso dell'upload
  const reprocessOcr = services.reprocessOcr ? () => {
    setEditErr('')
    services.reprocessOcr(doc.id)
      .then((job) => onJobStart?.(job))
      .catch((e) => setEditErr(e.message))
  } : undefined

  // azioni di colonna (click sull'intestazione A/B/C della preview xlsx):
  // l'anonimizzazione è deterministica (ogni valore distinto -> [CUSTOM_n],
  // niente modello) ma parte come JOB in coda perché ri-redazione e anteprima
  // possono durare; la deanonimizzazione è sincrona come per i segnaposto
  function columnAction({ mode, sheet, col, info }) {
    // la posizione («colonna B del foglio Dati») è un frammento riusato in due
    // frasi: si compone da una chiave sola, e in inglese può cambiare ordine
    const whereKey = info.header ? 'column.whereHeader' : 'column.where'
    const whereVals = { col, sheet, header: info.header }
    if (mode === 'anon') {
      setConfirm({
        title: t('column.anonTitle', { col, sheet }),
        label: t('column.anonymize'),
        body: (
          <>
            <p>
              <Trans i18nKey="column.anonBody" ns="viewer"
                     values={{
                       where: t(whereKey, whereVals),
                       values: info.values,
                       mapped: info.mapped > 0
                         ? t('column.anonBodyMapped', { count: info.mapped }) : '',
                     }}
                     components={{ b: <b /> }} />
            </p>
            <p>
              <Trans i18nKey="column.anonHow" ns="viewer"
                     components={{ b: <b />, code: <code /> }} />
              {t(columnJobs ? 'column.anonHowJob' : 'column.anonHowSync')}
            </p>
          </>
        ),
        action: async () => {
          if (!columnJobs) {
            // sincrona: la risposta È il nuovo stato, come per le altre
            // modifiche (applyEdit mostra l'avviso "ri-redigo…")
            await applyEdit(() => services.anonymizeColumn(doc.id, sheet, col))
            return
          }
          setEditErr('')
          try {
            onJobStart?.(await services.anonymizeColumn(doc.id, sheet, col))
          } catch (e) {
            setEditErr(e.message)
          }
        },
      })
    } else {
      setConfirm({
        title: t('column.deanonTitle', { col, sheet }),
        label: t('column.deanonymize'),
        body: (
          <p>
            <Trans i18nKey="column.deanonBody" ns="viewer"
                   values={{ count: info.mapped, where: t(whereKey, whereVals) }}
                   components={{ b: <b /> }} />
          </p>
        ),
        action: () => applyEdit(() => services.deanonymizeColumn(doc.id, sheet, col)),
      })
    }
  }

  // dimensioni per lato: nei formati a testo fluido (docx) l'anonimizzato
  // può avere un numero di pagine diverso dall'originale
  const sizesO = doc.page_sizes.original
  const sizesA = doc.page_sizes.anonymized
  // NIENTE da anonimizzare: nessun segnaposto in mappa, nessun box su nessuno
  // dei due lati, nessuna area sigillata. `skipped` esclude il caso opposto
  // (valori TROVATI ma lasciati in chiaro perché troppo corti): lì un
  // «non c'è niente» sarebbe una bugia, e l'avviso giallo lo dice già.
  const nothingToAnonymize =
    !Object.keys(doc.mapping || {}).length
    && !countBoxes(doc.original_boxes) && !countBoxes(doc.anonymized_boxes)
    && !doc.sealed?.length && !doc.report?.skipped?.length
  const warn = docWarnings(doc, t)

  function renderPage(source, n) {
    const original = source === 'original'
    return (
      <Page key={n} touchSelect={touchSelect} zoom={paired ? 100 : zoom}
            visible={desktop || mobileSide === source}
            docId={doc.id} source={source} n={n} rev={doc.rev || 0}
            size={(original ? sizesO : sizesA)[n]}
            boxes={(original ? doc.original_boxes : doc.anonymized_boxes)[n]}
            highlight={original} mapping={doc.mapping} services={services}
            tooltipFor={(b) => (b.sealed ? t('box.sealed')
              : original ? t('box.tooltipOriginal', { placeholder: b.ph })
              : t('box.tooltipAnon', { placeholder: b.ph, value: doc.mapping[b.ph] ?? '?' }))
              + (b.ocr ? t('box.ocrSuffix') : '')}
            columns={colLayout?.[source]?.[n]} onColumnAction={columnAction}
            onAnonymizeText={anonymizeText} onReprocessOcr={reprocessOcr}
            onDeanonymize={original ? undefined : deanonymize}
            hotKeys={hotTwins[source]} onHot={setHot}
            sealMode={!original && canSeal && sealMode} nPages={sizesA.length}
            onSealArea={!original && canSeal ? sealArea : undefined}
            onRemoveSeal={!original && canSeal ? removeSeal : undefined} />
    )
  }

  function renderTitle(source) {
    const original = source === 'original'
    return (
      <h3>{t(original ? 'pane.original' : 'pane.anonymized')}{' '}
        <span className="pane-hint">
          {t(original ? 'pane.originalHint' : sealMode ? 'pane.sealHint' : 'pane.anonymizedHint')}
        </span>
        {!original && canSeal && (
          <button className={'sealtoggle' + (sealMode ? ' on' : '')}
                  title={t('pane.sealToggleHint')}
                  onClick={() => { setSealMode((m) => !m); setTouchSelect(true) }}>
            {t(sealMode ? 'pane.sealToggleOn' : 'pane.sealToggle')}
          </button>
        )}
      </h3>
    )
  }

  return (
    <div className="viewer" data-mobile-side={mobileSide} data-fixed-layout={fixedLayout}>
      {tabProp == null && (
        <div className="viewer-head">
          <div className="viewer-actions">
            <div className="tabs">
              <button className={tab === 'preview' ? 'on' : ''}
                      onClick={() => setTab('preview')}>{t('tab.compare')}</button>
              <button className={tab === 'mapping' ? 'on' : ''}
                      onClick={() => setTab('mapping')}>
                {t('tab.mapping', { count: Object.keys(doc.mapping).length })}
              </button>
            </div>
          </div>
        </div>
      )}

      {tabProp == null
        && warn.map((w, i) => <div className="warning" key={i}>{w}</div>)}
      {applying && (
        <div className="warning"><span className="spinner" /> {t('applying')}</div>
      )}
      {/* job in corso su questo file (rielaborazione con OCR, colonna): il
          contenitore ricarica il descrittore da solo quando finisce */}
      {doc.busy && (
        <div className="warning"><span className="spinner" /> {t('busy')}</div>
      )}
      {editErr && <div className="error">{editErr}</div>}

      {tab === 'preview' && (
        <div className="viewer-mobile-tools">
          <div className="tabs" aria-label={t('tab.compare')}>
            <button type="button" className={mobileSide === 'original' ? 'on' : ''}
                    aria-pressed={mobileSide === 'original'} onClick={() => setMobileSide('original')}>
              {t('pane.original')}
            </button>
            <button type="button" className={mobileSide === 'anonymized' ? 'on' : ''}
                    aria-pressed={mobileSide === 'anonymized'} onClick={() => setMobileSide('anonymized')}>
              {t('pane.anonymized')}
            </button>
          </div>
          <div className="viewer-touch-tools">
            <button type="button" className={touchSelect ? 'primary' : 'ghost'}
                    aria-pressed={touchSelect} onClick={() => setTouchSelect((v) => !v)}>
              {t(touchSelect ? 'mobile.selecting' : 'mobile.select')}
            </button>
            <div className="viewer-zoom">
              <button type="button" className="ghost" disabled={zoom <= 100}
                      aria-label={t('mobile.zoomOut')} onClick={() => setZoom((z) => Math.max(100, z - 25))}>−</button>
              <span aria-live="polite">{zoom}%</span>
              <button type="button" className="ghost" disabled={zoom >= 250}
                      aria-label={t('mobile.zoomIn')} onClick={() => setZoom((z) => Math.min(250, z + 25))}>+</button>
            </div>
          </div>
        </div>
      )}
      {tab === 'preview' && (
        <div className={'compare' + (paired ? ' compare-fixed' : '') + (applying ? ' applying' : '')}>
          {/* nessuna entità: un cartellino al centro, SOPRA le due anteprime.
              pointer-events: none tranne la «x» — sotto si continua a poter
              trascinare per anonimizzare a mano quello che il rilevatore non
              considera PII, e chi vuole vedere le pagine lo chiude */}
          {nothingToAnonymize && !emptyNoteClosed && (
            <div className="nothing-anon">
              <div className="nothing-anon-card">
                <b>{t('nothing')}</b>
                <button type="button" title={t('actions.close', { ns: 'common' })}
                        onClick={() => setEmptyNoteClosed(true)}>×</button>
              </div>
            </div>
          )}
          {paired ? (
            <>
              <div className="pane pane-original">{renderTitle('original')}</div>
              <div className="pane pane-anonymized">{renderTitle('anonymized')}</div>
              <div className="pages pages-paired" ref={pairedRef}
                   role="region" aria-label={t('tab.compare')} tabIndex={0}
                   onScroll={() => rememberPosition(pairedRef.current)}>
                {sizesO.map((_size, n) => (
                  <div className="page-pair" key={n} data-page-index={n}
                       style={{ width: `${zoom}%` }}>
                    {renderPage('original', n)}
                    {renderPage('anonymized', n)}
                  </div>
                ))}
              </div>
            </>
          ) : (
            <>
              <section className="pane pane-original">
                {renderTitle('original')}
                <div className="pages" ref={leftRef} onScroll={() => onScroll(leftRef, rightRef)}>
                  {sizesO.map((_size, n) => renderPage('original', n))}
                  <div className="pages-tail" />
                </div>
              </section>
              <section className="pane pane-anonymized">
                {renderTitle('anonymized')}
                <div className="pages" ref={rightRef} onScroll={() => onScroll(rightRef, leftRef, true)}>
                  {sizesA.map((_size, n) => renderPage('anonymized', n))}
                  <div className="pages-tail" />
                </div>
              </section>
            </>
          )}
        </div>
      )}

      {tab === 'mapping' && <MappingTable doc={doc} onDeanonymize={deanonymize} applying={applying} />}

      {confirm && (
        <ConfirmDialog title={confirm.title} confirmLabel={confirm.label}
                       onCancel={() => setConfirm(null)}
                       onConfirm={() => { const a = confirm.action; setConfirm(null); a() }}>
          {confirm.body}
        </ConfirmDialog>
      )}
    </div>
  )
}

function MappingTable({ doc, onDeanonymize, applying }) {
  const rows = Object.entries(doc.mapping)
  const { t } = useTranslation('viewer')
  return (
    <div className="panel">
      <p className="muted">{t('mapping.intro')}</p>
      <table className="maptable">
        <thead>
          <tr>
            <th>{t('mapping.placeholder')}</th>
            <th>{t('mapping.value')}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map(([ph, val]) => (
            <tr key={ph}>
              <td><code>{ph}</code></td>
              <td>{val}</td>
              <td className="map-actions">
                <button className="ghost small" disabled={applying}
                        onClick={() => onDeanonymize({ placeholder: ph })}>
                  {t('deanon.action')}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
