import React from 'react'
import { cn } from '@/lib/utils'

// Tooltip solo CSS: compare al passaggio del mouse (e al focus da tastiera)
// sopra a qualunque cosa avvolga. Funziona anche su elementi disabilitati,
// perché lo :hover lo prende il contenitore. `side` decide dove si apre.
export function Tooltip({ text, side = 'top', className, children }) {
  if (!text) return children
  const place = {
    top: 'bottom-full left-1/2 mb-2 -translate-x-1/2',
    bottom: 'top-full left-1/2 mt-2 -translate-x-1/2',
    left: 'right-full top-1/2 mr-2 -translate-y-1/2',
    right: 'left-full top-1/2 ml-2 -translate-y-1/2',
  }[side]
  return (
    <span className={cn('group relative inline-flex', className)}>
      {children}
      <span role="tooltip"
            className={cn('pointer-events-none absolute z-30 hidden w-max max-w-72 rounded-md border border-border bg-popover px-2.5 py-2 text-xs font-normal normal-case text-popover-foreground shadow-md group-hover:block group-focus-within:block',
                          place)}>
        {text}
      </span>
    </span>
  )
}
