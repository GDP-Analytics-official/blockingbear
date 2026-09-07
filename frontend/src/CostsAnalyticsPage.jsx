import React, { useEffect, useMemo, useState } from 'react'
import { Loader2, RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from './api.js'
import { Button } from '@/components/ui/button'
import {
  ChartCard, OTHER_COLOR, PALETTE, StackedBars, StackedColumns, StatTile,
  fmtDay, fmtInt, fmtUsd, keyOwnerName,
} from './CostsShared.jsx'
import { usePageTitle } from '@/lib/usePageTitle.js'
import { useLocale } from '@/lib/format.js'

const PERIODS = [7, 30, 90]           // giorni
const TOP_MODELS = 10                 // barre nel grafico; la tabella li ha tutti

// Route /admin/analytics (solo admin): i numeri della spesa OpenRouter,
// interrogati on-demand (il perimetro "solo chiavi blockingbear-*" è del
// backend). Tre viste sugli STESSI due dataset: per giorno (giorno x utente)
// e per modello/utente (modello x utente, una query sola per due grafici).
export default function CostsAnalyticsPage() {
  const [days, setDays] = useState(30)
  const [overview, setOverview] = useState(null)
  const [daily, setDaily] = useState(null)        // righe giorno x chiave
  const [modelRows, setModelRows] = useState(null) // righe modello x chiave
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const { t } = useTranslation('costs')
  const locale = useLocale()
  usePageTitle(t('analytics.title'))
  // «Altri modelli» è anche la CHIAVE con cui il segmento si riconosce nei
  // calcoli qui sotto: deve cambiare con la lingua insieme all'etichetta
  const otherModels = t('analytics.otherModels')

  async function load(d = days) {
    setLoading(true)
    setError('')
    try {
      const [ov, dl, mu] = await Promise.all([
        api.usageOverview(),
        api.usageQuery({
          metrics: ['total_usage', 'request_count', 'tokens_total'],
          dimensions: ['api_key_id'], granularity: 'day', days: d,
        }),
        api.usageQuery({
          metrics: ['total_usage', 'request_count'],
          dimensions: ['model', 'api_key_id'], days: d, limit: 500,
        }),
      ])
      setOverview(ov)
      setDaily(dl.data || [])
      setModelRows(mu.data || [])
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { load(days) }, [days])   // eslint-disable-line react-hooks/exhaustive-deps

  // nome chiave -> username (dal join del backend): le analytics parlano di
  // persone, non di chiavi
  const keyIndex = useMemo(() => {
    const m = new Map()
    for (const k of overview?.keys || []) {
      if (k.user) m.set(k.name, k.user.username)
    }
    return m
  }, [overview])

  // colore per UTENTE: assegnazione alfabetica su tutti gli utenti noti,
  // stabile qualunque sia il periodo
  const userColor = useMemo(() => {
    const owners = [...new Set((overview?.keys || [])
      .map((k) => k.user?.username).filter(Boolean))].sort()
    const m = new Map(owners.map((o, i) => [o, PALETTE[i] || OTHER_COLOR]))
    return (name) => m.get(name) || OTHER_COLOR
  }, [overview])

  // --- giorno x utente --------------------------------------------------------
  const dailyView = useMemo(() => {
    const rows = daily || []
    const dateKey = rows.length
      ? Object.keys(rows[0]).find((k) => k.startsWith('date')) : null
    const byDate = new Map()
    const users = new Set()
    const tot = { usage: 0, requests: 0, tokens: 0 }
    for (const r of rows) {
      const owner = keyOwnerName(r.api_key_id, keyIndex)
      users.add(owner)
      const date = dateKey ? r[dateKey] : ''
      const v = Number(r.total_usage) || 0
      tot.usage += v
      tot.requests += Number(r.request_count) || 0
      tot.tokens += Number(r.tokens_total) || 0
      if (!byDate.has(date)) byDate.set(date, new Map())
      byDate.get(date).set(owner, (byDate.get(date).get(owner) || 0) + v)
    }
    const series = [...users].sort()
      .map((u) => ({ name: u, color: userColor(u) }))
    const categories = [...byDate.keys()].sort().map((date) => {
      const m = byDate.get(date)
      const segments = series.map((s) => ({ ...s, value: m.get(s.name) || 0 }))
      return { key: date, label: fmtDay(date, locale), segments,
               total: segments.reduce((a, s) => a + s.value, 0) }
    })
    const table = {
      headers: [t('analytics.byDay.day'), ...series.map((s) => s.name),
                t('analytics.total')],
      rows: categories.map((c) => [c.key,
                                   ...c.segments.map((s) => s.value), c.total]),
    }
    return { categories, series, table, tot }
  }, [daily, keyIndex, userColor, locale, t])

  // --- modello x utente (un dataset, due viste) -------------------------------
  const modelView = useMemo(() => {
    const rows = modelRows || []
    const byModel = new Map()          // modello -> Map(utente -> spesa)
    const byUser = new Map()           // utente  -> Map(modello -> spesa)
    const reqByModel = new Map()
    for (const r of rows) {
      const owner = keyOwnerName(r.api_key_id, keyIndex)
      const model = r.model || '?'
      const v = Number(r.total_usage) || 0
      if (!byModel.has(model)) byModel.set(model, new Map())
      byModel.get(model).set(owner, (byModel.get(model).get(owner) || 0) + v)
      if (!byUser.has(owner)) byUser.set(owner, new Map())
      byUser.get(owner).set(model, (byUser.get(owner).get(model) || 0) + v)
      reqByModel.set(model,
                     (reqByModel.get(model) || 0) + (Number(r.request_count) || 0))
    }
    const modelTotal = (m) => [...byModel.get(m).values()].reduce((a, b) => a + b, 0)
    const models = [...byModel.keys()].sort((a, b) => modelTotal(b) - modelTotal(a))
    const users = [...byUser.keys()].sort()
    const userSeries = users.map((u) => ({ name: u, color: userColor(u) }))

    // grafico "spesa per modello": barre orizzontali spaccate per utente
    const modelBars = models.slice(0, TOP_MODELS).map((m) => ({
      key: m,
      label: m,
      segments: userSeries.map((s) => ({ ...s, value: byModel.get(m).get(s.name) || 0 })),
      total: modelTotal(m),
      // `n` e non `count`: il valore è già formattato ("1.2 mln") e `count`
      // in i18next è la leva dei plurali, non una variabile qualsiasi
      hint: t('analytics.byModel.requestsShort',
              { n: fmtInt(reqByModel.get(m), t) }),
    }))
    const modelTable = {
      headers: [t('analytics.byModel.model'), ...users, t('analytics.total'),
                t('analytics.byModel.requests')],
      rows: models.map((m) => [m,
        ...users.map((u) => byModel.get(m).get(u) || 0),
        modelTotal(m), Number(reqByModel.get(m)) || 0]),
    }

    // grafico "spesa per utente": colonne verticali spaccate per modello.
    // Colori per MODELLO: alfabetico tra i primi TOP; il resto è un'unica
    // voce grigia (mai due modelli con lo stesso colore)
    const topModels = models.slice(0, PALETTE.length)
    const colorByModel = new Map([...topModels].sort()
      .map((m, i) => [m, PALETTE[i]]))
    const hasOther = models.length > topModels.length
    const modelSeries = [
      ...topModels.map((m) => ({ name: m, color: colorByModel.get(m) })),
      ...(hasOther ? [{ name: otherModels, color: OTHER_COLOR }] : []),
    ]
    const usersByTotal = [...users].sort((a, b) => {
      const t = (u) => [...byUser.get(u).values()].reduce((x, y) => x + y, 0)
      return t(b) - t(a)
    })
    const userCols = usersByTotal.map((u) => {
      const m = byUser.get(u)
      const segments = modelSeries.map((s) => ({
        ...s,
        value: s.name === otherModels
          ? models.slice(PALETTE.length)
              .reduce((a, mm) => a + (m.get(mm) || 0), 0)
          : m.get(s.name) || 0,
      }))
      return { key: u, label: u, segments,
               total: segments.reduce((a, s) => a + s.value, 0) }
    })
    const userTable = {
      headers: [t('analytics.byUser.user'), ...models, t('analytics.total')],
      rows: usersByTotal.map((u) => [u,
        ...models.map((m) => byUser.get(u).get(m) || 0),
        [...byUser.get(u).values()].reduce((a, b) => a + b, 0)]),
    }
    return { modelBars, userSeries, modelTable, userCols, modelSeries, userTable }
  }, [modelRows, keyIndex, userColor, otherModels, t])

  const credits = overview?.credits
  const balance = credits
    ? (Number(credits.total_credits) || 0) - (Number(credits.total_usage) || 0)
    : null

  return (
    <main className="min-w-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full page-content max-w-4xl flex-col gap-5">
        <header className="flex flex-wrap items-center gap-2.5">
          <h1 className="text-xl font-semibold tracking-tight">{t('analytics.title')}</h1>
          <span className="text-sm text-muted-foreground">{t('analytics.subtitle')}</span>
          <div className="ml-auto flex items-center gap-1 rounded-lg border border-border bg-card p-0.5 shadow-sm">
            {PERIODS.map((d) => (
              <button key={d} type="button"
                      className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                        days === d ? 'bg-secondary text-secondary-foreground'
                                   : 'text-muted-foreground hover:bg-accent'}`}
                      onClick={() => setDays(d)}>
                {t('analytics.period', { count: d })}
              </button>
            ))}
          </div>
          <Button variant="outline" size="sm" disabled={loading} onClick={() => load()}>
            {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            {t('analytics.refresh')}
          </Button>
        </header>

        {error && (
          <div className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
            {error}
          </div>
        )}

        {overview && (
          <>
            <div className="flex flex-wrap gap-3">
              <StatTile label={t('analytics.tile.balance')} value={fmtUsd(balance)} />
              <StatTile label={t('analytics.tile.spend', { count: days })}
                        value={fmtUsd(dailyView.tot.usage)}
                        hint={t('analytics.tile.spendHint')} />
              <StatTile label={t('analytics.tile.requests')}
                        value={fmtInt(dailyView.tot.requests, t)}
                        hint={t('analytics.tile.lastDays', { count: days })} />
              <StatTile label={t('analytics.tile.tokens')}
                        value={fmtInt(dailyView.tot.tokens, t)}
                        hint={t('analytics.tile.lastDays', { count: days })} />
            </div>

            {daily && (
              <ChartCard title={t('analytics.byDay.title')}
                         table={dailyView.table}
                         exportName={t('analytics.byDay.export', { days })}>
                <StackedColumns categories={dailyView.categories}
                                series={dailyView.series}
                                ariaLabel={t('analytics.byDay.aria')} />
              </ChartCard>
            )}

            {modelRows && (
              <ChartCard title={t('analytics.byModel.title')}
                         subtitle={modelView.modelTable.rows.length > TOP_MODELS
                           ? t('analytics.byModel.subtitle', { count: TOP_MODELS })
                           : undefined}
                         table={modelView.modelTable}
                         exportName={t('analytics.byModel.export', { days })}>
                <StackedBars rows={modelView.modelBars}
                             series={modelView.userSeries} />
              </ChartCard>
            )}

            {modelRows && (
              <ChartCard title={t('analytics.byUser.title')}
                         subtitle={t('analytics.byUser.subtitle')}
                         table={modelView.userTable}
                         exportName={t('analytics.byUser.export', { days })}>
                <StackedColumns categories={modelView.userCols}
                                series={modelView.modelSeries}
                                ariaLabel={t('analytics.byUser.aria')} />
              </ChartCard>
            )}
          </>
        )}
        {!overview && !error && (
          <div className="py-16 text-center text-sm text-muted-foreground">
            {t('state.loading', { ns: 'common' })}
          </div>
        )}
      </div>
    </main>
  )
}
