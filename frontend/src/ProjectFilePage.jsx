import React, { useEffect, useMemo, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api, downloadFile, downloadJson, fetchProjectPagePng } from './api.js'
import { useJobs } from './jobs.jsx'
import DocumentViewer from './DocumentViewer.jsx'

const SOURCE_EXT_RE =
  /\.(pdf|docx|pptx|xlsx|txt|md|csv|tsv|json|xml|png|jpe?g|bmp|gif|tiff?|webp)$/i
const stem = (name) => name.replace(SOURCE_EXT_RE, '')

// Route /projects/:id/files/:fileId — un file di progetto GIÀ confermato:
// DocumentViewer con i servizi puntati sugli endpoint del progetto (la mappa
// mostrata è il registro condiviso filtrato ai segnaposto di questo file).
// Le regole di modifica le impone il server: un'operazione distruttiva dopo
// il primo messaggio risponde 409 col motivo.
//
// Un file DA RIVEDERE non passa di qui: si rivede e si conferma nel modal
// della pagina del progetto (un solo punto di conferma), quindi chi arriva a
// questa rotta con un file non confermato viene rimandato al progetto.
export default function ProjectFilePage() {
  const { id, fileId } = useParams()
  const [doc, setDoc] = useState(null)
  const [error, setError] = useState('')
  const { addJob, docRefresh } = useJobs()
  const { t } = useTranslation('project')

  const load = () => api.getProjectFile(id, fileId).then(setDoc)
    .catch((e) => setError(e.message))

  useEffect(() => {
    setDoc(null)
    setError('')
    api.getProjectFile(id, fileId).then(setDoc).catch((e) => setError(e.message))
  }, [id, fileId])

  // job di colonna su QUESTO file concluso: mappa e preview sono cambiate
  useEffect(() => {
    if (docRefresh.n > 0 && docRefresh.docId === fileId) load()
  }, [docRefresh])           // eslint-disable-line react-hooks/exhaustive-deps

  // stessi contratti dei servizi Documenti, sugli endpoint del progetto
  const services = useMemo(() => ({
    // anonimizzazione di colonna come JOB in coda (vedi DocumentViewer)
    columnJobs: true,
    fetchPagePng: (_fid, source, n, rev) =>
      fetchProjectPagePng(id, fileId, source, n, rev),
    extractText: (_fid, source, page, rect) =>
      api.projectExtractText(id, fileId, source, page, rect),
    deanonymize: (_fid, body) => api.projectDeanonymize(id, fileId, body),
    anonymizeText: (_fid, text, saveTerm, termTag) =>
      api.projectAnonymizeText(id, fileId, text, saveTerm, termTag),
    getColumns: () => api.projectGetColumns(id, fileId),
    columnInfo: (_fid, sheet, column) =>
      api.projectColumnInfo(id, fileId, sheet, column),
    anonymizeColumn: (_fid, sheet, column) =>
      api.projectAnonymizeColumn(id, fileId, sheet, column),
    deanonymizeColumn: (_fid, sheet, column) =>
      api.projectDeanonymizeColumn(id, fileId, sheet, column),
    sealArea: (_fid, page, rect, allPages) =>
      api.projectSealArea(id, fileId, page, rect, allPages),
    removeSeal: (_fid, n) => api.projectRemoveSeal(id, fileId, n),
    reprocessOcr: (_fid) => api.projectReprocessOcr(id, fileId),
  }), [id, fileId])

  if (error) {
    return (
      <main className="main">
        <div className="page-head">
          <Link to={`/projects/${id}`} className="ghost backlink">{t('file.back')}</Link>
        </div>
        <div className="error">{error}</div>
      </main>
    )
  }
  if (!doc) {
    return (
      <main className="main">
        <div className="muted">{t('state.loading', { ns: 'common' })}</div>
      </main>
    )
  }
  // da rivedere: la revisione (e la conferma) stanno nel modal del progetto
  if (!doc.confirmed) return <Navigate to={`/projects/${id}`} replace />

  const ext = doc.filename.match(SOURCE_EXT_RE)?.[0] ?? '.pdf'
  return (
    <main className="main">
      <div className="page-head" style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <Link to={`/projects/${id}`} className="ghost backlink">{t('file.back')}</Link>
        <h2 style={{ margin: 0, fontSize: 16 }} title={doc.filename}>{doc.filename}</h2>
        {doc.briefing?.label && (
          <span className="muted" style={{ fontSize: 12 }}>{doc.briefing.label}</span>
        )}
        <span className="pill" style={{ color: 'var(--ok, #047857)', fontSize: 12 }}>
          {t('file.confirmed')}
        </span>
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
          {/* anche il suffisso del nome scaricato segue la lingua: il file
              finisce sul disco dell'utente, non resta dentro l'applicazione */}
          <button className="ghost"
                  onClick={() => downloadFile(`/api/projects/${id}/files/${fileId}/download`,
                    stem(doc.filename) + t('file.anonSuffix') + ext)}>
            {t('file.downloadAnon')}
          </button>
          <button className="ghost"
                  onClick={() => downloadFile(`/api/projects/${id}/files/${fileId}/download-original`,
                    doc.filename)}>
            {t('file.downloadOriginal')}
          </button>
          <button className="ghost"
                  onClick={() => downloadJson(doc.mapping,
                    stem(doc.filename) + t('file.mapSuffix') + '.json')}>
            {t('file.downloadMap')}
          </button>
        </span>
      </div>
      <DocumentViewer doc={doc} services={services}
                      onChange={setDoc} onJobStart={addJob} />
    </main>
  )
}
