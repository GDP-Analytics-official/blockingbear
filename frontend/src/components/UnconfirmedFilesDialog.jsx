import React, { useEffect, useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import { Trans, useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

// Avviso prima di aprire una NUOVA CHAT del progetto quando restano file da
// confermare: si può procedere, ma il modello quei file non li vedrà (solo
// i confermati finiscono nel system prompt e nella sandbox).
//
// «Non mostrare più» è una preferenza DI QUESTO PROGETTO (patchProject:
// skip_unconfirmed_warning), non del browser e non globale: i file da
// confermare sono suoi, e chi apre il progetto da un'altra postazione trova
// la stessa scelta.
export default function UnconfirmedFilesDialog({
  open, onOpenChange, files = [], onProceed, onSkipForever,
}) {
  const [dontAsk, setDontAsk] = useState(false)
  const { t } = useTranslation('anon')
  useEffect(() => { if (open) setDontAsk(false) }, [open])

  const n = files.length
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="size-4 text-amber-600" />
            {/* singolare e plurale sono due frasi intere, non un pezzo
                cucito a runtime: in italiano cambiano anche i pronomi
                (lo/li, confermarlo/confermarli) e in inglese l'accordo del
                verbo — i suffissi _one/_other di i18next li tengono separati */}
            {t('unconfirmed.title', { count: n })}
          </DialogTitle>
          <DialogDescription className="space-y-2 break-words">
            <span className="block">
              <Trans i18nKey="unconfirmed.body" ns="anon" count={n}
                     components={{ b: <b /> }} />
            </span>
          </DialogDescription>
        </DialogHeader>

        <ul className="max-h-40 overflow-y-auto rounded-lg border border-border bg-accent/40 px-3 py-2 text-sm">
          {files.map((f) => (
            <li key={f.id} className="truncate py-0.5" title={f.filename}>
              {f.filename}
            </li>
          ))}
        </ul>

        <label className="flex cursor-pointer select-none items-center gap-2 text-sm text-muted-foreground">
          <Checkbox checked={dontAsk} onCheckedChange={(v) => setDontAsk(!!v)} />
          {t('unconfirmed.dontAsk')}
        </label>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t('actions.cancel', { ns: 'common' })}
          </Button>
          <Button onClick={() => { if (dontAsk) onSkipForever(); onProceed() }}>
            {t('unconfirmed.proceed')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
