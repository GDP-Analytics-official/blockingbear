import React, { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useMatch, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import {
  ChartColumn, ChevronsUpDown, FolderKanban, KeyRound, Lock, LockOpen,
  LogOut, MessageSquarePlus, Search, Settings, Trash2, Users,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from './api.js'
import { useAuth } from './auth.jsx'
import { ChatsProvider, useChats } from './chats.jsx'
import { JobsProvider, JobCards } from './jobs.jsx'
import ChatSearchDialog from '@/components/ChatSearchDialog.jsx'
import { Logo } from '@/components/Logo.jsx'
import { FlagIt } from '@/i18n/languages.jsx'
import LanguageSwitcher from '@/components/LanguageSwitcher.jsx'
import { OnboardingProvider } from '@/onboarding/OnboardingProvider.jsx'
import { cn } from '@/lib/utils'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog'

function NavItem({ to, icon: Icon, children }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) => cn(
        'flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors',
        'hover:bg-accent hover:text-foreground',
        isActive && 'bg-secondary text-secondary-foreground hover:bg-secondary hover:text-secondary-foreground'
      )}
    >
      <Icon className="size-4 shrink-0" />
      {children}
    </NavLink>
  )
}

// L'elenco delle chat libere, direttamente nella sidebar (come Claude e
// ChatGPT). Le chat dei progetti non stanno qui: vivono nella loro pagina.
function SidebarChats() {
  const { chats, setChats } = useChats()
  const { t } = useTranslation('chat')
  const navigate = useNavigate()
  const [searchOpen, setSearchOpen] = useState(false)
  const [toDelete, setToDelete] = useState(null)   // chat in attesa di conferma
  // la chat aperta si legge dall'URL: il layout sta sopra la route
  const activeId = useMatch('/chats/:chatId')?.params.chatId

  function askRemove(c, e) {
    // il click sul cestino non deve anche APRIRE la chat che sta eliminando
    e.preventDefault()
    e.stopPropagation()
    setToDelete(c)
  }

  async function removeChat(id) {
    try {
      await api.deleteChat(id)
      setChats((cs) => cs.filter((c) => c.id !== id))
      if (id === activeId) navigate('/new', { replace: true })
    } catch (err) { toast.error(err.message) }
  }

  return (
    <div className="flex flex-col">
      <div className="flex items-center justify-between px-3 pb-1">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          {t('title')}
        </div>
        <button type="button" onClick={() => setSearchOpen(true)}
                aria-label={t('search.title')} title={t('search.title')}
                className="-my-1 cursor-pointer rounded p-1 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground">
          <Search className="size-3.5" />
        </button>
      </div>
      <ChatSearchDialog open={searchOpen} onOpenChange={setSearchOpen} />
      <ul className="flex flex-col gap-0.5">
        {chats.map((c) => (
          <li key={c.id}>
            <NavLink to={`/chats/${c.id}`}
                     className={({ isActive }) => cn(
                       'group flex items-center gap-2 rounded-md px-3 py-1.5 text-sm text-muted-foreground',
                       'hover:bg-accent hover:text-foreground',
                       isActive && 'bg-secondary text-secondary-foreground hover:bg-secondary hover:text-secondary-foreground')}>
              {c.anonymized
                ? <Lock className="size-3 shrink-0 text-emerald-600 dark:text-emerald-400" />
                : <LockOpen className="size-3 shrink-0 text-amber-600 dark:text-amber-400" />}
              <span className="min-w-0 flex-1 truncate" title={c.title}>{c.title}</span>
              <Trash2
                className="size-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity hover:text-destructive group-hover:opacity-100"
                role="button" aria-label={t('actions.delete', { ns: 'common' })}
                onClick={(e) => askRemove(c, e)}
              />
            </NavLink>
          </li>
        ))}
        {chats.length === 0 && (
          <li className="px-3 py-3 text-xs text-muted-foreground">{t('empty')}</li>
        )}
      </ul>

      <AlertDialog open={!!toDelete} onOpenChange={(open) => { if (!open) setToDelete(null) }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('confirmDelete.title')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('confirmDelete.text', { title: toDelete?.title || '' })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('actions.cancel', { ns: 'common' })}</AlertDialogCancel>
            <AlertDialogAction onClick={() => { const c = toDelete; setToDelete(null); removeChat(c.id) }}>
              {t('actions.delete', { ns: 'common' })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

// Il blocco utente nel footer della sidebar: al click si apre verso l'alto il
// menu con Impostazioni, le pagine di amministrazione (solo admin, in ordine
// alfabetico), il logout e in fondo la firma.
function UserMenu({ user, logout }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  // chiusura al click fuori e con Escape: il menu non ha un overlay sotto
  useEffect(() => {
    if (!open) return undefined
    const onDown = (e) => { if (!ref.current?.contains(e.target)) setOpen(false) }
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const items = [
    { to: '/settings', icon: Settings, label: t('nav.settings') },
    ...(user.role === 'admin' ? [
      { to: '/admin/analytics', icon: ChartColumn, label: t('nav.analytics') },
      { to: '/admin/keys', icon: KeyRound, label: t('nav.apiKeys') },
      { to: '/admin/users', icon: Users, label: t('nav.users') },
    ].sort((a, b) => a.label.localeCompare(b.label)) : []),
  ]

  return (
    <div ref={ref} className="relative min-w-0 flex-1">
      <button type="button" onClick={() => setOpen((o) => !o)}
              aria-haspopup="menu" aria-expanded={open} data-tour="user-menu"
              className="-m-1 flex w-full cursor-pointer items-center gap-2 rounded-md p-1 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40">
        <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-secondary text-[10px] font-semibold uppercase text-secondary-foreground">
          {user.username.slice(0, 1)}
        </span>
        <div className="min-w-0 flex-1 truncate text-[13px] font-medium text-muted-foreground">{user.username}</div>
        <ChevronsUpDown className="size-3.5 shrink-0 text-muted-foreground" />
      </button>
      {open && (
        <div role="menu"
             className="absolute bottom-full left-0 z-50 mb-2 flex w-56 flex-col gap-0.5 rounded-md border border-border bg-popover p-1 shadow-md">
          {items.map(({ to, icon: Icon, label }) => (
            <NavLink key={to} to={to} role="menuitem" onClick={() => setOpen(false)}
                     className={({ isActive }) => cn(
                       'flex items-center gap-2.5 rounded px-2 py-1.5 text-sm text-muted-foreground',
                       'hover:bg-accent hover:text-foreground',
                       isActive && 'bg-secondary text-secondary-foreground hover:bg-secondary hover:text-secondary-foreground')}>
              <Icon className="size-4 shrink-0" />
              {label}
            </NavLink>
          ))}
          <button type="button" role="menuitem"
                  onClick={() => { setOpen(false); logout() }}
                  className="flex cursor-pointer items-center gap-2.5 rounded px-2 py-1.5 text-left text-sm text-muted-foreground hover:bg-accent hover:text-foreground">
            <LogOut className="size-4 shrink-0" />
            {t('nav.logout')}
          </button>
          {/* firma: non si traduce, è la stessa riga in tutte le lingue */}
          <div className="mt-0.5 flex items-center justify-center gap-1.5 border-t border-border px-2 pb-1 pt-2 text-[11px] text-muted-foreground">
            <span>Made in</span>
            <FlagIt className="w-4" />
            <span>by{' '}
              <a href="https://www.gdpanalytics.com/" target="_blank" rel="noopener noreferrer"
                 className="font-medium hover:text-foreground hover:underline">GDP Analytics</a>
            </span>
          </div>
        </div>
      )}
    </div>
  )
}

// Layout delle pagine autenticate: menu di navigazione a sinistra, contenuto
// della route a destra. Le card dei job stanno nel menu, così l'avanzamento
// resta visibile da qualunque pagina.
export default function DashboardLayout() {
  const { user, logout } = useAuth()
  const { t } = useTranslation()

  return (
    <JobsProvider>
      <ChatsProvider>
      <OnboardingProvider>
        <div className="flex h-full">
          <nav className="flex w-[250px] flex-none flex-col border-r border-border bg-card">
            <div className="px-4 pb-4 pt-5">
              <Logo subtitle={t('app.subtitle')} />
            </div>

            {/* Impostazioni e le pagine di amministrazione non stanno qui:
                vivono nel menu dell'utente in fondo alla sidebar */}
            <div className="flex flex-col gap-0.5 px-3" data-tour="nav">
              <NavItem to="/new" icon={MessageSquarePlus}>{t('nav.newChat')}</NavItem>
              <NavItem to="/projects" icon={FolderKanban}>{t('nav.projects')}</NavItem>
            </div>

            <div className="mx-3 mt-3 border-t border-border/70" />

            <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-3 pt-5">
              <JobCards />
              <SidebarChats />
            </div>

            <div className="flex h-[42px] flex-none items-center gap-2 border-t border-border px-3">
              <UserMenu user={user} logout={logout} />
              {/* la lingua sta qui e non dentro il menu perché è la cosa da
                  cui si riparte quando l'interfaccia è nella lingua sbagliata:
                  deve essere raggiungibile senza saper leggere i menu */}
              <LanguageSwitcher className="shrink-0" />
            </div>
          </nav>
          <div className="flex min-h-0 min-w-0 flex-1">
            <Outlet />
          </div>
        </div>
      </OnboardingProvider>
      </ChatsProvider>
    </JobsProvider>
  )
}
