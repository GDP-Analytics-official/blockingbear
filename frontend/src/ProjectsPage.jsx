import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import {
  FolderKanban, Lock, LockOpen, MessageSquare, Files, Plus, Search, Trash2,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from './api.js'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
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

const fmtDate = (iso, locale) =>
  new Date(iso).toLocaleString(locale, {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
  })

// Solo la sezione "categorie da anonimizzare" di AnonOptions: i termini
// specifici non si scelgono per progetto (sono la lista dell'amministratore
// più la propria, dalle Impostazioni), quindi qui non compaiono.
function CategoryPicker({ tags, excluded, onChange }) {
  const { t } = useTranslation('projects')
  const set = new Set(excluded)
  function toggle(tag) {
    const next = new Set(set)
    if (next.has(tag)) next.delete(tag)
    else next.add(tag)
    onChange([...next].sort())
  }
  return (
    <section>
      <div className="mb-2 flex items-center justify-between gap-2">
        <h4 className="text-sm font-semibold">{t('categories.title')}</h4>
        <div className="flex gap-1">
          <Button type="button" variant="ghost" size="sm" className="h-6 px-2 text-xs"
                  disabled={set.size === 0} onClick={() => onChange([])}>
            {t('categories.all')}
          </Button>
          <Button type="button" variant="ghost" size="sm" className="h-6 px-2 text-xs"
                  disabled={set.size === tags.length}
                  onClick={() => onChange([...tags].sort())}>
            {t('categories.none')}
          </Button>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3">
        {tags.map((tag) => (
          <label key={tag}
                 className="flex cursor-pointer select-none items-center gap-2 rounded-md px-1.5 py-1 hover:bg-accent">
            <Checkbox checked={!set.has(tag)} onCheckedChange={() => toggle(tag)} />
            <span className="font-mono text-xs">{tag}</span>
          </label>
        ))}
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        {t('categories.help')}
      </p>
    </section>
  )
}

// Dialog di creazione: stesse due carte (e stesse regole) della nuova chat,
// più il nome e — per il progetto anonimizzato — le categorie.
function NewProjectDialog({ open, onOpenChange, tags, policyRequired, onCreate }) {
  const { t } = useTranslation('projects')
  const [name, setName] = useState('')
  const [mode, setMode] = useState(null)        // true | false | null
  const [excluded, setExcluded] = useState([])
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (open) {
      setName('')
      setMode(policyRequired ? true : null)
      setExcluded([])
      // le categorie partono dai default dell'admin
      api.getAnonDefaults()
        .then((d) => setExcluded(d.excluded_tags || []))
        .catch(() => {})
    }
  }, [open, policyRequired])

  const options = [
    { key: true, icon: Lock, title: t('dialog.anon.title'),
      lines: [t('dialog.anon.l1'), t('dialog.anon.l2'), t('dialog.anon.l3')] },
    { key: false, icon: LockOpen, title: t('dialog.plain.title'),
      lines: [t('dialog.plain.l1'), t('dialog.plain.l2'), t('dialog.plain.l3')] },
  ]

  async function create() {
    if (mode === null) return
    setSaving(true)
    try {
      const p = await api.createProject(
        name.trim() || t('dialog.defaultName'), mode,
        mode ? { excluded_tags: excluded } : null)
      onCreate(p)
    } catch (e) {
      toast.error(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{t('dialog.title')}</DialogTitle>
          <DialogDescription>{t('dialog.description')}</DialogDescription>
        </DialogHeader>
        <Input placeholder={t('dialog.namePlaceholder')} value={name} autoFocus
               onChange={(e) => setName(e.target.value)} />
        {policyRequired ? (
          <p className="text-xs text-emerald-700 dark:text-emerald-400">
            {t('dialog.policyRequired')}
          </p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {options.map((o) => (
              <button key={String(o.key)} type="button" onClick={() => setMode(o.key)}
                      className={cn(
                        'flex flex-col gap-2 rounded-lg border-2 p-3 text-left transition-colors',
                        mode === o.key
                          ? (o.key ? 'border-emerald-500/60 bg-emerald-500/5'
                                   : 'border-amber-500/60 bg-amber-500/5')
                          : 'border-border',
                        o.key ? 'hover:border-emerald-500/60 hover:bg-emerald-500/5'
                              : 'hover:border-amber-500/60 hover:bg-amber-500/5')}>
                <span className={cn('inline-flex items-center gap-2 font-medium',
                                    o.key ? 'text-emerald-700 dark:text-emerald-400'
                                          : 'text-amber-700 dark:text-amber-400')}>
                  <o.icon className="size-4" /> {o.title}
                </span>
                <ul className="flex flex-col gap-1 text-xs text-muted-foreground">
                  {o.lines.map((l) => <li key={l}>{l}</li>)}
                </ul>
              </button>
            ))}
          </div>
        )}
        {mode === true && tags.length > 0 && (
          <CategoryPicker tags={tags} excluded={excluded} onChange={setExcluded} />
        )}
        <DialogFooter>
          <Button variant="outline" disabled={saving} onClick={() => onOpenChange(false)}>
            {t('actions.cancel', { ns: 'common' })}
          </Button>
          <Button disabled={saving || mode === null} onClick={create}>
            {t('dialog.create')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// Route /projects: elenco dei progetti + creazione.
export default function ProjectsPage() {
  const [projects, setProjects] = useState([])
  const [loadError, setLoadError] = useState('')
  const [query, setQuery] = useState('')
  const [creating, setCreating] = useState(false)
  const [tags, setTags] = useState([])
  const [policy, setPolicy] = useState('optional')
  const [toDelete, setToDelete] = useState(null)
  const navigate = useNavigate()
  const { t } = useTranslation('projects')
  const locale = useLocale()
  usePageTitle(t('title'))

  const refresh = useCallback(() => {
    api.listProjects()
      .then((p) => { setProjects(p); setLoadError('') })
      .catch((e) => setLoadError(e.message))
  }, [])

  useEffect(() => {
    refresh()
    api.getTags().then((t) => setTags(t.all)).catch(() => {})
    api.getAnonDefaults()
      .then((d) => setPolicy(d.chat_anonymization_policy || 'optional'))
      .catch(() => {})
  }, [refresh])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return projects
    return projects.filter((p) => p.name.toLowerCase().includes(q))
  }, [projects, query])

  async function removeProject(p) {
    try {
      await api.deleteProject(p.id)
      toast.success(t('deleted'), { description: p.name })
      refresh()
    } catch (e) {
      toast.error(e.message)
    }
  }

  return (
    <main className="min-w-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full page-content max-w-3xl flex-col gap-5">
        <header className="flex flex-wrap items-center gap-2.5">
          <h1 className="text-xl font-semibold tracking-tight">{t('title')}</h1>
          {projects.length > 0 && <Badge variant="secondary">{projects.length}</Badge>}
          <Button className="ml-auto gap-2" data-tour="new-project" onClick={() => setCreating(true)}>
            <Plus /> {t('new')}
          </Button>
        </header>

        {loadError && (
          <div className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
            {loadError}
          </div>
        )}

        {projects.length > 0 && (
          <div className="flex flex-col gap-3">
            <div className="relative max-w-xs">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input placeholder={t('search')} value={query} className="pl-9"
                     onChange={(e) => setQuery(e.target.value)} />
            </div>

            <div className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
              <Table className="mobile-card-table">
                <TableHeader>
                  <TableRow className="hover:bg-transparent">
                    <TableHead>{t('table.project')}</TableHead>
                    <TableHead className="w-24 text-right">{t('table.files')}</TableHead>
                    <TableHead className="w-24 text-right">{t('table.chats')}</TableHead>
                    <TableHead className="w-40">{t('table.updated')}</TableHead>
                    <TableHead className="w-12" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filtered.map((p) => (
                    <TableRow key={p.id} className="cursor-pointer"
                              onClick={() => navigate(`/projects/${p.id}`)}>
                      <TableCell className="max-w-0">
                        <div className="flex items-center gap-2.5">
                          {p.anonymized
                            ? <Lock className="size-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
                            : <LockOpen className="size-4 shrink-0 text-amber-600 dark:text-amber-400" />}
                          <span className="truncate font-medium" title={p.name}>{p.name}</span>
                        </div>
                      </TableCell>
                      <TableCell data-label={t('table.files')} className="text-right tabular-nums text-muted-foreground">
                        <span className="inline-flex items-center gap-1">
                          <Files className="size-3.5" /> {p.n_files}
                        </span>
                      </TableCell>
                      <TableCell data-label={t('table.chats')} className="text-right tabular-nums text-muted-foreground">
                        <span className="inline-flex items-center gap-1">
                          <MessageSquare className="size-3.5" /> {p.n_chats}
                        </span>
                      </TableCell>
                      <TableCell className="mobile-secondary whitespace-nowrap tabular-nums text-muted-foreground">
                        {fmtDate(p.updated_at, locale)}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button variant="ghost" size="icon-sm"
                                title={t('actions.delete', { ns: 'common' })}
                                aria-label={t('deleteAria', { name: p.name })}
                                className="text-muted-foreground hover:text-destructive"
                                onClick={(e) => { e.stopPropagation(); setToDelete(p) }}>
                          <Trash2 />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                  {filtered.length === 0 && (
                    <TableRow className="hover:bg-transparent">
                      <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                        {t('noMatch', { query })}
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </div>
          </div>
        )}

        {projects.length === 0 && !loadError && (
          <div className="mx-auto mt-10 max-w-md text-center text-muted-foreground">
            <FolderKanban className="mx-auto mb-3 size-10 opacity-40" />
            <h2 className="mb-2 text-base font-semibold text-foreground">
              {t('empty.title')}
            </h2>
            <p className="text-sm">{t('empty.text')}</p>
          </div>
        )}
      </div>

      <NewProjectDialog open={creating} onOpenChange={setCreating} tags={tags}
                        policyRequired={policy === 'required'}
                        onCreate={(p) => { setCreating(false); navigate(`/projects/${p.id}`) }} />

      <AlertDialog open={!!toDelete} onOpenChange={(open) => { if (!open) setToDelete(null) }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('confirmDelete.title')}</AlertDialogTitle>
            <AlertDialogDescription className="break-words">
              {t('confirmDelete.text', { name: toDelete?.name || '' })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('actions.cancel', { ns: 'common' })}</AlertDialogCancel>
            <AlertDialogAction onClick={() => { const p = toDelete; setToDelete(null); removeProject(p) }}>
              {t('actions.delete', { ns: 'common' })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </main>
  )
}
