import React from 'react'
import { cn } from '@/lib/utils'

// Marchio dell'app: orso (public/logo.png) + wordmark opzionale.
export function LogoMark({ className }) {
  return (
    <img src="/logo.png" alt="BlockingBear" className={cn('size-8 shrink-0 object-contain', className)} draggable={false} />
  )
}

export function Logo({ subtitle, className }) {
  return (
    <div className={cn('flex items-center gap-2.5', className)}>
      <LogoMark />
      <div className="min-w-0 leading-tight">
        <div className="text-[15px] font-semibold tracking-tight">BlockingBear</div>
        {subtitle && <div className="truncate text-xs text-muted-foreground">{subtitle}</div>}
      </div>
    </div>
  )
}
