import React, { useEffect, useState } from 'react'
import { Trans, useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { Eye, EyeOff, Loader2 } from 'lucide-react'
import { api } from './api.js'
import i18n from './i18n/index.js'
import { LANGUAGES } from '@/i18n/languages.jsx'
import { CategoriesPicker, TermsEditor, cleanTerms, termsInvalid } from './AnonOptions.jsx'
import { Checkbox } from '@/components/ui/checkbox'
import { TriangleAlert } from 'lucide-react'
import { LogoMark } from '@/components/Logo.jsx'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Dialog, DialogContent } from '@/components/ui/dialog'
import ModelRulePicker, { isRuleComplete } from '@/components/ModelRulePicker.jsx'
import ModelAccessEditor from '@/components/ModelAccessEditor.jsx'
import { cn } from '@/lib/utils'

// Installazione guidata: compare sulla pagina di accesso finché non esiste
// nessun utente (GET /api/setup/status). Non si chiude: senza management key
// e senza amministratore l'app non ha niente da mostrare.
//
// Passo 0 (lingua) è bilingue e scritto a mano, come LanguageDialog: non si sa
// ancora chi c'è dall'altra parte. Da lì in avanti tutto passa da t(): la
// scelta finisce su i18next subito e sul profilo dell'admin alla creazione.
//
// Passi 1–8: benvenuto, management key, password admin, categorie da
// anonimizzare, termini globali, regole della chat (obbligo e deroga ZDR),
// modello predefinito (con le sue opzioni), modelli visibili agli utenti
// (tutti o una white list).
// Il wizard NON autentica: alla fine compare il login vero e proprio, con
// username e password precompilati solo se il wizard è stato fatto in questa
// stessa pagina (dopo un F5 la password non c'è più, e l'admin già creato
// viene segnalato allo step 3).
const STEPS = 8

// Categorie ATTIVE di partenza: quelle con meno falsi positivi. Tutte le altre
// del modello nascono spente e si accendono dallo step 4 (o poi da
// Impostazioni). Si salva l'esclusione: excluded = tags − DEFAULT_ON.
// Il gruppo «Cybersecurity» (credenziali e identificativi tecnici, regex di
// formato) nasce tutto acceso: pochi falsi positivi e dati che non devono uscire.
const DEFAULT_ON = new Set(['CF', 'CITY', 'CREDITCARDNUMBER', 'EMAIL', 'FULLNAME', 'IBAN',
                            'ORG', 'PASSWORD', 'PIVA', 'SECRET', 'STREET', 'TELEPHONENUM',
                            'URL', 'USERNAME',
                            'CERTIFICATE', 'CRYPTO_WALLET', 'DEVICE_ID', 'HOSTNAME',
                            'IP_ADDRESS', 'MAC_ADDRESS', 'PASSWORD_HASH', 'PRODUCT_KEY',
                            'SSH_FINGERPRINT', 'SSH_KEY', 'TOTP_SECRET', 'WINDOWS_SID'])

function PwField({ id, label, value, onChange, autoFocus }) {
  const [show, setShow] = useState(false)
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <div className="relative">
        <Input id={id} type={show ? 'text' : 'password'} value={value}
               autoComplete="new-password" autoFocus={autoFocus} className="pr-10"
               onChange={(e) => onChange(e.target.value)} />
        <button type="button" tabIndex={-1}
                className="absolute inset-y-0 right-0 flex w-10 items-center justify-center text-muted-foreground hover:text-foreground"
                onClick={() => setShow((v) => !v)}>
          {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
        </button>
      </div>
    </div>
  )
}

function Dots({ step }) {
  return (
    <div className="flex justify-center gap-1.5" aria-hidden="true">
      {Array.from({ length: STEPS }, (_, i) => (
        <span key={i} className={cn('h-1.5 w-6 rounded-full transition-colors',
                                    i + 1 <= step ? 'bg-primary' : 'bg-border')} />
      ))}
    </div>
  )
}

export default function SetupWizard({ status, onDone }) {
  const [step, setStep] = useState(0)
  const [lang, setLang] = useState(null)
  const [keySaved, setKeySaved] = useState(Boolean(status?.management_key))
  const [key, setKey] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [adminExists, setAdminExists] = useState(Boolean(status?.admin))
  const [recreating, setRecreating] = useState(false)   // «Ricrea admin» premuto
  const [tags, setTags] = useState([])
  const [tagGroups, setTagGroups] = useState(null)   // {cyber: [...]} da /api/tags
  const [excluded, setExcluded] = useState(null)   // null = tag non ancora arrivati
  const [defaultExcluded, setDefaultExcluded] = useState([])
  const [terms, setTerms] = useState([{ text: '', tag: 'CUSTOM' }])
  const [policy, setPolicy] = useState('optional')
  const [allowNonZdr, setAllowNonZdr] = useState(false)
  const [models, setModels] = useState(null)      // null = catalogo non ancora chiesto
  const [modelRule, setModelRule] = useState(null)
  // white list dei modelli per gli utenti non admin; `lockedModel` è il
  // modello a cui porta la regola appena scelta allo step 7 (lo dice il
  // server), che entra nella lista da solo
  const [modelAccess, setModelAccess] = useState({ enabled: false, models: [] })
  const [lockedModel, setLockedModel] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const { t } = useTranslation('setup')

  // i tag del modello (categorie + datalist dei termini): /api/tags è pubblico
  useEffect(() => {
    if (step !== 4 || excluded !== null) return
    api.getTags().then((r) => {
      const all = r.all || []
      setTags(all)
      setTagGroups(r.groups || null)
      const off = all.filter((tag) => !DEFAULT_ON.has(tag)).sort()
      setDefaultExcluded(off)
      setExcluded(off)
    }).catch(() => { setTags([]); setExcluded([]) })
  }, [step, excluded])

  // catalogo per lo step del modello: la chiave è quella dell'admin creato
  useEffect(() => {
    if (step !== 7 || models !== null) return
    api.setupModels().then((r) => setModels(r.models || [])).catch(() => setModels([]))
  }, [step, models])

  // allo step 8 si chiede al server a quale modello porta la regola scelta:
  // con la guidata non è scritto da nessuna parte, si calcola sul catalogo
  useEffect(() => {
    if (step !== 8) return
    if (!modelRule || !isRuleComplete(modelRule)) { setLockedModel(''); return }
    let alive = true
    api.setupResolveModel(modelRule, allowNonZdr)
      .then((r) => { if (alive) setLockedModel(r.model || '') })
      .catch(() => { if (alive) setLockedModel('') })
    return () => { alive = false }
  }, [step, modelRule, allowNonZdr])

  function pickLang(code) {
    setLang(code)
    i18n.changeLanguage(code)
    setStep(1)
  }

  async function saveKey() {
    setBusy(true)
    setError('')
    try {
      await api.setupManagementKey(key.trim())
      setKeySaved(true)
      setKey('')
      toast.success(t('key.saved'))
      setStep(3)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  async function createAdmin(e) {
    e.preventDefault()
    if (password !== confirm) {
      setError(t('admin.mismatch'))
      return
    }
    setBusy(true)
    setError('')
    try {
      await api.setupAdmin(password, lang, adminExists && recreating)
      setAdminExists(true)
      setRecreating(false)
      toast.success(t('admin.created'))
      setStep(4)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  async function finish() {
    setBusy(true)
    setError('')
    try {
      await api.setupFinish({
        excluded_tags: excluded || [],
        custom_terms: cleanTerms(terms),
        chat_anonymization_policy: policy,
        chat_allow_non_zdr: allowNonZdr,
        default_model_rule: modelRule,
        model_access: modelAccess,
      })
      toast.success(t('terms.done'))
      // password solo se scelta in questa pagina: dopo un F5 non c'è
      onDone({ username: 'admin', password: password || '' })
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const pwOk = password.length >= 8 && confirm.length >= 8

  return (
    <Dialog open>
      <DialogContent hideClose className="sm:max-w-lg"
                     onInteractOutside={(e) => e.preventDefault()}
                     onEscapeKeyDown={(e) => e.preventDefault()}
                     aria-label="BlockingBear setup">
        {step === 0 && (
          <div className="flex flex-col gap-5">
            <div className="flex flex-col items-center gap-3 pt-1 text-center">
              <LogoMark className="size-16" />
              <p className="text-base font-semibold leading-tight">
                Scegli la lingua
                <span className="block text-muted-foreground">Choose your language</span>
              </p>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              {LANGUAGES.map(({ code, label, Flag }) => (
                <button key={code} type="button" onClick={() => pickLang(code)}
                        className="flex cursor-pointer flex-col items-center gap-2.5 rounded-lg border-2 border-border p-4 transition-colors hover:border-primary/60 hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40">
                  <Flag className="w-14" />
                  <span className="font-medium">{label}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {step >= 1 && (
          <div className="flex flex-col gap-5">
            <div className="flex items-center gap-3">
              <LogoMark className="size-10" />
              <div className="min-w-0 leading-tight">
                <div className="text-[15px] font-semibold tracking-tight">BlockingBear</div>
                <div className="text-xs text-muted-foreground">
                  {t('title')} · {t('step', { n: step, total: STEPS })}
                </div>
              </div>
            </div>
            <Dots step={step} />

            {step === 1 && (
              <div className="flex flex-col items-center gap-4 text-center">
                <LogoMark className="size-28" />
                <h2 className="text-xl font-bold tracking-tight">{t('welcome.title')}</h2>
                <p className="text-sm text-muted-foreground">{t('welcome.text')}</p>
                <Button className="mt-2 w-full" onClick={() => setStep(2)}>{t('welcome.start')}</Button>
              </div>
            )}

            {step === 2 && (
              <div className="flex flex-col gap-4">
                <h2 className="text-lg font-semibold">{t('key.title')}</h2>
                <p className="text-sm text-muted-foreground">
                  <Trans i18nKey="key.intro" ns="setup"
                         components={{ u: <u className="font-semibold underline" /> }} />
                </p>
                <div className="rounded-md border border-border bg-muted/20 p-3 text-sm">
                  <div className="mb-1.5 font-medium">{t('key.howTitle')}</div>
                  <ol className="list-decimal space-y-1 pl-5 text-muted-foreground">
                    <li>
                      <Trans i18nKey="key.how1" ns="setup" components={{
                        a: <a href="https://openrouter.ai/settings/management-keys" target="_blank"
                              rel="noreferrer" className="underline" />,
                      }} />
                    </li>
                    <li>{t('key.how2')}</li>
                    <li>{t('key.how3')}</li>
                  </ol>
                </div>
                {keySaved && (
                  <p className="rounded-md border border-green-600/40 bg-green-600/10 px-3 py-2 text-sm text-green-700 dark:text-green-400">
                    {t('key.already')}
                  </p>
                )}
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="setup-key">{t('key.label')}</Label>
                  <Input id="setup-key" type="password" autoComplete="off" autoFocus
                         placeholder="sk-or-…" value={key}
                         onChange={(e) => { setKey(e.target.value); setError('') }}
                         onKeyDown={(e) => { if (e.key === 'Enter' && key.trim()) saveKey() }} />
                </div>
                {error && <p className="text-sm text-destructive">{error}</p>}
                <div className="flex justify-between gap-2">
                  <Button type="button" variant="ghost" disabled={busy} onClick={() => setStep(1)}>
                    {t('back')}
                  </Button>
                  <div className="flex gap-2">
                    {keySaved && (
                      <Button type="button" variant="outline" disabled={busy} onClick={() => setStep(3)}>
                        {t('key.keep')}
                      </Button>
                    )}
                    <Button type="button" disabled={busy || !key.trim()} onClick={saveKey}>
                      {busy && <Loader2 className="animate-spin" />}
                      {t('key.save')}
                    </Button>
                  </div>
                </div>
              </div>
            )}

            {step === 3 && adminExists && !recreating && (
              <div className="flex flex-col gap-4">
                <h2 className="text-lg font-semibold">{t('admin.title')}</h2>
                <p className="rounded-md border border-green-600/40 bg-green-600/10 px-3 py-2 text-sm text-green-700 dark:text-green-400">
                  {t('admin.already')}
                </p>
                <div className="flex justify-between gap-2">
                  <Button type="button" variant="ghost" onClick={() => setStep(2)}>{t('back')}</Button>
                  <div className="flex gap-2">
                    <Button type="button" variant="outline" onClick={() => setRecreating(true)}>
                      {t('admin.recreate')}
                    </Button>
                    <Button type="button" onClick={() => setStep(4)}>{t('next')}</Button>
                  </div>
                </div>
              </div>
            )}

            {step === 3 && (!adminExists || recreating) && (
              <form onSubmit={createAdmin} className="flex flex-col gap-4">
                <h2 className="text-lg font-semibold">{t('admin.title')}</h2>
                <p className="text-sm text-muted-foreground">
                  <Trans i18nKey="admin.intro" ns="setup" components={{ b: <b /> }} />
                </p>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="setup-user">{t('admin.username')}</Label>
                  <Input id="setup-user" value="admin" disabled readOnly />
                </div>
                <PwField id="setup-pw" label={t('admin.password')} value={password}
                         onChange={setPassword} autoFocus />
                <PwField id="setup-pw2" label={t('admin.confirm')} value={confirm}
                         onChange={setConfirm} />
                {error && <p className="text-sm text-destructive">{error}</p>}
                <div className="flex justify-between gap-2">
                  <Button type="button" variant="ghost" disabled={busy}
                          onClick={() => (recreating ? setRecreating(false) : setStep(2))}>
                    {t('back')}
                  </Button>
                  <Button type="submit" disabled={busy || !pwOk}>
                    {busy && <Loader2 className="animate-spin" />}
                    {t(recreating ? 'admin.recreate' : 'admin.create')}
                  </Button>
                </div>
              </form>
            )}

            {step === 4 && (
              <div className="flex flex-col gap-4">
                <h2 className="text-lg font-semibold">{t('categories.title')}</h2>
                <p className="text-sm text-muted-foreground">{t('categories.intro')}</p>
                {excluded === null
                  ? <div className="text-sm text-muted-foreground">{t('state.loading', { ns: 'common' })}</div>
                  : <CategoriesPicker tags={tags} groups={tagGroups} excluded={excluded} onChange={setExcluded}
                                      defaults={defaultExcluded} defaultLabel={t('categories.default')} />}
                {excluded !== null && excluded.length === 0 && tags.length > 0 && (
                  <div className="flex items-start gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-sm text-amber-800 dark:text-amber-300">
                    <TriangleAlert className="mt-0.5 size-4 shrink-0" />
                    <span>{t('categories.allWarning')}</span>
                  </div>
                )}
                <div className="flex justify-between gap-2">
                  <Button type="button" variant="ghost" disabled={busy} onClick={() => setStep(3)}>
                    {t('back')}
                  </Button>
                  <Button type="button" disabled={excluded === null} onClick={() => setStep(5)}>
                    {t('next')}
                  </Button>
                </div>
              </div>
            )}

            {step === 5 && (
              <div className="flex flex-col gap-4">
                <h2 className="text-lg font-semibold">{t('terms.title')}</h2>
                <p className="text-sm text-muted-foreground">{t('terms.intro')}</p>
                <TermsEditor terms={terms} tags={tags} onChange={setTerms}
                             datalistId="setup-tag-list" />
                <div className="flex justify-between gap-2">
                  <Button type="button" variant="ghost" disabled={busy} onClick={() => setStep(4)}>
                    {t('back')}
                  </Button>
                  <Button type="button" disabled={termsInvalid(terms)} onClick={() => setStep(6)}>
                    {t('next')}
                  </Button>
                </div>
              </div>
            )}

            {step === 6 && (
              <div className="flex flex-col gap-4">
                <h2 className="text-lg font-semibold">{t('chat.title')}</h2>
                <p className="text-sm text-muted-foreground">{t('chat.intro')}</p>
                <div className="flex flex-col gap-1.5 rounded-md border border-border bg-card p-3 shadow-sm">
                  <Label htmlFor="setup-policy">{t('chat.policy')}</Label>
                  <select id="setup-policy"
                          className="h-9 w-56 rounded-md border border-input bg-card px-3 text-sm"
                          value={policy} onChange={(e) => setPolicy(e.target.value)}>
                    <option value="optional">{t('chat.policyOptional')}</option>
                    <option value="required">{t('chat.policyRequired')}</option>
                  </select>
                  <p className="text-xs text-muted-foreground">{t('chat.policyHelp')}</p>
                </div>
                <div className="flex flex-col gap-1.5 rounded-md border border-border bg-card p-3 shadow-sm">
                  <label className="flex cursor-pointer items-center gap-2 text-sm font-medium">
                    <Checkbox checked={allowNonZdr} onCheckedChange={(v) => setAllowNonZdr(Boolean(v))} />
                    {t('chat.zdr')}
                  </label>
                  <p className="text-xs text-muted-foreground">{t('chat.zdrHelp')}</p>
                </div>
                <div className="flex justify-between gap-2">
                  <Button type="button" variant="ghost" disabled={busy} onClick={() => setStep(5)}>
                    {t('back')}
                  </Button>
                  <Button type="button" onClick={() => setStep(7)}>{t('next')}</Button>
                </div>
              </div>
            )}

            {step === 7 && (
              <div className="flex flex-col gap-4">
                <h2 className="text-lg font-semibold">{t('model.title')}</h2>
                <p className="text-sm text-muted-foreground">{t('model.intro')}</p>
                {models === null
                  ? <div className="text-sm text-muted-foreground">{t('state.loading', { ns: 'common' })}</div>
                  : <ModelRulePicker models={models} value={modelRule} onChange={setModelRule}
                                     allowNonZdr={allowNonZdr}
                                     resolveRule={(rule) => api.setupResolveModel(rule, allowNonZdr)}
                                     emptyLabel={t('model.none')} withOptions inline />}
                {models !== null && models.length === 0 && (
                  <p className="text-xs text-muted-foreground">{t('model.noCatalog')}</p>
                )}
                <div className="flex justify-between gap-2">
                  <Button type="button" variant="ghost" disabled={busy} onClick={() => setStep(6)}>
                    {t('back')}
                  </Button>
                  <Button type="button" disabled={!isRuleComplete(modelRule)}
                          onClick={() => setStep(8)}>
                    {t('next')}
                  </Button>
                </div>
              </div>
            )}

            {step === 8 && (
              <div className="flex flex-col gap-4">
                <h2 className="text-lg font-semibold">{t('access.title')}</h2>
                <p className="text-sm text-muted-foreground">{t('access.intro')}</p>
                <ModelAccessEditor models={models || []} value={modelAccess}
                                   onChange={setModelAccess} locked={lockedModel}
                                   allowNonZdr={allowNonZdr} />
                {error && <p className="text-sm text-destructive">{error}</p>}
                <div className="flex justify-between gap-2">
                  <Button type="button" variant="ghost" disabled={busy} onClick={() => setStep(7)}>
                    {t('back')}
                  </Button>
                  <Button type="button" disabled={busy} onClick={finish}>
                    {busy && <Loader2 className="animate-spin" />}
                    {t('chat.finish')}
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
