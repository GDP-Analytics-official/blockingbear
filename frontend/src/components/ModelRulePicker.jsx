import React, { useEffect, useState } from 'react'
import { Trans, useTranslation } from 'react-i18next'
import { Loader2 } from 'lucide-react'
import ModelSelector, { priceLabel } from './ModelSelector.jsx'
import ModelOptions from './ModelOptions.jsx'
import { Button } from '@/components/ui/button'

// Scelta del modello predefinito della chat. Non si salva un id ma una
// REGOLA (backend: openrouter/model_rules.py): un modello fisso, oppure le
// tre risposte di una mini procedura guidata («per cosa lo usate», «quanto
// conta il costo», «caricate immagini») che il server traduce in un modello
// sul catalogo del momento — e ritraduce quando il catalogo cambia. Lo stesso
// componente serve i due livelli — la regola dell'installazione e quella
// personale — perché le opzioni sono le stesse; cambia solo cosa vuol dire
// "nessuna scelta" (prop emptyLabel).
//
// Solo le CHIAVI: viaggiano verso il backend (model_rules.USES / BUDGETS),
// l'etichetta si traduce.
const USES = ['writing', 'documents', 'coding', 'mixed']
const BUDGETS = ['best', 'balanced', 'economy']

// Una regola che si può salvare: null (nessuna scelta), un fisso col modello,
// una guidata con tutte e tre le risposte. Il backend rifiuta il resto con
// 422; qui serve a spegnere il pulsante di salvataggio prima.
export function isRuleComplete(rule) {
  if (!rule) return true
  if (rule.kind === 'fixed') return Boolean(rule.model)
  if (rule.kind === 'guided') {
    return USES.includes(rule.use) && BUDGETS.includes(rule.budget)
      && typeof rule.images === 'boolean'
  }
  return false
}

function Question({ n, title, options, value, onPick }) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="text-sm font-medium">{n}. {title}</div>
      <div className="flex flex-wrap gap-1.5">
        {options.map((o) => (
          <Button key={String(o.key)} type="button" size="sm"
                  variant={value === o.key ? 'default' : 'outline'}
                  onClick={() => onPick(o.key)}>
            {o.label}
          </Button>
        ))}
      </div>
    </div>
  )
}

// Il risultato della procedura: lo calcola il SERVER (prop resolveRule) con
// la stessa funzione che poi userà la creazione della chat — il browser non
// ricalcola niente, mostra quello che succederà davvero.
function GuidedResult({ rule, models, resolveRule, onPickMyself }) {
  const { t } = useTranslation('models')
  const [state, setState] = useState({ loading: true })
  const key = JSON.stringify(rule)

  useEffect(() => {
    let alive = true
    setState({ loading: true })
    resolveRule(rule)
      .then((r) => { if (alive) setState({ loading: false, ...r }) })
      .catch((e) => { if (alive) setState({ loading: false, error: e.message }) })
    return () => { alive = false }
  }, [key])  // eslint-disable-line react-hooks/exhaustive-deps

  if (state.loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" /> {t('guided.computing')}
      </div>
    )
  }
  if (state.error) {
    return <p className="text-sm text-destructive">{state.error}</p>
  }
  if (!state.model) {
    return (
      <div className="flex flex-col gap-2">
        <p className="text-sm text-muted-foreground">
          {state.detail ? t('guided.noResult') : t('guided.unavailable')}
        </p>
        <div>
          <Button type="button" size="sm" variant="outline" onClick={() => onPickMyself('')}>
            {t('guided.pickMyself')}
          </Button>
        </div>
      </div>
    )
  }
  const model = models.find((m) => m.id === state.model)
  const d = state.detail || {}
  return (
    <div className="flex flex-col gap-2 rounded-md border border-border bg-card p-3">
      <p className="text-sm">
        <Trans i18nKey="guided.result" ns="models" components={{ b: <b /> }}
               values={{ model: model?.name || state.model }} />
      </p>
      <p className="text-xs text-muted-foreground">
        {t('guided.why', { pool: d.pool, score: d.score, best: d.best_score,
                           price: (model && priceLabel(model, t)) || '—' })}
      </p>
      <p className="text-xs text-muted-foreground">{t('guided.recomputed')}</p>
      <div>
        <Button type="button" size="sm" variant="outline"
                onClick={() => onPickMyself(state.model)}>
          {t('guided.pickMyself')}
        </Button>
      </div>
    </div>
  )
}

// `withOptions`: col modello fisso compare anche il pannello delle opzioni del
// modello (ragionamento, parametri), salvate nella regola: la nuova chat nasce
// con quelle. Niente ricerca web: è una scelta della singola conversazione.
// `resolveRule(rule) -> Promise<{model, detail}>`: l'anteprima della regola
// guidata, chiesta al server (endpoint diverso nel wizard e in Impostazioni).
// `allowNonZdr`: la deroga ZDR dell'installazione; spenta, il selettore del
// modello fisso non mostra i modelli senza provider conformi (la guidata li
// scarta già sul server con lo stesso criterio).
// `allowedIds`: la white list dell'admin per chi non è admin (null = tutti):
// il modello fisso si sceglie solo fra quelli, come poi in chat.
export default function ModelRulePicker({ models, value, onChange, emptyLabel,
                                          resolveRule, withOptions = false,
                                          inline = false, allowNonZdr = false,
                                          allowedIds = null }) {
  const kind = value?.kind || 'default'
  const { t } = useTranslation('models')

  function setKind(k) {
    if (k === 'default') onChange(null)
    else if (k === 'fixed') onChange({ kind: 'fixed', model: value?.model || '' })
    else onChange({ kind: 'guided', use: '', budget: '', images: null })
  }

  const label = (prefix, keys) => keys.map((k) => ({ key: k, label: t(`guided.${prefix}_${k}`) }))

  return (
    <div className="flex flex-wrap items-center gap-2">
      <select className="h-9 rounded-md border border-input bg-card px-3 text-sm"
              value={kind} onChange={(e) => setKind(e.target.value)}>
        <option value="default">{emptyLabel}</option>
        <option value="fixed">{t('rule.fixed')}</option>
        <option value="guided">{t('rule.guided')}</option>
      </select>

      {kind === 'fixed' && (
        // su una riga propria, sotto il criterio: il selettore è largo e le
        // opzioni del modello gli stanno accanto
        <div className="flex w-full flex-wrap items-center gap-2">
          <ModelSelector models={models} value={value.model} inline={inline}
                         allowNonZdr={allowNonZdr} allowedIds={allowedIds}
                         onChange={(id) => onChange({ kind: 'fixed', model: id })} />
          {withOptions && value.model && (
            <ModelOptions model={models.find((m) => m.id === value.model) || null}
                          value={value.options || {}} inline={inline}
                          onChange={(opts) => {
                            // si tengono solo le chiavi con contenuto: una regola
                            // senza opzioni resta una regola senza opzioni
                            const next = {}
                            if (opts.reasoning && Object.keys(opts.reasoning).length) next.reasoning = opts.reasoning
                            if (opts.params && Object.keys(opts.params).length) next.params = opts.params
                            onChange(Object.keys(next).length
                              ? { kind: 'fixed', model: value.model, options: next }
                              : { kind: 'fixed', model: value.model })
                          }} />
          )}
        </div>
      )}

      {kind === 'guided' && (
        <div className="flex w-full flex-col gap-3 rounded-md border border-border bg-muted/20 p-3">
          <Question n={1} title={t('guided.use')} options={label('use', USES)}
                    value={value.use} onPick={(k) => onChange({ ...value, use: k })} />
          <Question n={2} title={t('guided.budget')} options={label('budget', BUDGETS)}
                    value={value.budget} onPick={(k) => onChange({ ...value, budget: k })} />
          <Question n={3} title={t('guided.images')}
                    options={[{ key: true, label: t('guided.yes') },
                              { key: false, label: t('guided.no') }]}
                    value={value.images} onPick={(k) => onChange({ ...value, images: k })} />
          {isRuleComplete(value) && resolveRule && (
            <GuidedResult rule={value} models={models} resolveRule={resolveRule}
                          onPickMyself={(id) => onChange({ kind: 'fixed', model: id })} />
          )}
        </div>
      )}
    </div>
  )
}
