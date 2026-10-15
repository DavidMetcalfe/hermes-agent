import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import { extractOfflineDirFromConfig, resolveOfflineDir } from './offline-dir-resolver'

// Behavioral coverage for the offline-media directory resolution. These cases
// are regression guards for the "flat regex scan" bug: that implementation read
// `offlineDir:` anywhere after `hermes:`, including comments and unrelated
// mappings, so a config comment could change which media the installer used.

const tempDirs: string[] = []

function makeHermesHome(configText: string | null): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-offline-dir-'))

  tempDirs.push(dir)

  if (configText !== null) {
    fs.writeFileSync(path.join(dir, 'config.yaml'), configText)
  }

  return dir
}

afterEach(() => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop()

    if (dir) {
      fs.rmSync(dir, { force: true, recursive: true })
    }
  }
})

describe('extractOfflineDirFromConfig', () => {
  it('reads a plain scalar setting', () => {
    expect(extractOfflineDirFromConfig('hermes:\n  offlineDir: E:/bundle\n')).toBe('E:/bundle')
  })

  it('reads a double-quoted setting', () => {
    expect(extractOfflineDirFromConfig('hermes:\n  offlineDir: "E:/bundle"\n')).toBe('E:/bundle')
  })

  it('reads a single-quoted setting', () => {
    expect(extractOfflineDirFromConfig("hermes:\n  offlineDir: 'E:/bundle'\n")).toBe('E:/bundle')
  })

  it('returns the value without a trailing comment', () => {
    expect(extractOfflineDirFromConfig('hermes:\n  offlineDir: E:/bundle # USB media\n')).toBe('E:/bundle')
  })

  it('keeps a hash that is part of the path, not a comment', () => {
    expect(extractOfflineDirFromConfig('hermes:\n  offlineDir: E:/a#b\n')).toBe('E:/a#b')
  })

  it('ignores a commented-out setting', () => {
    expect(extractOfflineDirFromConfig('hermes:\n  # offlineDir: E:/disabled\n  enabled: true\n')).toBe('')
  })

  it('ignores an offlineDir in a different top-level block', () => {
    expect(extractOfflineDirFromConfig('hermes:\n  enabled: true\nother:\n  offlineDir: E:/other\n')).toBe('')
  })

  it('ignores an offlineDir in a different block that appears BEFORE hermes', () => {
    expect(extractOfflineDirFromConfig('other:\n  offlineDir: E:/other\nhermes:\n  enabled: true\n')).toBe('')
  })

  it('ignores an offlineDir nested deeper under hermes', () => {
    expect(extractOfflineDirFromConfig('hermes:\n  server:\n    offlineDir: E:/nested\n')).toBe('')
  })

  it('ignores an offlineDir after the hermes block has ended', () => {
    expect(extractOfflineDirFromConfig('hermes:\n  enabled: true\n\nterminal:\n  offlineDir: E:/other\n')).toBe('')
  })

  it('returns empty when hermes is absent', () => {
    expect(extractOfflineDirFromConfig('terminal:\n  cwd: /tmp\n')).toBe('')
  })

  it('returns empty for an empty scalar', () => {
    expect(extractOfflineDirFromConfig('hermes:\n  offlineDir:\n')).toBe('')
  })

  it('returns empty for a flow-style hermes mapping', () => {
    expect(extractOfflineDirFromConfig('hermes: {}\n')).toBe('')
  })

  it('reads the key when other keys surround it in the same block', () => {
    const text = 'hermes:\n  enabled: true\n  offlineDir: E:/bundle\n  other: 2\n'

    expect(extractOfflineDirFromConfig(text)).toBe('E:/bundle')
  })

  it('is not confused by a leading document marker or comments', () => {
    const text = '# hermes config\n---\nhermes:\n  # offlineDir: E:/commented\n  offlineDir: E:/real\n'

    expect(extractOfflineDirFromConfig(text)).toBe('E:/real')
  })
})

describe('resolveOfflineDir', () => {
  it('prefers HERMES_OFFLINE_DIR over the config setting', () => {
    const home = makeHermesHome('hermes:\n  offlineDir: E:/from-config\n')
    const resolved = resolveOfflineDir({ env: { HERMES_OFFLINE_DIR: 'E:/from-env' } as NodeJS.ProcessEnv, hermesHome: home })

    expect(resolved).toBe('E:/from-env')
  })

  it('trims the environment override', () => {
    const home = makeHermesHome(null)
    const resolved = resolveOfflineDir({ env: { HERMES_OFFLINE_DIR: '  E:/spaced  ' } as NodeJS.ProcessEnv, hermesHome: home })

    expect(resolved).toBe('E:/spaced')
  })

  it('falls back to the config setting when the env var is unset', () => {
    const home = makeHermesHome('hermes:\n  offlineDir: E:/from-config\n')
    const resolved = resolveOfflineDir({ env: {} as NodeJS.ProcessEnv, hermesHome: home })

    expect(resolved).toBe('E:/from-config')
  })

  it('returns empty when the config file is missing', () => {
    const home = makeHermesHome(null)

    expect(resolveOfflineDir({ env: {} as NodeJS.ProcessEnv, hermesHome: home })).toBe('')
  })

  it('returns empty rather than throwing on malformed content', () => {
    const home = makeHermesHome('hermes:\n  offlineDir: [unterminated\n')
    const resolved = resolveOfflineDir({ env: {} as NodeJS.ProcessEnv, hermesHome: home })

    expect(typeof resolved).toBe('string')
  })

  it('does not pick up offlineDir from a sibling block', () => {
    const home = makeHermesHome('other:\n  offlineDir: E:/other\nhermes:\n  enabled: true\n')

    expect(resolveOfflineDir({ env: {} as NodeJS.ProcessEnv, hermesHome: home })).toBe('')
  })
})
