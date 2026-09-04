import React from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { Toaster } from 'sonner'
import { AuthProvider, PrivateRoute, AdminRoute } from './auth.jsx'
import { LanguageProvider } from './i18n/LanguageProvider.jsx'
import LanguageDialog from './components/LanguageDialog.jsx'
import DashboardLayout from './DashboardLayout.jsx'
import { ForcePasswordGate } from './ForcePassword.jsx'
import Login from './Login.jsx'
import ProjectsPage from './ProjectsPage.jsx'
import ProjectPage from './ProjectPage.jsx'
import ProjectFilePage from './ProjectFilePage.jsx'
import ChatPage from './ChatPage.jsx'
import NewChatPage from './NewChatPage.jsx'
import UsersPage from './UsersPage.jsx'
import CostsAnalyticsPage from './CostsAnalyticsPage.jsx'
import CostsKeysPage from './CostsKeysPage.jsx'
import SettingsPage from './SettingsPage.jsx'

export default function App() {
  return (
    // LanguageProvider sta FUORI da AuthProvider perché è quest'ultimo, appena
    // sa chi è l'utente, a passargli il profilo da cui prendere la lingua.
    <LanguageProvider>
      <AuthProvider>
        <Toaster position="top-right" richColors toastOptions={{ style: { fontFamily: 'inherit' } }} />
        {/* fuori dalle Routes: dopo il login la domanda sulla lingua si
            sovrappone a qualunque pagina si sia aperta */}
        <LanguageDialog />
        <Routes>
          <Route path="/login" element={<Login />} />
          {/* tutto il resto è autenticato: PrivateRoute rimanda al login,
              DashboardLayout monta menu + JobsProvider (SSE dei job);
              ForcePasswordGate: col cambio password obbligatorio in sospeso
              l'app non si monta, c'è solo la schermata per cambiarla */}
          <Route element={
            <PrivateRoute>
              <ForcePasswordGate><DashboardLayout /></ForcePasswordGate>
            </PrivateRoute>
          }>
            {/* prima pagina dopo il login: la scelta del modo della nuova
                chat, a centro pagina */}
            <Route path="/new" element={<NewChatPage />} />
            {/* le chat libere si aprono dall'elenco nella sidebar */}
            <Route path="/chats/:chatId" element={<ChatPage />} />
            <Route path="/projects" element={<ProjectsPage />} />
            <Route path="/projects/:id" element={<ProjectPage />} />
            <Route path="/projects/:id/files/:fileId" element={<ProjectFilePage />} />
            <Route path="/projects/:projectId/chat" element={<ChatPage />} />
            {/* pagine di amministrazione (solo admin): si raggiungono dal
                menu dell'utente nel footer della sidebar, a tutta larghezza */}
            <Route path="/admin" element={<Navigate to="/admin/analytics" replace />} />
            <Route path="/admin/analytics" element={<AdminRoute><CostsAnalyticsPage /></AdminRoute>} />
            <Route path="/admin/keys" element={<AdminRoute><CostsKeysPage /></AdminRoute>} />
            <Route path="/admin/users" element={<AdminRoute><UsersPage /></AdminRoute>} />
            {/* vecchi percorsi: link salvati e abitudini portano a quelli nuovi */}
            <Route path="/chat" element={<Navigate to="/new" replace />} />
            <Route path="/users" element={<Navigate to="/admin/users" replace />} />
            <Route path="/costs" element={<Navigate to="/admin/analytics" replace />} />
            <Route path="/costs/analytics" element={<Navigate to="/admin/analytics" replace />} />
            <Route path="/costs/keys" element={<Navigate to="/admin/keys" replace />} />
            {/* non AdminRoute: la pagina mostra all'utente normale le sole
                preferenze personali (i suoi endpoint restano protetti uno a uno) */}
            <Route path="/settings" element={<SettingsPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/new" replace />} />
        </Routes>
      </AuthProvider>
    </LanguageProvider>
  )
}
