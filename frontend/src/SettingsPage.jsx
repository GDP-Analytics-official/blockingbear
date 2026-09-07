import React, { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { Loader2 } from 'lucide-react'
import AnonOptions, { TermsEditor, cleanOptions, cleanTerms, optionsInvalid, termsInvalid } from './AnonOptions.jsx'
import { api } from './api.js'
import { useAuth } from './auth.jsx'
import ModelRulePicker, { isRuleComplete } from '@/components/ModelRulePicker.jsx'
import ModelAccessEditor from '@/components/ModelAccessEditor.jsx'
import PasswordChangeForm from '@/components/PasswordChangeForm.jsx'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { usePageTitle } from '@/lib/usePageTitle.js'
import { Trans, useTranslation } from 'react-i18next'
import { LANGUAGES } from '@/i18n/languages.jsx'
import { useLanguage } from '@/i18n/LanguageProvider.jsx'
import { useOnboarding } from '@/onboarding/OnboardingProvider.jsx'
import { cn } from '@/lib/utils'

function SectionTitle({ children }) {
  return (
    <h3 className="border-b border-border pb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
      {children}
    </h3>
  )
}

// La lingua si cambia anche da qui, non solo dalle bandierine del menu: è
// dove si va a cercare una preferenza quando non si sa che sta là in fondo.
// Stesso stato, stesso salvataggio — è la stessa scelta vista da due punti.
function LanguagePreference() {
  const { t } = useTranslation()
  const { lang, choose } = useLanguage()

  return (
    <section className="flex flex-col gap-4">
      <SectionTitle>{t('language.title')}</SectionTitle>
      <div className="flex flex-wrap gap-3">
        {LANGUAGES.map(({ code, label, Flag }) => (
          <button key={code} type="button" onClick={() => choose(code)}
                  aria-pressed={code === lang}
                  className={cn(
                    'flex cursor-pointer items-center gap-2.5 rounded-lg border-2 px-4 py-2.5 text-sm transition-colors',
                    code === lang
                      ? 'border-primary/60 bg-accent font-medium'
                      : 'border-border hover:bg-accent')}>
            <Flag className="w-7" />
            {label}
          </button>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">{t('language.help')}</p>
    </section>
  )
}

// Il tutorial del primo accesso, da rivedere: azzera il flag sul profilo e
// porta alla prima pagina, dove parte il primo capitolo.
function TutorialPreference() {
  const { t } = useTranslation('settings')
  const { restart } = useOnboarding()
  const navigate = useNavigate()

  async function replay() {
    if (await restart()) navigate('/new')
    else toast.error(t('tour.failed'))
  }

  return (
    <section className="flex flex-col gap-4">
      <SectionTitle>{t('tour.title')}</SectionTitle>
      <div>
        <Button type="button" variant="outline" onClick={replay}>{t('tour.replay')}</Button>
      </div>
      <p className="text-xs text-muted-foreground">{t('tour.help')}</p>
    </section>
  )
}

// Cambio password: per tutti (admin compreso), nessuna email di conferma —
// l'app è locale, basta conoscere quella attuale. Il form è lo stesso della
// schermata del cambio obbligatorio al primo accesso (ForcePassword).
function ChangePassword() {
  const { t } = useTranslation('settings')
  return (
    <section className="flex flex-col gap-4">
      <SectionTitle>{t('password.title')}</SectionTitle>
      <p className="text-sm text-muted-foreground">{t('password.intro')}</p>
      <PasswordChangeForm onDone={() => toast.success(t('password.saved'))} />
    </section>
  )
}

// Default di anonimizzazione GLOBALI (categorie attive + termini specifici):
// l'admin li fissa qui e valgono per tutti gli utenti. I termini personali di
// ciascuno stanno in MyAnonTerms e si SOMMANO a questi.
// `onSaved` porta la lista globale appena salvata a MyAnonTerms, che la mostra
// in sola lettura: le due sezioni la leggono con due chiamate distinte, e senza
// questo l'admin vedrebbe l'elenco vecchio finché non ricarica la pagina.
function AnonDefaults({ onSaved }) {
  const [tags, setTags] = useState(null)
  const [groups, setGroups] = useState(null)   // {cyber: [...]} da /api/tags
  const [saved, setSaved] = useState(null)    // ultimo valore salvato
  const [draft, setDraft] = useState(null)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const { t } = useTranslation('settings')

  useEffect(() => {
    Promise.all([api.getTags(), api.getAnonDefaults()])
      .then(([tagsRes, d]) => {
        setTags(tagsRes.all); setGroups(tagsRes.groups || null); setSaved(d); setDraft(d)
      })
      .catch((e) => setError(e.message))
  }, [])

  if (!draft) {
    return (
      <section className="flex flex-col gap-4">
        <SectionTitle>{t('anonDefaults.title')}</SectionTitle>
        {error ? <div className="text-sm text-destructive">{error}</div>
               : <div className="text-sm text-muted-foreground">
                   {t('state.loading', { ns: 'common' })}
                 </div>}
      </section>
    )
  }

  const dirty = JSON.stringify(draft) !== JSON.stringify(saved)

  async function save() {
    setSaving(true)
    try {
      const updated = await api.saveAnonDefaults(cleanOptions(draft))
      setSaved(updated)
      setDraft(updated)
      onSaved?.(updated.custom_terms || [])
      toast.success(t('anonDefaults.saved'))
    } catch (e) {
      toast.error(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="flex flex-col gap-4">
      <SectionTitle>{t('anonDefaults.title')}</SectionTitle>
      <p className="text-sm text-muted-foreground">
        <Trans i18nKey="anonDefaults.intro" ns="settings" components={{ b: <b /> }} />
      </p>
      <div className="flex flex-col gap-1.5 rounded-md border border-border bg-muted/20 p-3">
        <Label htmlFor="chat-anonymization-policy">{t('anonDefaults.policy')}</Label>
        <select id="chat-anonymization-policy"
                className="h-9 w-full sm:w-56 rounded-md border border-input bg-card px-3 text-sm"
                value={draft.chat_anonymization_policy || 'optional'}
                onChange={(e) => setDraft({
                  ...draft, chat_anonymization_policy: e.target.value,
                })}>
          <option value="required">{t('anonDefaults.policyRequired')}</option>
          <option value="optional">{t('anonDefaults.policyOptional')}</option>
        </select>
        <p className="text-xs text-muted-foreground">{t('anonDefaults.policyHelp')}</p>
      </div>
      <AnonOptions tags={tags} groups={groups} value={draft} onChange={setDraft} />
      <div className="flex items-center gap-2">
        <Button type="button" disabled={!dirty || optionsInvalid(draft) || saving} onClick={save}>
          {saving && <Loader2 className="animate-spin" />}
          {saving ? t('saving') : t('actions.save', { ns: 'common' })}
        </Button>
        {dirty && (
          <Button type="button" variant="outline" onClick={() => setDraft(saved)}>
            {t('revert')}
          </Button>
        )}
      </div>
    </section>
  )
}

// I MIEI termini: la lista personale, che ognuno gestisce da sé (admin
// compreso — anche lui ha la sua, distinta da quella globale che fissa per
// tutti). Vale solo per i propri progetti e le proprie chat; quella globale
// resta comunque applicata sopra, e qui si vede in sola lettura perché una
// lista che mostra solo i propri termini lascerebbe credere che sia tutto
// quello che viene coperto.
function MyAnonTerms({ globals: freshGlobals }) {
  const [tags, setTags] = useState([])
  const [saved, setSaved] = useState(null)      // payload completo dal server
  const [draft, setDraft] = useState(null)      // solo la MIA lista
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const { t } = useTranslation('settings')

  function load(d) {
    setSaved(d)
    setDraft(d.my_custom_terms || [])
  }

  useEffect(() => {
    api.getAnonDefaults().then(load).catch((e) => setError(e.message))
    api.getTags().then((r) => setTags(r.all)).catch(() => {})
  }, [])

  if (!draft) {
    return (
      <section className="flex flex-col gap-4">
        <SectionTitle>{t('myTerms.title')}</SectionTitle>
        {error ? <div className="text-sm text-destructive">{error}</div>
               : <div className="text-sm text-muted-foreground">
                   {t('state.loading', { ns: 'common' })}
                 </div>}
      </section>
    )
  }

  // se l'admin ha appena salvato la lista globale qui sopra, vale quella: la
  // nostra copia arriva da una GET fatta al montaggio (vedi AnonDefaults.onSaved)
  const globals = freshGlobals || saved.custom_terms || []
  const dirty = JSON.stringify(draft) !== JSON.stringify(saved.my_custom_terms || [])

  async function save() {
    setSaving(true)
    try {
      load(await api.saveMyAnonTerms(cleanTerms(draft)))
      toast.success(t('myTerms.saved'))
    } catch (e) {
      toast.error(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="flex flex-col gap-4">
      <SectionTitle>{t('myTerms.title')}</SectionTitle>
      <p className="text-sm text-muted-foreground">
        <Trans i18nKey="myTerms.intro" ns="settings" components={{ b: <b /> }} />
      </p>
      <TermsEditor terms={draft} tags={tags} datalistId="my-anon-tag-list"
                   onChange={setDraft} />

      {globals.length > 0 && (
        <div className="rounded-md border border-border bg-accent/40 p-3">
          <div className="text-xs font-semibold">{t('myTerms.globalsTitle')}</div>
          <p className="mt-1 text-xs text-muted-foreground">
            {globals.map((g) => g.text).join(', ')}
          </p>
          <p className="mt-1 text-[11px] text-muted-foreground">
            {t('myTerms.globalsHelp')}
          </p>
        </div>
      )}

      <div className="flex items-center gap-2">
        <Button type="button" disabled={!dirty || termsInvalid(draft) || saving}
                onClick={save}>
          {saving && <Loader2 className="animate-spin" />}
          {saving ? t('saving') : t('actions.save', { ns: 'common' })}
        </Button>
        {dirty && (
          <Button type="button" variant="outline"
                  onClick={() => setDraft(saved.my_custom_terms || [])}>
            {t('revert')}
          </Button>
        )}
      </div>
    </section>
  )
}

// Modello predefinito della chat: due livelli, la stessa forma. Ognuno ha il
// suo (admin compreso) e chi resta su «default» segue quello che l'admin ha
// fissato per l'installazione. Si salva una regola, non un id: il modello
// vero si risolve alla creazione di ogni chat, e il server dice qui a quale
// si risolverebbe adesso.
// `allowNonZdr`: la deroga ZDR come la vede il chiamante (l'admin la ha nei
// parametri di esercizio appena salvati, così un cambio nella stessa pagina
// si riflette subito); se non arriva vale quella che il server manda col
// payload, l'unica leggibile da un utente normale.
// `catalog` {models, allowed}: il catalogo lo carica la pagina, una volta per
// tutte le sezioni; `allowed` è la white list per chi guarda (null = tutti) e
// restringe il selettore della regola personale come poi in chat.
// `onSaved`: dopo un salvataggio, la sezione della white list ricarica il
// modello «bloccato» (segue la regola madre appena cambiata).
function DefaultModel({ isAdmin, allowNonZdr, catalog, onSaved }) {
  const [saved, setSaved] = useState(null)    // {rule, personal, resolved, allow_non_zdr}
  const models = catalog.models
  const [personal, setPersonal] = useState(null)
  const [mother, setMother] = useState(null)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const { t } = useTranslation('settings')

  function load(d) {
    setSaved(d)
    setPersonal(d.personal)
    setMother(d.rule)
  }

  useEffect(() => {
    api.getDefaultModel().then(load).catch((e) => setError(e.message))
  }, [])

  if (!saved) {
    return (
      <section className="flex flex-col gap-4">
        <SectionTitle>{t('defaultModel.title')}</SectionTitle>
        {error ? <div className="text-sm text-destructive">{error}</div>
               : <div className="text-sm text-muted-foreground">
                   {t('state.loading', { ns: 'common' })}
                 </div>}
      </section>
    )
  }

  const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null)
  const dirty = !same(personal, saved.personal)
                || (isAdmin && !same(mother, saved.rule))
  // una guidata a metà o un fisso senza modello non si possono salvare
  const complete = isRuleComplete(personal) && (!isAdmin || isRuleComplete(mother))
  const resolved = models.find((m) => m.id === saved.resolved)
  const nonZdrOk = allowNonZdr ?? !!saved.allow_non_zdr

  async function save() {
    setSaving(true)
    try {
      let updated = saved
      if (isAdmin && !same(mother, saved.rule)) {
        updated = await api.setDefaultModel(mother)
      }
      if (!same(personal, saved.personal)) {
        updated = await api.setMyDefaultModel(personal)
      }
      load(updated)
      onSaved?.()
      toast.success(t('defaultModel.saved'))
    } catch (e) {
      toast.error(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="flex flex-col gap-4">
      <SectionTitle>{t('defaultModel.title')}</SectionTitle>
      <p className="text-sm text-muted-foreground">{t('defaultModel.intro')}</p>

      <div className="flex flex-col gap-1.5">
        <Label>{t('defaultModel.mine')}</Label>
        <ModelRulePicker models={models} value={personal} onChange={setPersonal}
                         resolveRule={api.resolveDefaultModel} allowNonZdr={nonZdrOk}
                         allowedIds={catalog.allowed}
                         emptyLabel={t('defaultModel.mineEmpty')} />
        <p className="text-xs text-muted-foreground">
          {saved.resolved
            ? <Trans i18nKey="defaultModel.resolved" ns="settings"
                     values={{ model: resolved?.name || saved.resolved }}
                     components={{ b: <b /> }} />
            : t('defaultModel.resolvedNone')}
        </p>
      </div>

      {isAdmin && (
        <div className="flex flex-col gap-1.5 rounded-md border border-border bg-accent/40 p-3">
          <Label>{t('defaultModel.admin')}</Label>
          <ModelRulePicker models={models} value={mother} onChange={setMother}
                           resolveRule={api.resolveDefaultModel} allowNonZdr={nonZdrOk}
                           emptyLabel={t('defaultModel.adminEmpty')} withOptions />
        </div>
      )}

      <div className="flex items-center gap-2">
        <Button type="button" disabled={!dirty || !complete || saving} onClick={save}>
          {saving && <Loader2 className="animate-spin" />}
          {saving ? t('saving') : t('actions.save', { ns: 'common' })}
        </Button>
        {dirty && (
          <Button type="button" variant="outline" onClick={() => load(saved)}>
            {t('revert')}
          </Button>
        )}
      </div>
    </section>
  )
}

// Modelli visibili agli utenti non admin (white list). Si salva {enabled,
// models}; `locked` — il modello predefinito dell'installazione, che entra
// nella lista da solo — arriva dal server e si ricarica quando la sezione
// del modello predefinito salva (prop refreshKey), senza toccare la bozza.
function ModelAccess({ models, allowNonZdr, refreshKey }) {
  const [saved, setSaved] = useState(null)    // {enabled, models, locked}
  const [draft, setDraft] = useState(null)    // {enabled, models}
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const { t } = useTranslation('settings')

  useEffect(() => {
    api.getModelAccess().then((d) => {
      setSaved(d)
      setDraft((cur) => cur ?? { enabled: d.enabled, models: d.models })
    }).catch((e) => setError(e.message))
  }, [refreshKey])

  if (!saved || !draft) {
    return (
      <section className="flex flex-col gap-4">
        <SectionTitle>{t('modelAccess.title')}</SectionTitle>
        {error ? <div className="text-sm text-destructive">{error}</div>
               : <div className="text-sm text-muted-foreground">
                   {t('state.loading', { ns: 'common' })}
                 </div>}
      </section>
    )
  }

  const dirty = draft.enabled !== saved.enabled
                || JSON.stringify(draft.models) !== JSON.stringify(saved.models)

  async function save() {
    setSaving(true)
    try {
      const d = await api.setModelAccess(draft.enabled, draft.models)
      setSaved(d)
      setDraft({ enabled: d.enabled, models: d.models })
      toast.success(t('modelAccess.saved'))
    } catch (e) {
      toast.error(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="flex flex-col gap-4">
      <SectionTitle>{t('modelAccess.title')}</SectionTitle>
      <p className="text-sm text-muted-foreground">{t('modelAccess.intro')}</p>
      <ModelAccessEditor models={models} value={draft} onChange={setDraft}
                         locked={saved.locked} allowNonZdr={allowNonZdr} />
      <div className="flex items-center gap-2">
        <Button type="button" disabled={!dirty || saving} onClick={save}>
          {saving && <Loader2 className="animate-spin" />}
          {saving ? t('saving') : t('actions.save', { ns: 'common' })}
        </Button>
        {dirty && (
          <Button type="button" variant="outline"
                  onClick={() => setDraft({ enabled: saved.enabled, models: saved.models })}>
            {t('revert')}
          </Button>
        )}
      </div>
    </section>
  )
}

// Pagina impostazioni. Aperta a tutti, ma quasi tutto è riservato all'admin:
// l'utente normale ci trova solo le sue preferenze (il modello predefinito
// della chat). I parametri di esercizio arrivano dal backend già descritti
// (sezione, etichetta, help, default, minimo): un parametro nuovo aggiunto a
// settings_store.REGISTRY compare qui senza toccare il frontend.
export default function SettingsPage() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [settings, setSettings] = useState(null)
  const [draft, setDraft] = useState({})      // key -> stringa nell'input
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  // la lista globale appena salvata: la sezione «I miei termini» la mostra in
  // sola lettura e altrimenti resterebbe indietro fino a un ricaricamento
  const [globalTerms, setGlobalTerms] = useState(null)
  // il catalogo OpenRouter, una volta per la pagina: lo usano il modello
  // predefinito e (admin) la white list. Se non arriva le sezioni restano
  // leggibili, con i selettori vuoti
  const [catalog, setCatalog] = useState({ models: [], allowed: null })
  // la sezione white list ricarica il modello «bloccato» quando cambia la
  // regola del modello predefinito
  const [accessRefresh, setAccessRefresh] = useState(0)
  const { t } = useTranslation('settings')
  usePageTitle(t('title'))

  useEffect(() => {
    api.orModels()
      .then((r) => setCatalog({ models: r.models, allowed: r.allowed ?? null }))
      .catch(() => {})
  }, [])

  const toDraft = (list) =>
    Object.fromEntries(list.map((s) => [s.key, String(s.value)]))

  useEffect(() => {
    if (!isAdmin) return              // /api/settings è riservata all'admin
    api.getSettings()
      .then((list) => { setSettings(list); setDraft(toDraft(list)) })
      .catch((e) => setError(e.message))
  }, [isAdmin])

  // le preferenze personali non aspettano i parametri di esercizio
  if (!isAdmin) {
    return (
      <main className="min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full page-content max-w-2xl flex-col gap-5">
          <header>
            <h1 className="text-xl font-semibold tracking-tight">{t('title')}</h1>
            <p className="mt-1 text-sm text-muted-foreground">{t('introUser')}</p>
          </header>
          <div className="rounded-xl border border-border bg-card p-4 sm:p-6 shadow-sm">
            <LanguagePreference />
          </div>
          <div className="rounded-xl border border-border bg-card p-4 sm:p-6 shadow-sm">
            <ChangePassword />
          </div>
          <div className="rounded-xl border border-border bg-card p-4 sm:p-6 shadow-sm">
            <DefaultModel isAdmin={false} catalog={catalog} />
          </div>
          <div className="rounded-xl border border-border bg-card p-4 sm:p-6 shadow-sm">
            <MyAnonTerms />
          </div>
          <div className="rounded-xl border border-border bg-card p-4 sm:p-6 shadow-sm">
            <TutorialPreference />
          </div>
        </div>
      </main>
    )
  }

  if (!settings) {
    return (
      <main className="min-w-0 flex-1 overflow-y-auto p-6">
        {error ? <div className="text-sm text-destructive">{error}</div>
               : <div className="text-sm text-muted-foreground">
                   {t('state.loading', { ns: 'common' })}
                 </div>}
      </main>
    )
  }

  const dirty = settings.some((s) => draft[s.key] !== String(s.value))
  const invalid = settings.some((s) => {
    const n = Number(draft[s.key])
    return draft[s.key] === '' || !Number.isInteger(n) || n < s.min
           || (s.max !== undefined && n > s.max)
  })

  async function save(e) {
    e.preventDefault()
    setSaving(true)
    try {
      const values = Object.fromEntries(
        settings.map((s) => [s.key, Number(draft[s.key])]))
      const updated = await api.saveSettings(values)
      setSettings(updated)
      setDraft(toDraft(updated))
      toast.success(t('savedSettings'))
    } catch (err) {
      toast.error(err.message)
    } finally {
      setSaving(false)
    }
  }

  const sections = [...new Set(settings.map((s) => s.section))]

  return (
    <main className="min-w-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full page-content max-w-2xl flex-col gap-5">
        <header>
          <h1 className="text-xl font-semibold tracking-tight">{t('title')}</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            <Trans i18nKey="introAdmin" ns="settings" components={{ code: <code /> }} />
          </p>
        </header>

        <form onSubmit={save}
              className="flex flex-col gap-6 rounded-xl border border-border bg-card p-4 sm:p-6 shadow-sm">
          {sections.map((sec) => (
            <section key={sec} className="flex flex-col gap-4">
              {/* il backend manda la CHIAVE del gruppo; se il catalogo non la
                  conosce (parametro nuovo) esce la chiave, non un vuoto */}
              <SectionTitle>{t(`section.${sec}`, { defaultValue: sec })}</SectionTitle>
              {settings.filter((s) => s.section === sec).map((s) => (
                <div key={s.key} className="flex flex-col gap-1.5">
                  {/* kind "bool" (min 0, max 1): il valore resta un intero,
                      cambia solo il controllo — una casella si legge meglio
                      di un campo numerico che accetta 0 o 1 */}
                  {s.kind === 'bool' ? (
                    <Label htmlFor={`setting-${s.key}`}
                           className="flex cursor-pointer items-center gap-2">
                      <input id={`setting-${s.key}`} type="checkbox"
                             checked={draft[s.key] === '1'}
                             onChange={(e) => setDraft({
                               ...draft, [s.key]: e.target.checked ? '1' : '0' })} />
                      {t(`registry.${s.key}.label`, { defaultValue: s.label })}
                    </Label>
                  ) : (
                    <>
                      <Label htmlFor={`setting-${s.key}`}>
                        {t(`registry.${s.key}.label`, { defaultValue: s.label })}
                      </Label>
                      <Input id={`setting-${s.key}`} type="number" min={s.min} step="1"
                             max={s.max} value={draft[s.key] ?? ''} className="w-44"
                             onChange={(e) => setDraft({ ...draft, [s.key]: e.target.value })} />
                    </>
                  )}
                  <p className="text-xs text-muted-foreground">
                    {/* `label` e `help` restano nel REGISTRY come ripiego:
                        un parametro nuovo si vede in italiano invece di
                        sparire (vedi settings_store.py) */}
                    {t(`registry.${s.key}.help`, { defaultValue: s.help })}
                    {s.kind === 'bool' ? ''
                      : t('defaultSuffix', { value: s.default })}
                  </p>
                </div>
              ))}
            </section>
          ))}
          <div className="flex items-center gap-2">
            <Button disabled={!dirty || invalid || saving}>
              {saving && <Loader2 className="animate-spin" />}
              {saving ? t('saving') : t('actions.save', { ns: 'common' })}
            </Button>
            {dirty && (
              <Button type="button" variant="outline"
                      onClick={() => setDraft(toDraft(settings))}>
                {t('revert')}
              </Button>
            )}
          </div>
        </form>

        <div className="rounded-xl border border-border bg-card p-4 sm:p-6 shadow-sm">
          <LanguagePreference />
        </div>

        <div className="rounded-xl border border-border bg-card p-4 sm:p-6 shadow-sm">
          <ChangePassword />
        </div>

        <div className="rounded-xl border border-border bg-card p-4 sm:p-6 shadow-sm">
          {/* la deroga ZDR come SALVATA (non la bozza della casella qui sopra):
              il selettore deve nascondere quello che il server rifiuterebbe
              oggi, e finché non si salva rifiuta ancora */}
          <DefaultModel isAdmin catalog={catalog}
                        allowNonZdr={Number(settings.find(
                          (s) => s.key === 'chat_allow_non_zdr')?.value) === 1}
                        onSaved={() => setAccessRefresh((n) => n + 1)} />
        </div>

        <div className="rounded-xl border border-border bg-card p-4 sm:p-6 shadow-sm">
          <ModelAccess models={catalog.models} refreshKey={accessRefresh}
                       allowNonZdr={Number(settings.find(
                         (s) => s.key === 'chat_allow_non_zdr')?.value) === 1} />
        </div>

        <div className="rounded-xl border border-border bg-card p-4 sm:p-6 shadow-sm">
          <AnonDefaults onSaved={setGlobalTerms} />
        </div>

        <div className="rounded-xl border border-border bg-card p-4 sm:p-6 shadow-sm">
          <MyAnonTerms globals={globalTerms} />
        </div>

        <div className="rounded-xl border border-border bg-card p-4 sm:p-6 shadow-sm">
          <TutorialPreference />
        </div>
      </div>
    </main>
  )
}
