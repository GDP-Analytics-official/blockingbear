import React from 'react'
import { Slot } from '@radix-ui/react-slot'
import { cva } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:ring-offset-1 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 cursor-pointer",
  {
    variants: {
      variant: {
        default: 'bg-primary text-primary-foreground shadow-sm hover:bg-primary-hover',
        destructive: 'bg-destructive text-destructive-foreground shadow-sm hover:bg-destructive/90',
        outline: 'border border-input bg-card shadow-sm hover:bg-accent hover:text-accent-foreground',
        ghost: 'hover:bg-accent hover:text-accent-foreground',
        link: 'text-primary underline-offset-4 hover:underline',
      },
      size: {
        default: 'h-9 px-4 py-2 [&_svg]:size-4',
        sm: 'h-8 rounded-md px-3 text-xs [&_svg]:size-3.5',
        lg: 'h-10 rounded-md px-6 [&_svg]:size-4',
        icon: 'size-9 [&_svg]:size-4',
        'icon-sm': 'size-7 rounded-md [&_svg]:size-3.5',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  }
)

const Button = React.forwardRef(({ className, variant, size, asChild = false, ...props }, ref) => {
  const Comp = asChild ? Slot : 'button'
  return (
    <Comp ref={ref} className={cn(buttonVariants({ variant, size, className }))} {...props} />
  )
})
Button.displayName = 'Button'

export { Button, buttonVariants }
