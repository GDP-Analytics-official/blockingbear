import React from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App.jsx'
// prima di qualunque componente: init di i18next (lingua di partenza letta
// dal browser), altrimenti il primo render userebbe le chiavi al posto dei testi
import './i18n/index.js'
import './index.css'
// CSS del viewer (DocumentViewer): va DOPO index.css, perché ridefinisce
// variabili che Tailwind dichiara lì
import './styles.css'

createRoot(document.getElementById('root')).render(
  <BrowserRouter>
    <App />
  </BrowserRouter>
)
