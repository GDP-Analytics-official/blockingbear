import React from 'react'
import { cn } from '@/lib/utils'

const Table = React.forwardRef(({ className, ...props }, ref) => (
  <div className="relative w-full overflow-x-auto">
    <table ref={ref} className={cn('w-full caption-bottom text-sm', className)} {...props} />
  </div>
))
Table.displayName = 'Table'

const TableHeader = React.forwardRef(({ className, ...props }, ref) => (
  <thead ref={ref} className={cn('[&_tr]:border-b', className)} {...props} />
))
TableHeader.displayName = 'TableHeader'

const TableBody = React.forwardRef(({ className, ...props }, ref) => (
  <tbody ref={ref} className={cn('[&_tr:last-child]:border-0', className)} {...props} />
))
TableBody.displayName = 'TableBody'

const TableRow = React.forwardRef(({ className, ...props }, ref) => (
  <tr
    ref={ref}
    className={cn('border-b border-border transition-colors hover:bg-accent/60 data-[state=selected]:bg-muted', className)}
    {...props}
  />
))
TableRow.displayName = 'TableRow'

const TableHead = React.forwardRef(({ className, ...props }, ref) => (
  <th
    ref={ref}
    className={cn('h-10 px-3 text-left align-middle text-xs font-medium uppercase tracking-wide text-muted-foreground', className)}
    {...props}
  />
))
TableHead.displayName = 'TableHead'

const TableCell = React.forwardRef(({ className, ...props }, ref) => (
  <td ref={ref} className={cn('px-3 py-2.5 align-middle', className)} {...props} />
))
TableCell.displayName = 'TableCell'

export { Table, TableHeader, TableBody, TableRow, TableHead, TableCell }
