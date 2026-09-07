import React, { useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { Menu } from 'lucide-react'
import { useMediaQuery } from '@/lib/useMediaQuery.js'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

// One navigation tree, presented as a focus-trapped drawer on smaller screens.
export default function ResponsiveSidebar({ title, children, className,
                                            breakpoint = 1024, brand = null }) {
  const wide = useMediaQuery(`(min-width: ${breakpoint}px)`)
  const [open, setOpen] = useState(false)
  const location = useLocation()
  useEffect(() => { setOpen(false) }, [location.key, wide])

  if (wide) return <nav aria-label={title} className={className}>{children}</nav>

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <div className="mobile-nav-bar flex shrink-0 items-center gap-3 border-b border-border bg-card px-3 py-1.5">
        <DialogTrigger asChild>
          <Button variant="ghost" size={brand ? 'icon' : 'sm'} aria-label={title}>
            <Menu />{!brand && <span className="truncate">{title}</span>}
          </Button>
        </DialogTrigger>
        {brand}
      </div>
      <DialogContent className={cn('mobile-drawer left-0 top-0 translate-x-0 translate-y-0', className)} aria-describedby={undefined}>
        <DialogTitle className="sr-only">{title}</DialogTitle>
        {children}
      </DialogContent>
    </Dialog>
  )
}
