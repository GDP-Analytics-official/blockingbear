import { useEffect, useState } from 'react'

// One observer per scroll surface, with lightweight page-sized placeholders.
// Keeping their geometry preserves scroll anchors while images are unloaded.
const roots = new WeakMap()

export function usePreviewVisibility(pageRef, enabled) {
  const [nearby, setNearby] = useState(false)
  useEffect(() => {
    if (!enabled) {
      setNearby(false)
      return undefined
    }
    const page = pageRef.current
    const root = page?.closest('.pages')
    if (!root) return undefined
    let group = roots.get(root)
    if (!group) {
      const callbacks = new Map()
      const observer = new IntersectionObserver((entries) => {
        for (const entry of entries) callbacks.get(entry.target)?.(entry.isIntersecting)
      }, { root, rootMargin: '600px 0px' })
      group = { callbacks, observer }
      roots.set(root, group)
    }
    group.callbacks.set(page, setNearby)
    group.observer.observe(page)
    return () => {
      group.observer.unobserve(page)
      group.callbacks.delete(page)
      if (!group.callbacks.size) {
        group.observer.disconnect()
        roots.delete(root)
      }
    }
  }, [pageRef, enabled])
  return nearby
}
