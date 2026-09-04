import React from 'react'
import { cn } from '@/lib/utils'

// Il pannello a tendina di un pulsante (selettore modelli, opzioni modello).
//
// Normalmente è `absolute` sotto il pulsante. Dentro un dialog che scorre (il
// wizard di configurazione) una tendina assoluta resta tagliata dal bordo del
// dialog, e portarla fuori col portal si scontra col focus trap di Radix: con
// `inline` il pannello si apre invece NEL flusso della pagina, a tutta
// larghezza, e il dialog scorre da solo per mostrarlo.
export default function FloatingPanel({ inline = false, className, children, panelRef }) {
  return (
    <div ref={panelRef}
         className={cn(inline ? 'mt-1 w-full' : 'absolute z-20 mt-1', className)}>
      {children}
    </div>
  )
}
