import React from 'react'
import * as CheckboxPrimitive from '@radix-ui/react-checkbox'
import { Check, Minus } from 'lucide-react'
import { cn } from '@/lib/utils'

// checked accetta anche 'indeterminate' (casella padre di un gruppo con solo
// alcuni figli accesi): Radix la marca data-state=indeterminate, qui si veste
// come una spunta piena ma col trattino al posto del segno.
const Checkbox = React.forwardRef(({ className, ...props }, ref) => (
  <CheckboxPrimitive.Root
    ref={ref}
    className={cn(
      'peer size-4 shrink-0 rounded-[4px] border border-input bg-card shadow-sm transition-colors',
      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/30',
      'disabled:cursor-not-allowed disabled:opacity-50',
      'data-[state=checked]:bg-primary data-[state=checked]:border-primary data-[state=checked]:text-primary-foreground',
      'data-[state=indeterminate]:bg-primary data-[state=indeterminate]:border-primary data-[state=indeterminate]:text-primary-foreground',
      className
    )}
    {...props}
  >
    <CheckboxPrimitive.Indicator className="flex items-center justify-center text-current">
      {props.checked === 'indeterminate'
        ? <Minus className="size-3" strokeWidth={3} />
        : <Check className="size-3" strokeWidth={3} />}
    </CheckboxPrimitive.Indicator>
  </CheckboxPrimitive.Root>
))
Checkbox.displayName = 'Checkbox'

export { Checkbox }
