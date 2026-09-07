import React, { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { CheckCircle2, KeyRound, Loader2, Pencil, RefreshCw } from 'lucide-react'
import { Trans, useTranslation } from 'react-i18next'
import { api } from './api.js'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table'
import { fmtUsd } from './CostsShared.jsx'
import { usePageTitle } from '@/lib/usePageTitle.js'

// cadenze note: il valore viene da OpenRouter, l'etichetta dal catalogo

// Route /admin/keys (solo admin): le chiavi OpenRouter degli utenti, coi
// consumi e i limiti di spesa. NIENTE disabilitazione: per bloccare qualcuno si
// elimina l'utente dalla pagina Utenti, che revoca anche la chiave. La
// creazione/rotazione delle chiavi sta in Utenti.
export default function CostsKeysPage() {
  const [overview, setOverview] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [editKey, setEditKey] = useState(null)  // riga chiave in modifica limite
  const { t } = useTranslation('costs')
  usePageTitle(t('keys.title'))

  async function load() {
    setLoading(true)
    setError('')
    try {
      setOverview(await api.usageOverview())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { load() }, [])

  return (
    <main className="min-w-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full page-content max-w-4xl flex-col gap-5">
        <header className="flex flex-wrap items-center gap-2.5">
          <h1 className="text-xl font-semibold tracking-tight">{t('keys.title')}</h1>
          <span className="text-sm text-muted-foreground">{t('keys.subtitle')}</span>
          <Button variant="outline" size="sm" className="ml-auto"
                  disabled={loading} onClick={load}>
            {loading ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            {t('keys.refresh')}
          </Button>
        </header>

        <ManagementKey onChanged={load} />

        {error && (
          <div className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
            {error}
          </div>
        )}

        {overview && (
          <section className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
            <div className="flex flex-wrap items-center gap-2 px-4 pt-4">
              <h2 className="text-sm font-semibold">{t('keys.sectionTitle')}</h2>
              <span className="text-xs text-muted-foreground">{t('keys.sectionHint')}</span>
            </div>
            <Table className="mobile-card-table mt-2">
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>{t('keys.table.user')}</TableHead>
                  <TableHead className="text-right">{t('keys.table.today')}</TableHead>
                  <TableHead className="text-right">{t('keys.table.week')}</TableHead>
                  <TableHead className="text-right">{t('keys.table.month')}</TableHead>
                  <TableHead className="text-right">{t('keys.table.total')}</TableHead>
                  <TableHead className="text-right">{t('keys.table.limit')}</TableHead>
                  <TableHead className="w-14" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {(overview.keys || []).map((k) => (
                  <TableRow key={k.hash}>
                    <TableCell>
                      <div className="font-medium">{k.user?.username || k.name}</div>
                      <div className="text-xs text-muted-foreground">{k.name}</div>
                    </TableCell>
                    <TableCell data-label={t('keys.table.today')} className="text-right tabular-nums">{fmtUsd(k.usage_daily)}</TableCell>
                    <TableCell data-label={t('keys.table.week')} className="text-right tabular-nums">{fmtUsd(k.usage_weekly)}</TableCell>
                    <TableCell data-label={t('keys.table.month')} className="text-right tabular-nums">{fmtUsd(k.usage_monthly)}</TableCell>
                    <TableCell data-label={t('keys.table.total')} className="text-right tabular-nums">{fmtUsd(k.usage)}</TableCell>
                    <TableCell data-label={t('keys.table.limit')} className="text-right">
                      {k.limit == null
                        ? <span className="text-muted-foreground">{t('keys.noLimit')}</span>
                        : <div className="tabular-nums">
                            {fmtUsd(k.limit)}
                            <div className="text-xs text-muted-foreground">
                              {/* una cadenza che OpenRouter aggiungesse domani
                                  esce com'è, invece di sparire */}
                              {k.limit_reset
                                ? t(`keys.reset.${k.limit_reset}`, { defaultValue: k.limit_reset })
                                : t('keys.noReset')}
                              {k.limit_remaining != null
                                && t('keys.remaining', { value: fmtUsd(k.limit_remaining) })}
                            </div>
                          </div>}
                    </TableCell>
                    <TableCell className="text-right">
                      {k.user && (
                        <Button variant="ghost" size="icon-sm" title={t('keys.editLimit')}
                                aria-label={t('keys.editLimitAria', { user: k.user.username })}
                                className="text-muted-foreground"
                                onClick={() => setEditKey(k)}>
                          <Pencil />
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {(overview.users_without_key || []).length > 0 && (
              <div className="border-t border-border px-5 py-3 text-xs text-muted-foreground">
                {t('keys.withoutKey', {
                  users: overview.users_without_key.map((u) => u.username).join(', '),
                })}
              </div>
            )}
          </section>
        )}
        {!overview && !error && (
          <div className="py-16 text-center text-sm text-muted-foreground">
            {t('state.loading', { ns: 'common' })}
          </div>
        )}
      </div>

      <LimitDialog keyRow={editKey} onClose={() => setEditKey(null)}
                   onSaved={() => { setEditKey(null); load() }} />
    </main>
  )
}

// La management key dell'installazione: stato (mascherata, saldo account) e
// sostituzione. Write-only: il server la valida con OpenRouter e restituisce
// solo la forma mascherata.
function ManagementKey({ onChanged }) {
  const [status, setStatus] = useState(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const { t } = useTranslation('costs')

  useEffect(() => { api.orStatus().then(setStatus).catch(() => setStatus({})) }, [])

  async function save() {
    const key = draft.trim()
    if (!key) return
    setBusy(true)
    try {
      const r = await api.setManagementKey(key)
      setStatus((s) => ({ ...s, provisioning: true, management_masked: r.management_masked }))
      setDraft('')
      toast.success(t('keys.management.saved'))
      onChanged?.()
    } catch (e) {
      toast.error(e.message)
    } finally {
      setBusy(false)
    }
  }

  const balance = status?.credits && status.credits.total_credits != null
    ? ((Number(status.credits.total_credits) || 0) - (Number(status.credits.total_usage) || 0)).toFixed(2)
    : null

  return (
    <section className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-sm">
      <h2 className="text-sm font-semibold">{t('keys.management.title')}</h2>
      <p className="text-xs text-muted-foreground">
        <Trans i18nKey="keys.management.help" ns="costs" components={{ i: <i /> }} />
      </p>
      {status && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-primary/40 bg-primary/5 px-3 py-2 text-sm">
          <CheckCircle2 className="size-4 text-primary" />
          {status.provisioning
            ? <span>{t('keys.management.current')} <code>{status.management_masked}</code></span>
            : <span>{t('keys.management.missing')}</span>}
          {balance != null && (
            <span className="text-muted-foreground">· {t('keys.management.balance', { value: balance })}</span>
          )}
        </div>
      )}
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex min-w-0 basis-56 flex-1 flex-col gap-1.5">
          <Label htmlFor="mgmt-key">
            {t(status?.provisioning ? 'keys.management.replace' : 'keys.management.insert')}
          </Label>
          <Input id="mgmt-key" type="password" autoComplete="off" placeholder="sk-or-…"
                 value={draft} onChange={(e) => setDraft(e.target.value)}
                 onKeyDown={(e) => { if (e.key === 'Enter') save() }} />
        </div>
        <Button type="button" disabled={!draft.trim() || busy} onClick={save}>
          {busy ? <Loader2 className="animate-spin" /> : <KeyRound />}
          {t('actions.save', { ns: 'common' })}
        </Button>
      </div>
    </section>
  )
}

// Modifica del tetto di spesa di una chiave: limite in USD (vuoto = nessun
// tetto) + cadenza di azzeramento. PATCH diretta su OpenRouter.
function LimitDialog({ keyRow, onClose, onSaved }) {
  const [limit, setLimit] = useState('')
  const [reset, setReset] = useState('')
  const [saving, setSaving] = useState(false)
  const { t } = useTranslation('costs')

  useEffect(() => {
    if (keyRow) {
      setLimit(keyRow.limit == null ? '' : String(keyRow.limit))
      setReset(keyRow.limit_reset || '')
    }
  }, [keyRow])

  async function save() {
    setSaving(true)
    try {
      const body = limit === ''
        ? { clear_limit: true, clear_limit_reset: true }
        : { limit: Number(limit),
            ...(reset ? { limit_reset: reset } : { clear_limit_reset: true }) }
      await api.patchUserKey(keyRow.user.id, body)
      toast.success(t('keys.dialog.saved'), { description: keyRow.name })
      onSaved()
    } catch (e) {
      toast.error(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={!!keyRow} onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>{t('keys.dialog.title')}</DialogTitle>
          <DialogDescription>
            {t('keys.dialog.description', { user: keyRow?.user?.username || '' })}
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="limit-usd">{t('keys.dialog.limit')}</Label>
            <Input id="limit-usd" type="number" min="0" step="0.01" value={limit}
                   inputMode="decimal" placeholder={t('keys.dialog.limitPlaceholder')}
                   onChange={(e) => setLimit(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="limit-reset">{t('keys.dialog.reset')}</Label>
            <select id="limit-reset" value={reset} disabled={limit === ''}
                    className="h-9 rounded-md border border-input bg-card px-2 text-sm shadow-sm disabled:opacity-50"
                    onChange={(e) => setReset(e.target.value)}>
              <option value="">{t('keys.dialog.resetNever')}</option>
              <option value="daily">{t('keys.dialog.resetDaily')}</option>
              <option value="weekly">{t('keys.dialog.resetWeekly')}</option>
              <option value="monthly">{t('keys.dialog.resetMonthly')}</option>
            </select>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {t('actions.cancel', { ns: 'common' })}
          </Button>
          <Button disabled={saving || (limit !== '' && !(Number(limit) >= 0))} onClick={save}>
            {saving && <Loader2 className="animate-spin" />} {t('actions.save', { ns: 'common' })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
