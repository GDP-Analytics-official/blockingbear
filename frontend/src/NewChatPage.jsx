import React, { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { Lock, LockOpen, Loader2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from './api.js'
import { useChats } from './chats.jsx'
import { usePageTitle } from '@/lib/usePageTitle.js'
import { cn } from '@/lib/utils'
import { Tooltip } from '@/components/ui/tooltip'

// Prima pagina dopo il login: la scelta del modo (anonimizzata o normale), a
// centro pagina, come unico contenuto. La scelta si fa una volta sola, prima
// di scrivere — dopo il primo invio i turni già partiti resterebbero nel
// contesto nella forma in cui sono partiti.
export default function NewChatPage() {
  const navigate = useNavigate()
  const { refresh } = useChats()
  const { t } = useTranslation('chat')
  const [status, setStatus] = useState(null)
  const [creating, setCreating] = useState(null)   // true/false = card cliccata
  usePageTitle(t('new'))

  useEffect(() => {
    api.orStatus().then(setStatus).catch(() => {})
  }, [])

  // con la policy obbligatoria la chat normale non esiste: la card resta
  // visibile ma disabilitata, con la spiegazione del perché
  const anonRequired = status?.chat_anonymization_policy === 'required'

  async function pick(anonymized) {
    if (creating != null) return
    setCreating(anonymized)
    try {
      const c = await api.createChat('', anonymized, null)
      refresh()
      navigate(`/chats/${c.id}`)
    } catch (e) {
      toast.error(e.message)
      setCreating(null)
    }
  }

  const options = [
    { key: true, icon: Lock, title: t('newDialog.anon.title'),
      lines: [t('newDialog.anon.l1'), t('newDialog.anon.l2'), t('newDialog.anon.l3')] },
    { key: false, icon: LockOpen, title: t('newDialog.plain.title'),
      lines: [t('newDialog.plain.l1'), t('newDialog.plain.l2'), t('newDialog.plain.l3')],
      off: anonRequired },
  ]

  return (
    <div className="flex min-h-0 w-full flex-1 flex-col overflow-y-auto p-4 sm:p-6">
      <div className="m-auto w-full max-w-xl shrink-0">
        <div className="mb-5 flex flex-col gap-1 text-center">
          <h1 className="text-lg font-semibold">{t('newDialog.title')}</h1>
          <p className="text-sm text-muted-foreground">{t('newDialog.description')}</p>
        </div>
        <div className={cn('grid gap-3', options.length > 1 && 'sm:grid-cols-2')}>
          {options.map((o) => (
            <Tooltip key={String(o.key)} text={o.off ? t('newDialog.plain.required') : ''}
                     className="flex">
            <button type="button" onClick={() => pick(o.key)}
                    data-tour={o.key ? 'new-anon' : 'new-plain'}
                    disabled={creating != null || o.off}
                    className={cn(
                      'flex w-full flex-col gap-2 rounded-lg border-2 border-border bg-card p-4 text-left transition-colors disabled:opacity-60',
                      o.off ? 'cursor-not-allowed disabled:opacity-50'
                        : o.key ? 'hover:border-emerald-500/60 hover:bg-emerald-500/5'
                                : 'hover:border-amber-500/60 hover:bg-amber-500/5')}>
              <span className={cn('inline-flex items-center gap-2 font-medium',
                                  o.key ? 'text-emerald-700 dark:text-emerald-400'
                                        : 'text-amber-700 dark:text-amber-400')}>
                {creating === o.key
                  ? <Loader2 className="size-4 animate-spin" />
                  : <o.icon className="size-4" />}
                {o.title}
              </span>
              <ul className="flex flex-col gap-1 text-xs text-muted-foreground">
                {o.lines.map((l) => <li key={l}>{l}</li>)}
              </ul>
            </button>
            </Tooltip>
          ))}
        </div>
      </div>
    </div>
  )
}
