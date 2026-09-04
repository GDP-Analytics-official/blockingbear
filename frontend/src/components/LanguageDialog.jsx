import React, { useState } from 'react'
import { Loader2 } from 'lucide-react'
import { Dialog, DialogContent } from '@/components/ui/dialog'
import { LANGUAGES } from '@/i18n/languages.jsx'
import { useLanguage } from '@/i18n/LanguageProvider.jsx'
import { Logo } from '@/components/Logo.jsx'

// Popup del primo accesso. Compare SUBITO DOPO il login, quando il profilo
// entra senza una lingua: prima di allora non si sa a chi si sta parlando, e
// una risposta data sulla schermata di accesso non avrebbe un profilo su cui
// finire.
//
// I testi sono BILINGUI e scritti a mano, non tradotti: è l'unico riquadro
// dell'applicazione che si mostra a qualcuno di cui non si conosce la lingua,
// e scriverlo con t() significherebbe porre la domanda già in una delle due
// risposte possibili.
//
// Non si chiude senza scegliere: la risposta va sul profilo e questa è la sola
// occasione in cui viene chiesta (le bandierine accanto al logout permettono
// poi di cambiare idea in qualsiasi momento). Se il salvataggio non riesce il
// popup resta aperto: chiuderlo lascerebbe il profilo senza lingua e la
// domanda tornerebbe al prossimo accesso.
export default function LanguageDialog() {
  const { asking, choose } = useLanguage()
  const [saving, setSaving] = useState('')     // codice in corso di salvataggio
  const [failed, setFailed] = useState(false)

  async function pick(code) {
    setSaving(code)
    setFailed(false)
    const ok = await choose(code)
    setSaving('')
    if (!ok) setFailed(true)
  }

  return (
    <Dialog open={asking}>
      <DialogContent hideClose className="sm:max-w-md"
                     onInteractOutside={(e) => e.preventDefault()}
                     onEscapeKeyDown={(e) => e.preventDefault()}
                     aria-label="Scegli la lingua / Choose your language">
        <div className="flex flex-col items-center gap-1 pt-1">
          <Logo />
          <p className="mt-3 text-center text-base font-semibold leading-tight">
            Scegli la lingua
            <span className="block text-muted-foreground">Choose your language</span>
          </p>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          {LANGUAGES.map(({ code, label, Flag }) => (
            <button key={code} type="button" disabled={Boolean(saving)}
                    onClick={() => pick(code)}
                    className="flex cursor-pointer flex-col items-center gap-2.5 rounded-lg border-2 border-border p-4 transition-colors hover:border-primary/60 hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:cursor-default disabled:opacity-60">
              <Flag className="w-14" />
              <span className="inline-flex items-center gap-2 font-medium">
                {saving === code && <Loader2 className="size-4 animate-spin" />}
                {label}
              </span>
            </button>
          ))}
        </div>

        {failed ? (
          <p className="text-center text-xs text-destructive">
            Non è stato possibile salvare la scelta: riprova.
            <span className="block">Could not save your choice: please try again.</span>
          </p>
        ) : (
          <p className="text-center text-xs text-muted-foreground">
            Si può cambiare in qualsiasi momento dal menu.
            <span className="block">You can change it later from the menu.</span>
          </p>
        )}
      </DialogContent>
    </Dialog>
  )
}
