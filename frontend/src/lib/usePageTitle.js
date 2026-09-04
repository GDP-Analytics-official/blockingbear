import { useEffect } from 'react'

// Aggiorna il titolo della scheda del browser per la pagina corrente.
export function usePageTitle(title) {
  useEffect(() => {
    document.title = title ? `${title} — BlockingBear` : 'BlockingBear'
    return () => { document.title = 'BlockingBear' }
  }, [title])
}
