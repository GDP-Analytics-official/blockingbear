import React, { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { KeyRound, Loader2, RefreshCw, Trash2, UserPlus } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from './api.js'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '@/components/ui/table'
import { usePageTitle } from '@/lib/usePageTitle.js'

// solo le chiavi: il valore va al backend, l'etichetta la traduce la pagina
const RESET_PERIODS = ['daily', 'weekly', 'monthly']

// Route /users (solo admin): gestione degli account e della chiave OpenRouter
// personale di ciascuno (creata dal server col nome blockingbear-<username>;
// consumi e modifiche dei limiti stanno nella pagina Costi).
export default function UsersPage() {
  const [users, setUsers] = useState([])
  const [error, setError] = useState('')
  const [form, setForm] = useState({
    username: '', password: '', role: 'standard', limit: '', reset: 'monthly',
    mustChange: true,     // preselezionato: la password d'ufficio è di passaggio
  })
  const [toDelete, setToDelete] = useState(null)
  const [toRotate, setToRotate] = useState(null)
  const [busyKey, setBusyKey] = useState(null)   // user id con operazione chiave in corso
  const [creating, setCreating] = useState(false) // creazione (utente + chiave OpenRouter) in corso
  const { t } = useTranslation('users')
  usePageTitle(t('title'))

  const refresh = () => api.listUsers().then(setUsers).catch((e) => setError(e.message))
  useEffect(() => { refresh() }, [])

  async function create(e) {
    e.preventDefault()
    if (creating) return
    setCreating(true)
    try {
      const limit = form.limit === '' ? null : Number(form.limit)
      const created = await api.createUser(
        form.username, form.password, form.role, limit,
        limit != null ? form.reset : null, form.mustChange)
      if (created.key_error) {
        toast.warning(t('toast.createdNoKey'), {
          description: t('toast.createdNoKeyHint', { error: created.key_error }),
        })
      } else {
        toast.success(t('toast.created'), { description: form.username })
      }
      setForm({ username: '', password: '', role: 'standard', limit: '',
                reset: 'monthly', mustChange: true })
      refresh()
    } catch (err) {
      toast.error(err.message)
    } finally {
      setCreating(false)
    }
  }

  async function remove(u) {
    try {
      const res = await api.deleteUser(u.id)
      if (res.key_error) {
        toast.warning(t('toast.deletedKeyKept'), {
          description: t('toast.deletedKeyKeptHint', { error: res.key_error }),
        })
      } else {
        toast.success(t('toast.deleted'), { description: u.username })
      }
      refresh()
    } catch (err) {
      toast.error(err.message)
    }
  }

  async function rotate(u) {
    setBusyKey(u.id)
    try {
      await api.rotateUserKey(u.id)
      toast.success(t(u.has_key ? 'key.rotated' : 'key.created'),
        { description: u.key_name })
      refresh()
    } catch (err) {
      toast.error(err.message)
    } finally {
      setBusyKey(null)
    }
  }

  return (
    <main className="min-w-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full page-content max-w-2xl flex-col gap-5">
        <header className="flex items-baseline gap-2.5">
          <h1 className="text-xl font-semibold tracking-tight">{t('title')}</h1>
          {users.length > 0 && <Badge variant="secondary">{users.length}</Badge>}
        </header>

        {error && (
          <div className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
            {error}
          </div>
        )}

        <div className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
          <Table className="mobile-card-table">
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>{t('table.user')}</TableHead>
                <TableHead className="w-28">{t('table.role')}</TableHead>
                <TableHead className="w-40">{t('table.key')}</TableHead>
                <TableHead className="w-20" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {users.map((u) => (
                <TableRow key={u.id}>
                  <TableCell className="font-medium [overflow-wrap:anywhere]">
                    {u.username}
                    {/* sparisce da solo: il flag cade al primo cambio password */}
                    {u.must_change_password && (
                      <Badge variant="outline"
                             className="ml-2 border-dashed font-normal text-muted-foreground">
                        {t('table.mustChange')}
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <Badge variant={u.role === 'admin' ? 'secondary' : 'outline'}>{u.role}</Badge>
                  </TableCell>
                  <TableCell>
                    {u.has_key
                      ? <Badge variant="outline" className="gap-1 font-normal text-muted-foreground">
                          <KeyRound className="size-3" /> {t('key.active')}
                        </Badge>
                      : <Badge variant="outline" className="border-dashed font-normal text-muted-foreground">
                          {t('key.missing')}
                        </Badge>}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button variant="ghost" size="icon-sm"
                            title={t(u.has_key ? 'key.rotate' : 'key.create')}
                            aria-label={t(u.has_key ? 'key.rotateAria' : 'key.createAria',
                                          { user: u.username })}
                            className="text-muted-foreground"
                            disabled={busyKey === u.id}
                            onClick={() => (u.has_key ? setToRotate(u) : rotate(u))}>
                      <RefreshCw className={busyKey === u.id ? 'animate-spin' : ''} />
                    </Button>
                    <Button variant="ghost" size="icon-sm"
                            title={t('actions.delete', { ns: 'common' })}
                            aria-label={t('deleteAria', { user: u.username })}
                            className="text-muted-foreground hover:text-destructive"
                            onClick={() => setToDelete(u)}>
                      <Trash2 />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>

        <form onSubmit={create}
              className="flex flex-col gap-2 rounded-xl border border-border bg-card p-3 shadow-sm">
          <div className="flex flex-wrap items-center gap-2">
            <Input placeholder={t('form.username')} value={form.username} className="min-w-40 flex-1"
                   autoComplete="off"
                   onChange={(e) => setForm({ ...form, username: e.target.value })} />
            <Input placeholder={t('form.password')} type="password" value={form.password}
                   className="min-w-40 flex-1" autoComplete="new-password"
                   onChange={(e) => setForm({ ...form, password: e.target.value })} />
            <select
              value={form.role}
              aria-label={t('form.roleAria')}
              className="h-9 w-32 rounded-md border border-input bg-card px-2 text-sm shadow-sm focus-visible:outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/25"
              onChange={(e) => setForm({ ...form, role: e.target.value })}
            >
              <option value="standard">standard</option>
              <option value="admin">admin</option>
            </select>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Input placeholder={t('form.limit')} type="number" min="0"
                   step="0.01" value={form.limit} className="min-w-0 w-full sm:min-w-56 flex-1" inputMode="decimal"
                   aria-label={t('form.limitAria')}
                   onChange={(e) => setForm({ ...form, limit: e.target.value })} />
            <select
              value={form.reset}
              aria-label={t('form.resetAria')}
              disabled={form.limit === ''}
              className="h-9 w-36 rounded-md border border-input bg-card px-2 text-sm shadow-sm disabled:opacity-50 focus-visible:outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/25"
              onChange={(e) => setForm({ ...form, reset: e.target.value })}
            >
              {RESET_PERIODS.map((v) => (
                <option key={v} value={v}>{t(`reset.${v}`)}</option>
              ))}
            </select>
            <Button disabled={creating || form.username.length < 3 || form.password.length < 8}>
              {creating ? <Loader2 className="animate-spin" /> : <UserPlus />} {t('form.add')}
            </Button>
          </div>
          <label className="flex w-fit cursor-pointer items-center gap-2 text-sm">
            <input type="checkbox" checked={form.mustChange}
                   onChange={(e) => setForm({ ...form, mustChange: e.target.checked })} />
            {t('form.mustChange')}
          </label>
          <p className="text-xs text-muted-foreground">{t('form.help')}</p>
        </form>
      </div>

      <AlertDialog open={!!toDelete} onOpenChange={(open) => { if (!open) setToDelete(null) }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('confirmDelete.title')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('confirmDelete.text', { user: toDelete?.username || '' })}
              {' '}
              {/* quanto si porta via: conteggi dalla lista utenti */}
              {t('confirmDelete.content', {
                projects: toDelete?.n_projects ?? 0,
                chats: toDelete?.n_chats ?? 0,
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('actions.cancel', { ns: 'common' })}</AlertDialogCancel>
            <AlertDialogAction onClick={() => { const u = toDelete; setToDelete(null); remove(u) }}>
              {t('actions.delete', { ns: 'common' })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={!!toRotate} onOpenChange={(open) => { if (!open) setToRotate(null) }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('confirmRotate.title')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('confirmRotate.text', { user: toRotate?.username || '' })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('actions.cancel', { ns: 'common' })}</AlertDialogCancel>
            <AlertDialogAction onClick={() => { const u = toRotate; setToRotate(null); rotate(u) }}>
              {t('confirmRotate.action')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </main>
  )
}
