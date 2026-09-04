import React from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { cn } from '@/lib/utils'

const Dialog = DialogPrimitive.Root
const DialogTrigger = DialogPrimitive.Trigger
const DialogPortal = DialogPrimitive.Portal
const DialogClose = DialogPrimitive.Close

const DialogOverlay = React.forwardRef(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      'fixed inset-0 z-50 bg-black/45 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=closed]:fade-out-0',
      className
    )}
    {...props}
  />
))
DialogOverlay.displayName = 'DialogOverlay'

// `hideClose`: niente X in alto a destra. Serve ai dialog che non hanno una
// uscita neutra — chiuderli È una decisione, e va presa da un bottone che dice
// quale (es. il turno fermo in attesa delle fusioni: si continua o si annulla).
const DialogContent = React.forwardRef(({ className, children,
                                          hideClose = false, ...props }, ref) => {
  const { t } = useTranslation()
  return (
  <DialogPortal>
    <DialogOverlay />
    <DialogPrimitive.Content
      ref={ref}
      className={cn(
        'fixed left-1/2 top-1/2 z-50 grid w-full max-w-lg -translate-x-1/2 -translate-y-1/2 gap-4',
        'max-h-[86vh] overflow-y-auto rounded-xl border border-border bg-card p-6 shadow-2xl',
        className
      )}
      {...props}
    >
      {children}
      {!hideClose && (
        <DialogPrimitive.Close className="absolute right-4 top-4 rounded-md p-1 text-muted-foreground opacity-70 transition-opacity hover:opacity-100 hover:bg-accent focus:outline-none focus:ring-2 focus:ring-ring/30 disabled:pointer-events-none">
          <X className="size-4" />
          <span className="sr-only">{t('actions.close')}</span>
        </DialogPrimitive.Close>
      )}
    </DialogPrimitive.Content>
  </DialogPortal>
  )
})
DialogContent.displayName = 'DialogContent'

function DialogHeader({ className, ...props }) {
  return <div className={cn('flex flex-col gap-1.5 text-left', className)} {...props} />
}

function DialogFooter({ className, ...props }) {
  return <div className={cn('flex flex-row justify-end gap-2', className)} {...props} />
}

const DialogTitle = React.forwardRef(({ className, ...props }, ref) => (
  <DialogPrimitive.Title
    ref={ref}
    className={cn('text-base font-semibold leading-none tracking-tight', className)}
    {...props}
  />
))
DialogTitle.displayName = 'DialogTitle'

const DialogDescription = React.forwardRef(({ className, ...props }, ref) => (
  <DialogPrimitive.Description
    ref={ref}
    className={cn('text-sm text-muted-foreground', className)}
    {...props}
  />
))
DialogDescription.displayName = 'DialogDescription'

export {
  Dialog, DialogPortal, DialogOverlay, DialogTrigger, DialogClose,
  DialogContent, DialogHeader, DialogFooter, DialogTitle, DialogDescription,
}
