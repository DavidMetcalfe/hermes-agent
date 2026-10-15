import fs from 'node:fs'
import path from 'node:path'

/**
 * Resolution for the Desktop offline-install media directory.
 *
 * Precedence:
 *   1. `HERMES_OFFLINE_DIR` environment variable
 *   2. `hermes.offlineDir` in `<hermesHome>/config.yaml`
 *
 * Why this is not a `.match(/hermes:[\s\S]*?offlineDir:/)` over the raw text:
 * a flat scan does not respect YAML block boundaries. It picks up a commented
 * out setting, a sibling mapping that merely appears after `hermes:`, or the
 * value plus a trailing `# comment`, and any of those silently changes which
 * media directory the bootstrap installs from. The parser below walks the
 * actual block structure: it finds the top-level `hermes:` mapping, then reads
 * only the scalar `offlineDir` key that is a DIRECT child of it.
 */

export interface ResolveOfflineDirOptions {
  hermesHome: string
  env?: NodeJS.ProcessEnv
}

/** Strip a `# comment` that is not inside a quoted scalar. */
function stripTrailingComment(raw: string): string {
  let quote: string | null = null

  for (let index = 0; index < raw.length; index += 1) {
    const char = raw[index]

    if (quote) {
      if (char === quote) {
        quote = null
      }

      continue
    }

    if (char === '"' || char === "'") {
      quote = char

      continue
    }

    // A comment marker only starts a comment at the start of the value or when
    // preceded by whitespace -- `E:/a#b` is a path, `E:/a #b` is a comment.
    if (char === '#' && (index === 0 || /\s/.test(raw[index - 1]))) {
      return raw.slice(0, index)
    }
  }

  return raw
}

/** Remove one layer of surrounding quotes, then trim. */
function unquoteScalar(raw: string): string {
  const trimmed = raw.trim()

  if (trimmed.length >= 2) {
    const first = trimmed[0]
    const last = trimmed[trimmed.length - 1]

    if ((first === '"' || first === "'") && last === first) {
      return trimmed.slice(1, -1).trim()
    }
  }

  return trimmed
}

/**
 * Read the scalar `hermes.offlineDir` value from config.yaml text.
 *
 * Returns '' when the key is absent, commented out, empty, non-scalar
 * (block/null), or nested inside some other key of the `hermes` mapping.
 */
export function extractOfflineDirFromConfig(text: string): string {
  const lines = text.split(/\r?\n/)

  let hermesIndent: number | null = null
  let hermesIndex = -1

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]

    if (!line.trim() || /^\s*#/.test(line)) {
      continue
    }

    const match = /^(\s*)(["']?)hermes\2\s*:(.*)$/.exec(line)

    if (match) {
      hermesIndent = match[1].length
      hermesIndex = index

      break
    }
  }

  if (hermesIndent === null) {
    return ''
  }

  // A flow-style value (`hermes: {}` / `hermes: null`) carries no child block.
  const hermesRest = stripTrailingComment(lines[hermesIndex].slice(lines[hermesIndex].indexOf(':') + 1)).trim()

  if (hermesRest !== '') {
    return ''
  }

  // The end of the `hermes:` block is the first later non-blank, non-comment
  // line whose indentation is not greater than the block's own.
  let endIndex = lines.length

  for (let index = hermesIndex + 1; index < lines.length; index += 1) {
    const line = lines[index]

    if (!line.trim() || /^\s*#/.test(line)) {
      continue
    }

    if (/^\s/.exec(line) === null) {
      endIndex = index

      break
    }
  }

  for (let index = hermesIndex + 1; index < endIndex; index += 1) {
    const line = lines[index]

    if (!line.trim() || /^\s*#/.test(line)) {
      continue
    }

    const indent = /^\s*/.exec(line)?.[0].length ?? 0

    // Only DIRECT children of `hermes:` are considered.
    if (indent !== hermesIndent + 2) {
      continue
    }

    const entry = /^([A-Za-z_][\w-]*)\s*:(.*)$/.exec(line.trim())

    if (!entry || entry[1] !== 'offlineDir') {
      continue
    }

    const value = stripTrailingComment(entry[2])

    if (/^[>|[]/.test(value.trim())) {
      // Block scalars / sequences are not a path.
      return ''
    }

    return unquoteScalar(value)
  }

  return ''
}

export function resolveOfflineDir({ hermesHome, env = process.env }: ResolveOfflineDirOptions): string {
  // 1. Environment variable takes precedence.
  const fromEnv = env.HERMES_OFFLINE_DIR ? String(env.HERMES_OFFLINE_DIR).trim() : ''

  if (fromEnv) {
    return fromEnv
  }

  // 2. Config file: `hermes.offlineDir` in <hermesHome>/config.yaml.
  try {
    const configPath = path.join(hermesHome, 'config.yaml')
    const text = fs.readFileSync(configPath, 'utf8')

    return extractOfflineDirFromConfig(text)
  } catch {
    // Config file missing or unreadable — no offline directory configured.
    return ''
  }
}
