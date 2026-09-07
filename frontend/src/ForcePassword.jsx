import React from 'react'
import { toast } from 'sonner'
import { useTranslation } from 'react-i18next'
import { useAuth } from './auth.jsx'
import { Logo } from '@/components/Logo.jsx'
import { Button } from '@/components/ui/button'
import PasswordChangeForm from '@/components/PasswordChangeForm.jsx'
import { usePageTitle } from '@/lib/usePageTitle.js'

// Cambio password OBBLIGATORIO al primo accesso (flag dell'admin alla
// creazione dell'account): finché non è fatto, questa schermata prende il
// posto di tutta l'app. Non è solo cosmesi: il backend risponde 403
// password_change_required a qualunque altro endpoint (vedi auth.py), quindi
// non c'è niente da nascondere — c'è solo questo da fare.
//
// Il popup della lingua qui NON compare (LanguageProvider non lo apre finché
// il flag è alzato): prima l'obbligo, poi la domanda — arriva al primo
// ingresso nell'app, una volta sola.
export default function ForcePassword() {
  const { user, setUser, logout } = useAuth()
  const { t } = useTranslation('settings')
  usePageTitle(t('password.title'))

  return (
    <div className="grid min-h-full place-items-center px-4 py-6">
      <div className="w-full max-w-sm">
        <div className="flex flex-col gap-5 rounded-xl border border-border bg-card p-5 sm:p-8 shadow-[0_10px_40px_rgba(20,28,46,0.08)]">
          <Logo />
          <div>
            <h2 className="text-base font-semibold">{t('password.forcedTitle')}</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              {t('password.forcedIntro')}
            </p>
          </div>
          <PasswordChangeForm autoFocus
            onDone={() => {
              toast.success(t('password.saved'))
              // il flag cade anche lato server: basta aggiornare la copia
              // locale e le PrivateRoute mostrano l'app
              setUser({ ...user, must_change_password: false })
            }} />
        </div>
        <div className="mt-3 text-center">
          <Button variant="ghost" size="sm" className="text-muted-foreground"
                  onClick={logout}>
            {t('nav.logout', { ns: 'common' })}
          </Button>
        </div>
      </div>
    </div>
  )
}

// Da mettere attorno alle route autenticate: se l'utente deve ancora
// cambiare la password, al posto dell'app c'è solo la schermata qui sopra.
export function ForcePasswordGate({ children }) {
  const { user } = useAuth()
  if (user?.must_change_password) return <ForcePassword />
  return children
}
