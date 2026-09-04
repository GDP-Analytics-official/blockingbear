import React, { useState } from 'react'
import { Eye, EyeOff, Loader2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from '@/api.js'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

// Campo password con l'occhio per mostrarla (stesso pattern del login)
function PwField({ id, label, value, onChange, autoComplete, autoFocus }) {
  const [show, setShow] = useState(false)
  const { t } = useTranslation('settings')
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <div className="relative">
        <Input id={id} type={show ? 'text' : 'password'} value={value}
               autoComplete={autoComplete} autoFocus={autoFocus} className="pr-10"
               onChange={(e) => onChange(e.target.value)} />
        <button type="button" tabIndex={-1}
                className="absolute inset-y-0 right-0 flex w-10 items-center justify-center text-muted-foreground hover:text-foreground"
                title={t(show ? 'password.hide' : 'password.show')}
                aria-label={t(show ? 'password.hide' : 'password.show')}
                onClick={() => setShow((v) => !v)}>
          {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
        </button>
      </div>
    </div>
  )
}

// Il cambio password vero e proprio: lo stesso form nella pagina Impostazioni
// e nella schermata del cambio obbligatorio (ForcePassword). La vecchia
// password serve sempre — anche al primo accesso l'utente la conosce, gliel'ha
// data l'admin. Gli errori del server (vecchia password sbagliata, nuova
// uguale alla vecchia) arrivano già tradotti da api.js.
export default function PasswordChangeForm({ onDone, autoFocus = false }) {
  const [oldPw, setOldPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const { t } = useTranslation('settings')

  const mismatch = confirm !== '' && confirm !== newPw
  const ready = oldPw !== '' && newPw.length >= 8 && confirm === newPw

  async function submit(e) {
    e.preventDefault()
    if (!ready || busy) return
    setBusy(true)
    setError('')
    try {
      await api.changePassword(oldPw, newPw)
      setOldPw(''); setNewPw(''); setConfirm('')
      onDone?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4">
      <PwField id="pw-old" label={t('password.current')} value={oldPw}
               onChange={setOldPw} autoComplete="current-password"
               autoFocus={autoFocus} />
      <PwField id="pw-new" label={t('password.new')} value={newPw}
               onChange={setNewPw} autoComplete="new-password" />
      <div className="flex flex-col gap-1.5">
        <PwField id="pw-confirm" label={t('password.confirm')} value={confirm}
                 onChange={setConfirm} autoComplete="new-password" />
        {mismatch && (
          <p className="text-xs text-destructive">{t('password.mismatch')}</p>
        )}
      </div>

      {error && <div className="text-sm text-destructive">{error}</div>}

      <div>
        <Button type="submit" disabled={!ready || busy}>
          {busy && <Loader2 className="animate-spin" />}
          {t('password.submit')}
        </Button>
      </div>
    </form>
  )
}
