import React, { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { Eye, EyeOff, Loader2 } from 'lucide-react'
import { api, setToken } from './api.js'
import i18n, { LANGS } from './i18n/index.js'
import { useAuth } from './auth.jsx'
import { LogoMark } from '@/components/Logo.jsx'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { usePageTitle } from '@/lib/usePageTitle.js'
import SetupWizard from './SetupWizard.jsx'
import { FlagIt } from '@/i18n/languages.jsx'

// Unica pagina SENZA traduzione, e di proposito: qui non si sa ancora chi c'è
// dall'altra parte, quindi nemmeno che lingua parla — la si chiede dopo il
// login, quando c'è un profilo su cui salvarla (vedi LanguageDialog).
//
// Per starci dentro la pagina dice quasi nulla: il nome dell'app e due campi.
// «Username», «Password» e «Login» sono le stesse parole in italiano e in
// inglese, quindi non c'è niente da tradurre né da scrivere due volte.
// Sottotitolo e nota sulla riservatezza sono spariti per lo stesso motivo:
// erano frasi vere e proprie, e le ritrova comunque una volta dentro.

// I messaggi d'errore invece una frase la sono, e sono anche gli unici testi
// che l'utente può incontrare qui: si mostrano in tutt'e due le lingue,
// prendendo la stessa chiave (`error.<codice>`) dai due cataloghi. Se il
// codice manca — errore nuovo o non convertito — resta il testo del server,
// che è uno solo.
function bilingualError(err) {
  if (!err) return []
  if (!err.code) return [err.message]
  const texts = LANGS.map(
    (lng) => i18n.getFixedT(lng)(`error.${err.code}`, { defaultValue: err.message }))
  return [...new Set(texts)]
}

export default function Login() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPw, setShowPw] = useState(false)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  // installazione ancora da completare? null = non si sa ancora
  const [setup, setSetup] = useState(null)
  const { user, checking, setUser } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  usePageTitle(null)                      // solo «BlockingBear», senza pagina
  // dopo il login si torna alla pagina che aveva richiesto l'autenticazione
  const from = location.state?.from?.pathname || '/new'

  useEffect(() => {
    if (user) navigate(from, { replace: true })
  }, [user, from, navigate])

  // Prima di mostrare i campi: se non esiste nessun utente si apre il wizard
  // di installazione, che non si chiude finché non ha creato l'admin. Se il
  // server non risponde si mostra comunque il login: l'errore lo darà lui.
  useEffect(() => {
    api.setupStatus().then(setSetup).catch(() => setSetup({ needed: false }))
  }, [])

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const res = await api.login(username, password)
      setToken(res.token)
      // `lang` va portato dentro l'utente: è da lì che AuthProvider allinea
      // l'interfaccia al profilo, o scopre che la lingua va ancora chiesta.
      // `must_change_password` idem: è quello che fa comparire la schermata
      // di cambio password obbligatorio (ForcePasswordGate).
      setUser({ username: res.username, role: res.role, lang: res.lang,
                must_change_password: res.must_change_password,
                tour_done: res.tour_done })
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  if (checking || setup === null) {
    return (
      <div className="grid h-full place-items-center text-muted-foreground">
        <Loader2 className="size-5 animate-spin" />
      </div>
    )
  }

  // Installazione da completare: in pagina c'è SOLO il wizard, niente logo né
  // campi di accesso sotto. Il wizard finisce già autenticato (la risposta di
  // /api/setup/admin è quella del login) e da lì vale il percorso normale.
  if (setup.needed) {
    return (
      <div className="h-full">
        <SetupWizard status={setup}
                     onDone={({ username: u, password: p }) => {
                       // il wizard non autentica: si passa dal login, con i
                       // campi precompilati se la password è stata scelta qui
                       setUsername(u)
                       setPassword(p)
                       setSetup({ needed: false })
                     }} />
      </div>
    )
  }

  return (
    <div className="relative flex min-h-full flex-col items-center justify-center gap-6 px-4 py-8">
      <div className="flex w-full max-w-sm flex-col items-center gap-6">
        {/* In primo piano: marchio grande e nome dell'app. */}
        <div className="flex flex-col items-center gap-2 text-center">
          <LogoMark className="size-32 sm:size-40" />
          <h1 className="text-3xl font-bold tracking-tight sm:text-4xl">BlockingBear</h1>
        </div>

        <form
          onSubmit={submit}
          className="flex w-full flex-col gap-5 rounded-xl border border-border bg-card p-5 sm:p-8 shadow-[0_10px_40px_rgba(20,28,46,0.08)]"
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="login-user">Username</Label>
            <Input id="login-user" value={username} autoComplete="username"
                   onChange={(e) => setUsername(e.target.value)} />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="login-pw">Password</Label>
            <div className="relative">
              <Input id="login-pw" type={showPw ? 'text' : 'password'} value={password}
                     autoComplete="current-password" className="pr-10"
                     onChange={(e) => setPassword(e.target.value)} />
              <button type="button" tabIndex={-1}
                      className="absolute inset-y-0 right-0 flex w-10 items-center justify-center text-muted-foreground hover:text-foreground"
                      title={showPw ? 'Nascondi password / Hide password' : 'Mostra password / Show password'}
                      aria-label={showPw ? 'Nascondi password / Hide password' : 'Mostra password / Show password'}
                      onClick={() => setShowPw((v) => !v)}>
                {showPw ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
              </button>
            </div>
          </div>

          {error && (
            <div className="text-sm text-destructive">
              {bilingualError(error).map((line) => <div key={line}>{line}</div>)}
            </div>
          )}

          <Button type="submit" disabled={busy || !username || !password} className="w-full">
            {busy && <Loader2 className="animate-spin" />}
            Login
          </Button>
        </form>
      </div>
      {/* firma, la stessa del menu utente nella sidebar: non si traduce */}
      <div className="flex items-center justify-center gap-1.5 text-[11px] text-muted-foreground">
        <span>Made in</span>
        <FlagIt className="w-4" />
        <span>by{' '}
          <a href="https://www.gdpanalytics.com/" target="_blank" rel="noopener noreferrer"
             className="font-medium hover:text-foreground hover:underline">GDP Analytics</a>
        </span>
      </div>
    </div>
  )
}
