import React, { useEffect, useRef, useState } from 'react'
import { Sliders, Brain, RotateCcw, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import FloatingPanel from './FloatingPanel.jsx'

// Pannello delle opzioni del modello per la conversazione corrente.
//
// QUALI controlli appaiono lo decide il MODELLO, non questo file: la sezione
// ragionamento nasce dal blocco `reasoning` del catalogo (mandatory,
// supported_efforts, supports_max_tokens) e ogni parametro compare solo se sta
// in `supported_parameters`. Qui c'è solo il come mostrarli (etichetta,
// intervallo): sono costanti del protocollo, e il backend ri-valida tutto in
// catalog.sanitize_options — il browser propone, il server decide.
//
// Un parametro NON impostato non viene inviato: vale il default del provider.
// Per questo ogni controllo ha uno stato "predefinito" distinto dal valore
// neutro (temperatura 1 scelta a mano != temperatura non inviata).
// La chiave è quella del protocollo OpenRouter: qui restano solo intervallo e
// tipo di controllo. Etichetta e spiegazione stanno nel catalogo i18n, sotto
// `options.param.<chiave>` e `options.param.<chiave>Help`.
const CONTROLS = [
  { key: 'temperature', min: 0, max: 2, step: 0.05, neutral: 1 },
  { key: 'top_p', min: 0, max: 1, step: 0.05, neutral: 1 },
  { key: 'top_k', min: 0, max: 200, step: 1, neutral: 0 },
  { key: 'frequency_penalty', min: -2, max: 2, step: 0.1, neutral: 0 },
  { key: 'presence_penalty', min: -2, max: 2, step: 0.1, neutral: 0 },
  { key: 'max_tokens', kind: 'number' },
  { key: 'seed', kind: 'number' },
]

function Row({ label, help, children }) {
  return (
    <div className="flex flex-col gap-1 border-b border-border px-3 py-2 last:border-b-0">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium">{label}</span>
        {children}
      </div>
      {help && <span className="text-[11px] leading-snug text-muted-foreground">{help}</span>}
    </div>
  )
}

// `web` (opzionale): stato della ricerca web per la conversazione. Non è un
// parametro OpenRouter — la gestiamo noi lato host — quindi la riga c'è
// SEMPRE, qualunque cosa dichiari il modello; si disabilita solo quando la
// feature non può girare (container Docker spento, o admin che l'ha tolta).
// `inline`: pannello nel flusso della pagina invece che a tendina (FloatingPanel)
export default function ModelOptions({ model, value, onChange, disabled, web, inline = false }) {
  const [open, setOpen] = useState(false)
  const boxRef = useRef(null)
  const panelRef = useRef(null)
  const { t } = useTranslation('models')

  useEffect(() => {
    if (!open) return
    const onDoc = (e) => {
      const inBox = boxRef.current && boxRef.current.contains(e.target)
      const inPanel = panelRef.current && panelRef.current.contains(e.target)
      if (!inBox && !inPanel) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  const opts = value || {}
  const r = opts.reasoning || {}
  const params = opts.params || {}
  const meta = model?.reasoning || null
  const supported = new Set(model?.supported_parameters || [])
  const canReason = !!meta || supported.has('reasoning')
  const mandatory = !!meta?.mandatory
  const efforts = meta?.supported_efforts || []
  const enabled = r.enabled ?? (mandatory || !!meta?.default_enabled)
  const visible = CONTROLS.filter((c) => supported.has(c.key))
  const touched = Object.keys(params).length > 0 || Object.keys(r).length > 0

  // le chiavi a null si TOLGONO: "non impostato" è l'assenza della chiave,
  // così il backend non deve distinguere null da mancante
  const setReasoning = (patch) => {
    const next = { ...r, ...patch }
    Object.keys(next).forEach((k) => { if (next[k] == null) delete next[k] })
    onChange({ ...opts, reasoning: next })
  }
  const setParam = (key, v) => {
    const next = { ...params }
    if (v === '' || v === null || Number.isNaN(v)) delete next[key]
    else next[key] = v
    onChange({ ...opts, params: next })
  }

  return (
    <div className={inline ? 'contents' : 'relative'} ref={boxRef}>
      <Button variant="outline" size="sm" disabled={disabled || !model}
              title={t(model ? 'options.open' : 'options.pickFirst')}
              className={cn('gap-1.5', touched && 'border-primary text-primary')}
              onClick={() => setOpen((o) => !o)}>
        <Sliders className="size-3.5" />
        {enabled && canReason && <Brain className="size-3.5" />}
      </Button>

      {open && model && (
        <FloatingPanel inline={inline} panelRef={panelRef}
                       className={cn(inline ? 'max-w-full' : 'w-[360px] max-w-[85vw] right-0', 'overflow-hidden rounded-lg border border-border bg-popover shadow-lg')}>
          <div className="flex items-center justify-between border-b border-border px-3 py-2">
            <span className="text-xs font-semibold">
              {t('options.title', { model: model.name })}
            </span>
            <button className="text-muted-foreground hover:text-foreground"
                    title={t('actions.close', { ns: 'common' })}
                    onClick={() => setOpen(false)}>
              <X className="size-3.5" />
            </button>
          </div>

          <div className="max-h-[60vh] overflow-y-auto">
            {/* --- ricerca web (gestita internamente, mai dal catalogo) --- */}
            {web && (
              <Row label={t('options.web.label')}
                   help={t(web.usable ? 'options.web.help'
                     : web.dockerOff ? 'options.web.dockerOff'
                     : 'options.web.adminOff')}>
                <input type="checkbox" checked={web.active} disabled={!web.usable}
                       onChange={(e) => onChange({ ...opts, web_search: e.target.checked })} />
              </Row>
            )}

            {/* --- ragionamento --- */}
            {!canReason ? (
              <div className="border-b border-border px-3 py-2 text-[11px] text-muted-foreground">
                {t('options.noReasoning')}
              </div>
            ) : (
              <>
                <Row label={t('options.reasoning.label')}
                     help={t(mandatory ? 'options.reasoning.helpMandatory'
                                       : 'options.reasoning.help')}>
                  <input type="checkbox" checked={enabled} disabled={mandatory}
                         onChange={(e) => setReasoning({ enabled: e.target.checked })} />
                </Row>
                {enabled && efforts.length > 0 && (
                  <Row label={t('options.reasoning.effort')}
                       help={t('options.reasoning.effortHelp')}>
                    <div className="flex gap-1">
                      <button
                        className={cn('rounded border px-1.5 py-0.5 text-[11px]',
                                      !r.effort ? 'border-primary text-primary' : 'border-border text-muted-foreground')}
                        onClick={() => setReasoning({ effort: null })}>
                        {t('options.default')}
                      </button>
                      {efforts.map((e) => (
                        <button key={e}
                                className={cn('rounded border px-1.5 py-0.5 text-[11px]',
                                              r.effort === e ? 'border-primary text-primary' : 'border-border text-muted-foreground')}
                                onClick={() => setReasoning({ effort: e })}>
                          {e}
                        </button>
                      ))}
                    </div>
                  </Row>
                )}
                {enabled && efforts.length === 0 && meta?.supports_max_tokens && (
                  <Row label={t('options.reasoning.maxTokens')}
                       help={t('options.reasoning.maxTokensHelp')}>
                    <input type="number" min={1} step={256} value={r.max_tokens ?? ''}
                           onChange={(e) => setReasoning({
                             max_tokens: e.target.value === '' ? null : Number(e.target.value) })}
                           className="w-24 rounded border border-input bg-card px-1.5 py-0.5 text-right text-xs outline-none focus-visible:border-ring" />
                  </Row>
                )}
                {enabled && (
                  <Row label={t('options.reasoning.exclude')}
                       help={t('options.reasoning.excludeHelp')}>
                    <input type="checkbox" checked={!!r.exclude}
                           onChange={(e) => setReasoning({ exclude: e.target.checked })} />
                  </Row>
                )}
              </>
            )}

            {/* --- parametri di generazione --- */}
            {visible.length === 0 ? (
              <div className="px-3 py-2 text-[11px] text-muted-foreground">
                {t('options.noParams')}
              </div>
            ) : visible.map((c) => {
              const set = params[c.key] !== undefined
              return (
                <Row key={c.key} label={t(`options.param.${c.key}`)}
                     help={t(`options.param.${c.key}Help`)}>
                  <div className="flex items-center gap-1.5">
                    {c.kind === 'number' ? (
                      <input type="number" min={1} value={set ? params[c.key] : ''}
                             placeholder={t('options.default')}
                             onChange={(e) => setParam(c.key, e.target.value === ''
                               ? '' : Math.trunc(Number(e.target.value)))}
                             className="w-28 rounded border border-input bg-card px-1.5 py-0.5 text-right text-xs outline-none placeholder:text-muted-foreground focus-visible:border-ring" />
                    ) : (
                      <>
                        <input type="range" min={c.min} max={c.max} step={c.step}
                               value={set ? params[c.key] : c.neutral}
                               onChange={(e) => setParam(c.key, Number(e.target.value))}
                               className="w-28" />
                        <span className={cn('w-14 text-right text-[11px]',
                                            set ? 'text-foreground' : 'text-muted-foreground')}>
                          {set ? params[c.key] : t('options.defaultShort')}
                        </span>
                      </>
                    )}
                    {set && (
                      <button className="text-muted-foreground hover:text-foreground"
                              title={t('options.resetOne')}
                              onClick={() => setParam(c.key, '')}>
                        <X className="size-3" />
                      </button>
                    )}
                  </div>
                </Row>
              )
            })}
          </div>

          <div className="flex items-center justify-between gap-2 border-t border-border px-3 py-2">
            <span className="text-[11px] text-muted-foreground">
              {t('options.fromNextMessage')}
            </span>
            <Button variant="ghost" size="sm" className="gap-1.5 text-xs"
                    disabled={!touched} onClick={() => onChange({})}>
              <RotateCcw className="size-3" /> {t('options.reset')}
            </Button>
          </div>
        </FloatingPanel>
      )}
    </div>
  )
}
