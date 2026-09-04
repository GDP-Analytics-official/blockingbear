// Gruppi di tag della UI, letti da /api/tags (campo "groups": {nome: [tag, ...]}).
// L'unico per ora è «cyber» (Cybersecurity): nei picker sta in fondo, sotto un
// separatore, richiuso, con una casella padre a tre stati. Il padre NON è un
// tag: lo stato salvato resta la sola lista di esclusione e il padre si deriva
// dai figli (tutti accesi -> acceso, tutti spenti -> spento, altrimenti
// indeterminato). Un tag escluso che il backend non elenca più resta fra i
// "rest", come prima.
export function splitGroup(tags, groups, name = 'cyber') {
  const members = new Set(groups?.[name] || [])
  const inGroup = tags.filter((t) => members.has(t))
  const rest = tags.filter((t) => !members.has(t))
  return { rest, inGroup }
}

// true / false / 'indeterminate': il valore che Radix Checkbox si aspetta
export function groupChecked(children, excludedSet) {
  const on = children.filter((t) => !excludedSet.has(t)).length
  if (on === 0) return false
  if (on === children.length) return true
  return 'indeterminate'
}

// lista di esclusione dopo un clic sul padre: se erano tutti accesi si spengono
// tutti, altrimenti (nessuno o solo alcuni) si accendono tutti
export function toggleGroup(children, excludedSet) {
  const next = new Set(excludedSet)
  const allOn = children.every((t) => !next.has(t))
  for (const t of children) {
    if (allOn) next.add(t)
    else next.delete(t)
  }
  return [...next].sort()
}
