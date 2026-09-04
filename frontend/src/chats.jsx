import React, {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
} from 'react'
import { api } from './api.js'

// L'elenco delle chat LIBERE vive nella sidebar principale (come Claude e
// ChatGPT), quindi lo stato sta sopra le pagine: lo montano DashboardLayout
// (che lo mostra) e ChatPage/NewChatPage (che lo aggiornano quando una chat
// nasce, cambia titolo dopo il primo turno o viene eliminata).
// Le chat dei progetti NON passano di qui: restano nell'aside della loro pagina.
const ChatsContext = createContext(null)

export function ChatsProvider({ children }) {
  const [chats, setChats] = useState([])

  const refresh = useCallback(() => {
    api.listChats().then(setChats).catch(() => {})
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const value = useMemo(() => ({ chats, setChats, refresh }),
                        [chats, refresh])
  return <ChatsContext.Provider value={value}>{children}</ChatsContext.Provider>
}

export function useChats() {
  return useContext(ChatsContext)
}
