// Pure helpers shared by the renderer and contributor regression tests.
export function splitBlocks(src) {
  const blocks = []
  const lines = src.replace(/\r\n/g, '\n').split('\n')
  let buf = []
  let i = 0
  const flush = () => {
    if (buf.length) { blocks.push({ type: 'text', content: buf.join('\n') }); buf = [] }
  }
  while (i < lines.length) {
    const m = lines[i].match(/^ {0,3}```([^\s`]*)\s*$/)
    if (m) {
      flush()
      const code = []
      i++
      while (i < lines.length && !/^ {0,3}```\s*$/.test(lines[i])) { code.push(lines[i]); i++ }
      i++
      blocks.push({ type: 'code', lang: m[1], content: code.join('\n') })
    } else {
      buf.push(lines[i]); i++
    }
  }
  flush()
  return blocks
}

export function isTableDelimiter(line) {
  let value = line.trim()
  if (value.startsWith('|')) value = value.slice(1)
  if (value.endsWith('|')) value = value.slice(0, -1)
  return value.split('|').every((cell) => /^:?-+:?$/.test(cell.trim()))
}

export function utf16Entities(text, entities) {
  // API spans count Python Unicode code points; JS slice counts UTF-16 units.
  const offsets = [0]
  let position = 0
  for (const character of text) {
    position += character.length
    offsets.push(position)
  }
  return entities.filter(({ start, end }) => Number.isInteger(start)
    && Number.isInteger(end) && start >= 0 && end >= start && end < offsets.length)
    .map((entity) => ({ ...entity, start: offsets[entity.start], end: offsets[entity.end] }))
}
