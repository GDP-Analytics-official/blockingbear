// Client API minimale: token JWT in localStorage (condiviso da tutte le schede
// del browser, così una finestra nuova ritrova la sessione), errori come
// Error(message).

import i18n from '@/i18n/index.js'

let token = localStorage.getItem('token') || null

// --- Errori --------------------------------------------------------------
// Il server manda `code` (stabile) accanto a `detail` (italiano, vedi
// app/errors.py): qui si prova a tradurre il codice e si ripiega sul testo
// del server. Così un errore che il backend non ha ancora convertito, o che
// è più nuovo di questi cataloghi, resta comunque leggibile invece di
// uscire come «error.qualcosa».
//
// `t` si prende da i18n direttamente e non da useTranslation: questo file non
// è un componente e viene chiamato anche fuori da React (SSE, retry).
function errorMessage(body, status) {
  const fallback = body?.detail || body?.error || i18n.t('error.http', { status })
  if (typeof fallback !== 'string') return i18n.t('error.http', { status })
  if (!body?.code) return fallback
  return i18n.t(`error.${body.code}`,
                { ...(body.params || {}), defaultValue: fallback })
}

async function errorFrom(res) {
  let body = null
  try { body = await res.json() } catch { /* risposta non JSON */ }
  const err = new Error(errorMessage(body, res.status))
  err.status = res.status
  err.code = body?.code || null
  return err
}

export function setToken(t) {
  token = t
  if (t) localStorage.setItem('token', t)
  else localStorage.removeItem('token')
}

export function getToken() {
  return token
}

async function request(path, opts = {}) {
  const headers = { ...(opts.headers || {}) }
  if (token) headers.Authorization = `Bearer ${token}`
  let res
  try {
    res = await fetch(path, { ...opts, headers })
  } catch (e) {
    // L'annullamento è voluto (il chiamante ha abortito): deve arrivargli
    // intatto, o passerebbe per un guasto di rete. Tutto il resto qui è
    // server spento o cavo staccato, e il messaggio del browser
    // («Failed to fetch») non è né tradotto né utile.
    if (e?.name === 'AbortError') throw e
    throw new Error(i18n.t('error.network'))
  }
  if (res.status === 401 && token) {
    setToken(null)
    window.dispatchEvent(new Event('blockingbear-logout'))
  }
  if (!res.ok) throw await errorFrom(res)
  const ct = res.headers.get('content-type') || ''
  return ct.includes('application/json') ? res.json() : res
}

export const api = {
  login: (username, password) =>
    request('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    }),
  me: () => request('/api/auth/me'),
  changePassword: (old_password, new_password) =>
    request('/api/auth/password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_password, new_password }),
    }),

  // tag rilevabili: {model, regex_only, all} (derivati dal config del modello)
  getTags: () => request('/api/tags'),
  // Anonimizzazione, due livelli in un GET solo:
  //   excluded_tags, custom_terms          = globali (li scrive solo l'admin)
  //   my_custom_terms                      = i miei
  //   effective_custom_terms               = l'unione applicata ai miei
  //                                          documenti, ogni voce con `scope`
  getAnonDefaults: () => request('/api/settings/anonymization'),
  // la lista GLOBALE + le categorie escluse (solo admin)
  saveAnonDefaults: (body) =>
    request('/api/settings/anonymization', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  // la MIA lingua dell'interfaccia ("it" | "en"). Preferenza personale senza
  // livello globale: la si legge da /api/auth/me insieme al profilo.
  saveMyLanguage: (lang) =>
    request('/api/settings/my-language', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lang }),
    }),
  // il MIO tutorial del primo accesso: done=true finito o saltato,
  // done=false lo fa ricomparire (Impostazioni → «Rivedi il tutorial»)
  saveMyTour: (done) =>
    request('/api/settings/my-tour', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ done }),
    }),
  // la MIA lista di termini (chiunque, admin compreso)
  saveMyAnonTerms: (custom_terms) =>
    request('/api/settings/my-terms', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ custom_terms }),
    }),
  listJobs: () => request('/api/jobs'),
  getJob: (id) => request(`/api/jobs/${id}`),
  // annulla: immediato se in coda, cooperativo se in lavorazione (lo stato
  // `canceled` arriva via SSE al primo checkpoint del worker)
  cancelJob: (id) => request(`/api/jobs/${id}/cancel`, { method: 'POST' }),

  // parametri di esercizio (solo admin): lista [{key, value, default, ...meta}]
  getSettings: () => request('/api/settings'),
  saveSettings: (values) =>
    request('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ values }),
    }),

  listUsers: () => request('/api/users'),
  // key_limit (USD) e key_limit_reset (daily|weekly|monthly) valgono per la
  // chiave OpenRouter personale creata insieme all'utente (se il server ha
  // la management key); null/undefined = chiave senza tetto.
  // must_change_password: finché l'utente non se ne sceglie una sua, la
  // sessione vale solo per cambiarla (schermata dedicata, vedi ForcePassword)
  createUser: (username, password, role, key_limit, key_limit_reset,
               must_change_password = false) =>
    request('/api/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username, password, role,
        key_limit: key_limit ?? null,
        key_limit_reset: key_limit_reset || null,
        must_change_password,
      }),
    }),
  deleteUser: (id) => request(`/api/users/${id}`, { method: 'DELETE' }),
  // crea la chiave se manca, o revoca+ricrea (rotazione)
  rotateUserKey: (id, body) =>
    request(`/api/users/${id}/key`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }),
  // aggiorna limite/cadenza/stato: {limit, clear_limit, limit_reset,
  // clear_limit_reset, disabled}
  patchUserKey: (id, body) =>
    request(`/api/users/${id}/key`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  // --- Dashboard costi (solo admin, tutto on-demand) -----------------------
  // {credits, keys: [{...chiave, user}], users_without_key}
  usageOverview: () => request('/api/usage/overview'),
  // proxy validato di /analytics/query: {data: [...], metadata: {...}}
  usageQuery: (body) =>
    request('/api/usage/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  usageMeta: () => request('/api/usage/meta'),

  // --- Chat LLM via OpenRouter -------------------------------------------
  // catalogo normalizzato {models, source}; ogni modello porta le capability
  orModels: (refresh) => request(`/api/openrouter/models${refresh ? '?refresh=1' : ''}`),
  // {configured, personal_key, sandbox, ... ; se admin: provisioning,
  // management_masked, masked, credits}
  orStatus: () => request('/api/openrouter/status'),
  // management key OpenRouter (solo admin, pagina API keys): validata da
  // OpenRouter prima del salvataggio, torna solo mascherata
  setManagementKey: (key) =>
    request('/api/settings/management-key', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    }),

  // --- Installazione guidata (pubblico finché non esiste un utente) --------
  // {needed, management_key, admin}
  setupStatus: () => request('/api/setup/status'),
  setupManagementKey: (key) =>
    request('/api/setup/management-key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    }),
  // crea `admin` con la password scelta e la sua chiave OpenRouter; NON
  // autentica: il login si fa dopo dalla pagina di accesso
  // replace=true: elimina l'admin già creato (wizard ripreso) e lo ricrea
  setupAdmin: (password, lang, replace = false) =>
    request('/api/setup/admin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password, lang: lang || null, replace }),
    }),
  // catalogo modelli durante il wizard (con la chiave dell'admin appena creato)
  setupModels: () => request('/api/setup/models'),
  // anteprima della regola del modello predefinito nel wizard: {model, detail};
  // la deroga ZDR non è ancora salvata, si passa quella scelta allo step prima
  setupResolveModel: (rule, allowNonZdr) =>
    request('/api/setup/resolve-model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rule, chat_allow_non_zdr: allowNonZdr }),
    }),
  // ultimo passo: {excluded_tags, custom_terms, chat_anonymization_policy,
  // chat_allow_non_zdr, default_model_rule, model_access}; alza il flag di
  // configurazione completata
  setupFinish: (body) =>
    request('/api/setup/finish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  // Modello predefinito della chat. Si salva una REGOLA (modello fisso, o le
  // tre risposte della procedura guidata), non un id: {rule} è quella dell'installazione
  // (solo admin), {personal} la propria (null = «default», segui quella
  // dell'installazione), {resolved} il modello a cui si risolve adesso.
  getDefaultModel: () => request('/api/settings/default-model'),
  // modelli visibili agli utenti non admin (white list): {enabled, models,
  // locked}; `locked` è il modello predefinito dell'installazione, che entra
  // nella lista da solo e la UI mostra selezionato e non deselezionabile
  getModelAccess: () => request('/api/settings/model-access'),
  setModelAccess: (enabled, models) =>
    request('/api/settings/model-access', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled, models }),
    }),
  setDefaultModel: (rule) =>
    request('/api/settings/default-model', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rule }),
    }),
  setMyDefaultModel: (rule) =>
    request('/api/settings/my-default-model', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rule }),
    }),
  // a quale modello porterebbe ADESSO una regola, senza salvarla: {model,
  // detail} — l'anteprima della procedura guidata, calcolata dal server
  resolveDefaultModel: (rule) =>
    request('/api/settings/default-model/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rule }),
    }),

  // ricerca (substring + fuzzy sui typo) su titoli e messaggi delle chat
  // libere; risultati già ordinati per pertinenza, con snippet
  searchChats: (q) => request(`/api/chats/search?q=${encodeURIComponent(q)}`),
  // senza project: le chat LIBERE; con project: le chat di quel progetto
  listChats: (project) =>
    request(`/api/chats${project ? `?project=${encodeURIComponent(project)}` : ''}`),
  // il modo (anonimizzata o no) si sceglie qui e vale per tutta la chat;
  // in un progetto il modo è quello del progetto (anonymized ignorato)
  createChat: (model, anonymized = false, projectId = null) =>
    request('/api/chats', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: model || '', anonymized,
                             project_id: projectId }),
    }),
  getChat: (id) => request(`/api/chats/${id}`),
  // body: {title, model, options, anonymized, anon_options}. anon_options =
  // {excluded_tags: [...]} sono le categorie lasciate in chiaro in questa
  // conversazione; excluded_tags null torna ai default dell'amministratore.
  patchChat: (id, body) =>
    request(`/api/chats/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteChat: (id) => request(`/api/chats/${id}`, { method: 'DELETE' }),
  uploadChatAttachment: (id, file) => {
    const form = new FormData()
    form.append('file', file)
    return request(`/api/chats/${id}/attachments`, { method: 'POST', body: form })
  },
  removeChatAttachment: (convId, attId) =>
    request(`/api/chats/${convId}/attachments/${attId}`, { method: 'DELETE' }),
  chatAttachmentUrl: (convId, attId) =>
    `/api/chats/${convId}/attachments/${attId}`,
  // la copia redatta: esiste solo dopo l'invio (o l'anteprima pre-invio)
  chatAttachmentAnonymizedUrl: (convId, attId) =>
    `/api/chats/${convId}/attachments/${attId}/anonymized`,
  stopChat: (id) => request(`/api/chats/${id}/stop`, { method: 'POST' }),
  // registro delle entità della conversazione + revisione delle fusioni
  listChatEntities: (id) => request(`/api/chats/${id}/entities`),
  mergeChatEntity: (convId, entityId, into) =>
    request(`/api/chats/${convId}/entities/${entityId}/merge`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ into }),
    }),
  keepChatEntitySeparate: (convId, entityId) =>
    request(`/api/chats/${convId}/entities/${entityId}/keep-separate`,
            { method: 'POST' }),
  // il registro come lo vuole ReviewModal/EntityReview: le tre operazioni,
  // già legate al loro scope (la chat qui, il progetto più sotto). Ogni
  // risposta ha la stessa forma {entities, suggestions[, staged]}.
  chatRegistry: (convId) => ({
    list: () => api.listChatEntities(convId),
    merge: (entityId, into) => api.mergeChatEntity(convId, entityId, into),
    keepSeparate: (entityId) => api.keepChatEntitySeparate(convId, entityId),
  }),
  // «continua»: il turno fermo in attesa che si confermino le fusioni
  // (evento SSE merge_check) riprende e parte verso il modello
  continueChatMessage: (convId) =>
    request(`/api/chats/${convId}/messages/continue`, { method: 'POST' }),

  // --- Anteprima pre-invio del turno anonimizzato (chat) -------------------
  // stessi contratti degli endpoint dei file di progetto, sul turno in anteprima
  discardStaged: (convId) =>
    request(`/api/chats/${convId}/staged`, { method: 'DELETE' }),
  stagedExtractText: (convId, itemId, source, page, rect) =>
    request(`/api/chats/${convId}/staged/${itemId}/extract`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source, page, rect }),
    }),
  // {placeholder} o {label} -> stato staged completo (tutti gli item)
  stagedDeanonymize: (convId, body) =>
    request(`/api/chats/${convId}/staged/deanonymize`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  stagedAnonymizeText: (convId, text, saveTerm = false, termTag = null) =>
    request(`/api/chats/${convId}/staged/anonymize-text`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, save_term: saveTerm, term_tag: termTag }),
    }),
  // sigilla un'area dell'anteprima di un allegato PDF/immagine (come
  // api.sealArea, ma sull'item del turno in anteprima)
  stagedSealArea: (convId, itemId, page, rect, allPages = false) =>
    request(`/api/chats/${convId}/staged/${itemId}/seal-area`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ page, rect, all_pages: allPages }),
    }),
  stagedRemoveSeal: (convId, itemId, n) =>
    request(`/api/chats/${convId}/staged/${itemId}/seal-area/${n}`,
            { method: 'DELETE' }),
  // lo stato dell'anteprima com'è adesso: serve a riprendere il modal dopo
  // la rielaborazione con OCR, che gira in un worker e riscrive il turno
  getStaged: (convId) => request(`/api/chats/${convId}/staged`),
  // colonne cliccabili di un allegato xlsx del turno (come projectGetColumns)
  stagedGetColumns: (convId, itemId) =>
    request(`/api/chats/${convId}/staged/${itemId}/columns`),
  stagedColumnInfo: (convId, itemId, sheet, column) =>
    request(`/api/chats/${convId}/staged/${itemId}/column-info`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sheet, column }),
    }),
  // sincrone come le altre modifiche dell'anteprima: rispondono con lo stato
  // staged completo (la mappa è condivisa, cambiano tutti i pezzi)
  stagedAnonymizeColumn: (convId, itemId, sheet, column) =>
    request(`/api/chats/${convId}/staged/${itemId}/anonymize-column`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sheet, column }),
    }),
  stagedDeanonymizeColumn: (convId, itemId, sheet, column) =>
    request(`/api/chats/${convId}/staged/${itemId}/deanonymize-column`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sheet, column }),
    }),
  // rilegge l'allegato con l'OCR attivo: JOB in coda (202 col descrittore, da
  // seguire con jobEvents come i caricamenti di progetto)
  stagedReprocessOcr: (convId, itemId) =>
    request(`/api/chats/${convId}/staged/${itemId}/reprocess-ocr`,
            { method: 'POST' }),

  // --- Progetti (file pre-caricati + N chat sul loro registro condiviso) ----
  listProjects: () => request('/api/projects'),
  // il modo si sceglie qui e vale per file e chat del progetto; anonOptions =
  // {excluded_tags: [...]} (solo progetti anonimizzati)
  createProject: (name, anonymized, anonOptions = null) =>
    request('/api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, anonymized, anon_options: anonOptions }),
    }),
  getProject: (id) => request(`/api/projects/${id}`),
  patchProject: (id, body) =>
    request(`/api/projects/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteProject: (id) => request(`/api/projects/${id}`, { method: 'DELETE' }),
  // sonda pre-upload per il popup OCR:
  // {probe_id, images, pdf_no_text, ocr_available}. I byte restano sul
  // server: uploadProjectFile cita `probe_id` e non li rimanda.
  probeProject: (file) => {
    const form = new FormData()
    form.append('file', file)
    return request('/api/projects/probe', { method: 'POST', body: form })
  },
  // sonda abbandonata (popup OCR annullato): libera i byte messi da parte
  dropProbe: (probeId) =>
    request(`/api/projects/probe/${probeId}`, { method: 'DELETE' }),
  // progetto anonimizzato: 202 col descrittore del JOB, da seguire con
  // jobEvents(id) fino a status done/failed; progetto in chiaro: risposta
  // immediata {file}. options (facoltative): {ocr} scelto nel popup OCR.
  // probeId: il biglietto della sonda — i byte sono già sul server e il
  // file NON viene ritrasferito (410 se scaduto: si ritenta col file).
  uploadProjectFile: (projectId, file, options, probeId) => {
    const form = new FormData()
    if (probeId) form.append('probe_id', probeId)
    else form.append('file', file)
    if (options) form.append('options', JSON.stringify(options))
    return request(`/api/projects/${projectId}/files`,
                   { method: 'POST', body: form })
  },
  getProjectFile: (projectId, fileId) =>
    request(`/api/projects/${projectId}/files/${fileId}`),
  deleteProjectFile: (projectId, fileId) =>
    request(`/api/projects/${projectId}/files/${fileId}`,
            { method: 'DELETE' }),
  // il file diventa visibile alle chat (controllo di uscita incluso)
  confirmProjectFile: (projectId, fileId) =>
    request(`/api/projects/${projectId}/files/${fileId}/confirm`,
            { method: 'POST' }),
  // registro condiviso del progetto (le stesse entità di tutte le sue chat)
  // + le fusioni proposte, calcolate al momento della chiamata
  listProjectEntities: (projectId) =>
    request(`/api/projects/${projectId}/entities`),
  mergeProjectEntity: (projectId, entityId, into) =>
    request(`/api/projects/${projectId}/entities/${entityId}/merge`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ into }),
    }),
  keepProjectEntitySeparate: (projectId, entityId) =>
    request(`/api/projects/${projectId}/entities/${entityId}/keep-separate`,
            { method: 'POST' }),
  projectRegistry: (projectId) => ({
    list: () => api.listProjectEntities(projectId),
    merge: (entityId, into) => api.mergeProjectEntity(projectId, entityId, into),
    keepSeparate: (entityId) => api.keepProjectEntitySeparate(projectId, entityId),
  }),
  // ri-redazione col registro corrente (solo file non confermati)
  realignProjectFile: (projectId, fileId) =>
    request(`/api/projects/${projectId}/files/${fileId}/realign`,
            { method: 'POST' }),
  // la stessa ri-redazione ma in coda (202 col descrittore del job): è
  // l'azione del warning di disallineamento, ammessa anche sui confermati
  rebuildProjectFile: (projectId, fileId) =>
    request(`/api/projects/${projectId}/files/${fileId}/rebuild`,
            { method: 'POST' }),
  // verifica esplicita dell'allineamento (per i file che la GET del progetto
  // lascia in sospeso perché troppo grossi da leggere in linea)
  projectStaleCheck: (projectId, fileId) =>
    request(`/api/projects/${projectId}/files/${fileId}/stale-check`,
            { method: 'POST' }),
  // rielabora il file con l'OCR attivo (job in coda: 202 col descrittore,
  // da seguire con jobEvents come i caricamenti)
  projectReprocessOcr: (projectId, fileId) =>
    request(`/api/projects/${projectId}/files/${fileId}/reprocess-ocr`,
            { method: 'POST' }),
  // modifiche dalla preview del file di progetto (stessi contratti degli
  // endpoint staged della chat): rect in punti PDF
  projectExtractText: (projectId, fileId, source, page, rect) =>
    request(`/api/projects/${projectId}/files/${fileId}/extract`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source, page, rect }),
    }),
  projectDeanonymize: (projectId, fileId, body) =>
    request(`/api/projects/${projectId}/files/${fileId}/deanonymize`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  projectAnonymizeText: (projectId, fileId, text, saveTerm = false, termTag = null) =>
    request(`/api/projects/${projectId}/files/${fileId}/anonymize-text`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, save_term: saveTerm, term_tag: termTag }),
    }),
  projectSealArea: (projectId, fileId, page, rect, allPages = false) =>
    request(`/api/projects/${projectId}/files/${fileId}/seal-area`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ page, rect, all_pages: allPages }),
    }),
  projectRemoveSeal: (projectId, fileId, n) =>
    request(`/api/projects/${projectId}/files/${fileId}/seal-area/${n}`,
            { method: 'DELETE' }),
  projectGetColumns: (projectId, fileId) =>
    request(`/api/projects/${projectId}/files/${fileId}/columns`),
  projectColumnInfo: (projectId, fileId, sheet, column) =>
    request(`/api/projects/${projectId}/files/${fileId}/column-info`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sheet, column }),
    }),
  projectAnonymizeColumn: (projectId, fileId, sheet, column) =>
    request(`/api/projects/${projectId}/files/${fileId}/anonymize-column`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sheet, column }),
    }),
  projectDeanonymizeColumn: (projectId, fileId, sheet, column) =>
    request(`/api/projects/${projectId}/files/${fileId}/deanonymize-column`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sheet, column }),
    }),
}

// Lettore SSE condiviso da invio e riaggancio: fetch con header
// Authorization (EventSource non permette né il POST né gli header) e
// parsing a mano delle righe `data:`. Ritorna la funzione per annullare la
// LETTURA: il turno lato server non si ferma chiudendo la connessione
// (si ferma con api.stopChat).
function streamSse(url, init, onEvent, onError) {
  const controller = new AbortController()
  ;(async () => {
    let res
    try {
      res = await fetch(url, {
        ...init,
        headers: {
          ...(init.headers || {}),
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        signal: controller.signal,
      })
    } catch (e) {
      if (e.name !== 'AbortError') onError?.(new Error(i18n.t('error.connectionLost')))
      return
    }
    if (res.status === 401 && token) {
      setToken(null)
      window.dispatchEvent(new Event('blockingbear-logout'))
      return
    }
    if (!res.ok) {
      onError?.(await errorFrom(res))
      return
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let nl
        while ((nl = buffer.indexOf('\n')) !== -1) {
          const line = buffer.slice(0, nl).trim()
          buffer = buffer.slice(nl + 1)
          // saltare i keep-alive ": ping" e le righe vuote
          if (!line || line.startsWith(':')) continue
          if (line.startsWith('data: ')) {
            try { onEvent(JSON.parse(line.slice(6))) } catch { /* frammento */ }
          }
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') onError?.(new Error('Stream interrotto.'))
    }
  })()
  return () => controller.abort()
}

// Invio di un messaggio in chat: POST con body e risposta SSE. onEvent
// riceve ogni evento del turno {type: 'turn'|'start'|'text'|'reasoning'|
// 'tool_call'|'tool_result'|'error'|'done', ...}.
export function streamChatMessage(convId, body, onEvent, onError) {
  return streamSse(`/api/chats/${convId}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }, onEvent, onError)
}

// Riaggancio al turno in corso (dopo un refresh o un cambio di pagina): il
// server rigioca gli eventi DALL'INIZIO — così si ricostruisce esattamente
// ciò che il turno aveva già mostrato — e poi segue in diretta. 404 se non
// c'è niente da rigiocare (lo gestisce onError).
export function attachChatStream(convId, onEvent, onError) {
  return streamSse(`/api/chats/${convId}/messages/live`, { method: 'GET' },
                   onEvent, onError)
}

// Stream SSE dello stato di un job. EventSource non può impostare header:
// il token viaggia come query param (l'endpoint lo valida come gli altri).
// La riconnessione automatica è di EventSource; il server rimanda sempre lo
// stato corrente all'apertura, quindi non si perde mai un aggiornamento.
export function jobEvents(jobId, onUpdate) {
  const es = new EventSource(
    `/api/jobs/${jobId}/events?token=${encodeURIComponent(token || '')}`)
  es.onmessage = (e) => {
    const job = JSON.parse(e.data)
    // senza close() esplicita EventSource riaprirebbe lo stream all'infinito
    if (job.status === 'done' || job.status === 'failed' ||
        job.status === 'canceled') es.close()
    onUpdate(job)
  }
  return es
}

// Pagine PNG dell'anteprima pre-invio della chat: blob URL perché il token
// via header non funziona nei tag <img>. rev = cache-buster: dopo una
// modifica della mappa le pagine cambiano.
// cache: 'no-store' — l'URL di un pezzo dell'anteprima è IDENTICO a ogni
// turno (stessa chat, item "prompt", rev che riparte da 0 a ogni
// preparazione): senza questo, dal secondo messaggio in poi il browser
// riproporrebbe le pagine del primo. La richiesta non passa mai dalla cache,
// quindi non conta nemmeno quello che il browser si è già tenuto da parte.
export async function fetchStagedPagePng(convId, itemId, source, n, rev = 0, signal) {
  const res = await request(
    `/api/chats/${convId}/staged/${itemId}/pages/${source}/${n}.png?rev=${rev}`,
    { cache: 'no-store', signal })
  return URL.createObjectURL(await res.blob())
}

// Pagine PNG della preview di un file di progetto (stesso meccanismo).
export async function fetchProjectPagePng(projectId, fileId, source, n, rev = 0, signal) {
  const res = await request(
    `/api/projects/${projectId}/files/${fileId}/pages/${source}/${n}.png?rev=${rev}`, { signal })
  return URL.createObjectURL(await res.blob())
}

// Allegato come blob URL (per mostrare le immagini nel thread): come le
// pagine PNG, un <img src> nudo non porterebbe l'header Authorization.
export async function fetchAttachmentUrl(convId, attId) {
  const res = await request(`/api/chats/${convId}/attachments/${attId}`)
  return URL.createObjectURL(await res.blob())
}

export async function downloadFile(path, filename) {
  const res = await request(path)
  const url = URL.createObjectURL(await res.blob())
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export function downloadJson(obj, filename) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}
