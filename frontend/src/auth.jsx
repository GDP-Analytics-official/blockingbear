import React, { createContext, useContext, useEffect, useState } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, getToken, setToken } from './api.js'
import { useLanguage } from './i18n/LanguageProvider.jsx'

// Sessione utente condivisa da tutte le route. Il logout forzato (401 dal
// backend, evento blockingbear-logout emesso da api.js) azzera l'utente: le
// PrivateRoute rimandano al login da sole.
const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [checking, setChecking] = useState(Boolean(getToken()))
  const { applyUser } = useLanguage()

  useEffect(() => {
    const onLogout = () => setUser(null)
    window.addEventListener('blockingbear-logout', onLogout)
    if (getToken()) {
      api.me().then(setUser).catch(() => setToken(null)).finally(() => setChecking(false))
    }
    return () => window.removeEventListener('blockingbear-logout', onLogout)
  }, [])

  // Appena si sa CHI sta usando l'app, l'interfaccia passa alla lingua del suo
  // profilo (o, se il profilo non ne ha ancora una, gliela si chiede). Vale
  // dopo /me e dopo il login, che passano entrambi da setUser, e al logout,
  // dove `user` torna null e si riparte dalla schermata di accesso.
  useEffect(() => {
    applyUser(user)
  }, [user, applyUser])

  const logout = () => {
    setToken(null)
    setUser(null)
  }

  return (
    <AuthContext.Provider value={{ user, checking, setUser, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}

// Wrapper delle route autenticate: mentre il token viene verificato mostra
// il caricamento, senza utente rimanda al login (ricordando da dove veniva).
export function PrivateRoute({ children }) {
  const { user, checking } = useAuth()
  const location = useLocation()
  const { t } = useTranslation()
  if (checking) return <div className="grid h-full place-items-center text-muted-foreground">{t('state.loading')}</div>
  if (!user) return <Navigate to="/login" replace state={{ from: location }} />
  return children
}

// Route riservate agli admin (dentro una PrivateRoute: utente già presente).
export function AdminRoute({ children }) {
  const { user } = useAuth()
  if (user?.role !== 'admin') return <Navigate to="/new" replace />
  return children
}
