const PLACEHOLDER_SOURCE = String.raw`\[[A-Z][A-Z0-9_]*_\d+\]`

function known(values, placeholder) {
  return values != null
    && Object.prototype.hasOwnProperty.call(values, placeholder)
}

// Sostituzione volutamente letterale e a passaggio singolo: i caratteri del
// valore (backtick, HTML, "$&", newline...) non sono sintassi e un valore che
// contiene a sua volta un testo simile a un placeholder non viene riesaminato.
export function restorePlaceholders(text, values) {
  if (!text || !values) return text
  return text.replace(new RegExp(PLACEHOLDER_SOURCE, 'g'), (placeholder) =>
    known(values, placeholder) ? String(values[placeholder]) : placeholder)
}

export function hasRestorablePlaceholder(text, values) {
  if (!text || !values) return false
  return Array.from(text.matchAll(new RegExp(PLACEHOLDER_SOURCE, 'g')))
    .some((match) => known(values, match[0]))
}

// Segmenti per il renderer React dei blocchi di codice. Il parsing Markdown
// è già finito quando questi pezzi vengono usati: i valori diventano nodi di
// testo e non possono chiudere il code block o introdurre markup/link.
export function splitRestorablePlaceholders(text, values) {
  if (!text || !values) return [{ text: text || '' }]
  const parts = []
  const pushText = (value) => {
    if (!value) return
    const last = parts[parts.length - 1]
    if (last && !last.placeholder) last.text += value
    else parts.push({ text: value })
  }
  const pattern = new RegExp(PLACEHOLDER_SOURCE, 'g')
  let pos = 0
  let match
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > pos) pushText(text.slice(pos, match.index))
    const placeholder = match[0]
    if (known(values, placeholder)) {
      parts.push({ placeholder, value: String(values[placeholder]) })
    } else {
      pushText(placeholder)
    }
    pos = match.index + placeholder.length
  }
  if (pos < text.length) pushText(text.slice(pos))
  return parts.length ? parts : [{ text }]
}
