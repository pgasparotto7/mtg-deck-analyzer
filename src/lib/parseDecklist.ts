export type ParsedLine = {
  quantity: number
  name: string
  raw: string
  ok: boolean
}

const CARD_LINE = /^(\d+)\s+(.+)$/

const SECTION_HEADERS = new Set([
  'deck',
  'main',
  'mainboard',
  'maindeck',
  'sideboard',
  'commander',
  'maybeboard',
])

function isIgnoredLine(line: string): boolean {
  if (line.length === 0) {
    return true
  }

  if (line.startsWith('//')) {
    return true
  }

  return SECTION_HEADERS.has(line.toLowerCase())
}

export function parseDecklist(text: string): ParsedLine[] {
  const lines = text.split(/\r?\n/)
  const parsed: ParsedLine[] = []

  for (const original of lines) {
    const raw = original.trim()

    if (isIgnoredLine(raw)) {
      continue
    }

    const match = CARD_LINE.exec(raw)

    if (!match) {
      parsed.push({
        quantity: 0,
        name: '',
        raw,
        ok: false,
      })
      continue
    }

    const quantity = Number(match[1])
    const name = match[2].trim()

    if (!Number.isInteger(quantity) || quantity < 1 || name.length === 0) {
      parsed.push({
        quantity: 0,
        name: '',
        raw,
        ok: false,
      })
      continue
    }

    parsed.push({
      quantity,
      name,
      raw,
      ok: true,
    })
  }

  return parsed
}
