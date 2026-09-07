import React, { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@/lib/utils'

// Portal dropdowns escape the chat's clipping containers. Dialogs keep their
// inline panels inside Radix's focus scope and scroll with the dialog.
export default function FloatingPanel({ inline = false, className, children, panelRef,
                                        anchorRef, align = 'start', onClose, ...props }) {
  const localRef = useRef(null)
  const ref = panelRef || localRef
  const [position, setPosition] = useState(null)
  const positioned = position !== null

  useLayoutEffect(() => {
    if (inline || !positioned) return undefined
    const panel = ref.current
    const trigger = anchorRef?.current?.querySelector('button')
    if (panel && !panel.contains(document.activeElement)) {
      (panel.querySelector('[data-panel-autofocus]') || panel).focus({ preventScroll: true })
    }
    return () => {
      if (panel?.contains(document.activeElement)) trigger?.focus({ preventScroll: true })
    }
  }, [inline, positioned, ref, anchorRef])

  useLayoutEffect(() => {
    if (inline) return undefined
    const panel = ref.current
    const anchor = anchorRef?.current
    if (!panel || !anchor) return undefined
    const viewport = window.visualViewport
    let frame
    const place = () => {
      const margin = 12
      const width = viewport?.width ?? window.innerWidth
      const height = viewport?.height ?? window.innerHeight
      const x = viewport?.offsetLeft ?? 0
      const y = viewport?.offsetTop ?? 0
      const rect = anchor.getBoundingClientRect()
      const small = window.matchMedia('(max-width: 639px)').matches
      const below = Math.max(0, y + height - margin - rect.bottom - 4)
      const above = Math.max(0, rect.top - y - margin - 4)
      const upwards = !small && below < Math.min(panel.scrollHeight, 240) && above > below
      const maxHeight = small ? Math.min(height * 0.7, 600)
        : Math.min(height - margin * 2, upwards ? above : below)
      const panelHeight = Math.min(panel.getBoundingClientRect().height, maxHeight)
      const left = small ? x + margin : Math.max(x + margin, Math.min(
        align === 'end' ? rect.right - panel.offsetWidth : rect.left,
        x + width - panel.offsetWidth - margin))
      const top = small
        ? `calc(${y + height - panelHeight}px - max(12px, env(safe-area-inset-bottom)))`
        : Math.max(y + margin, Math.min(y + height - margin - panelHeight,
          upwards ? rect.top - 4 - panelHeight : rect.bottom + 4))
      const next = { left, top, maxHeight: Math.max(0, maxHeight),
        maxWidth: Math.max(0, width - margin * 2), ...(small ? { width: width - margin * 2 } : {}) }
      setPosition(previous => JSON.stringify(previous) === JSON.stringify(next) ? previous : next)
    }
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(place) }
    place()
    const observer = new ResizeObserver(schedule)
    observer.observe(panel)
    observer.observe(anchor)
    window.addEventListener('resize', schedule)
    window.addEventListener('scroll', schedule, true)
    viewport?.addEventListener('resize', schedule)
    viewport?.addEventListener('scroll', schedule)
    return () => {
      cancelAnimationFrame(frame)
      observer.disconnect()
      window.removeEventListener('resize', schedule)
      window.removeEventListener('scroll', schedule, true)
      viewport?.removeEventListener('resize', schedule)
      viewport?.removeEventListener('scroll', schedule)
    }
  }, [inline, anchorRef, align, ref])

  useEffect(() => {
    const outside = (event) => {
      if (!anchorRef?.current?.contains(event.target) && !ref.current?.contains(event.target)) onClose?.()
    }
    const escape = (event) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      event.stopPropagation()
      onClose?.()
      anchorRef?.current?.querySelector('button')?.focus({ preventScroll: true })
    }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('pointerdown', outside)
      document.removeEventListener('keydown', escape)
    }
  }, [anchorRef, ref, onClose])

  const panel = (
    <div ref={ref} role="region" tabIndex={-1} {...props}
         style={inline ? undefined : { ...position, visibility: position ? undefined : 'hidden' }}
         className={cn('floating-panel', inline ? 'floating-inline mt-1 w-full' : 'floating-portal', className)}>
      {children}
    </div>
  )
  return inline ? panel : createPortal(panel, document.body)
}
