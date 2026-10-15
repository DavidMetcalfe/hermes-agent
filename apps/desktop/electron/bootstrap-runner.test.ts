import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  buildPinArgs,
  buildPosixPinArgs,
  cachedScriptPath,
  hasExistingGitCheckout,
  hasInstallerSiblings,
  installedAgentInstallScript,
  installRefForStamp,
  installSiblingNames,
  isPinnedCommit,
  offlineInstallerCandidates,
  resolveInstallScript,
  resolveMarkerPinnedCommit,
  runBootstrap
} from './bootstrap-runner'

const SCRIPT_NAME = process.platform === 'win32' ? 'install.ps1' : 'install.sh'
const ZERO_COMMIT = '0000000000000000000000000000000000000000'

function mkTmpHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-bootstrap-test-'))
}

test('runBootstrap bails immediately when the signal is already aborted', async () => {
  const controller = new AbortController()
  controller.abort()

  const events = []

  const result = await runBootstrap({
    installStamp: null,
    activeRoot: '/tmp/hermes-runner-test',
    sourceRepoRoot: null,
    hermesHome: '/tmp/hermes-runner-test',
    logRoot: '/tmp/hermes-runner-test',
    onEvent: ev => events.push(ev),
    abortSignal: controller.signal
  })

  // Cancelled before any install script is spawned.
  assert.deepEqual(result, { ok: false, cancelled: true })
  assert.ok(
    events.some(ev => ev.type === 'failed' && /cancelled/i.test(ev.error)),
    'should emit a cancelled failure event'
  )
})

test('installedAgentInstallScript resolves the installer in the agent checkout', () => {
  const home = mkTmpHome()

  try {
    assert.equal(installedAgentInstallScript(home), null, 'absent before the checkout exists')

    const scriptsDir = path.join(home, 'hermes-agent', 'scripts')
    fs.mkdirSync(scriptsDir, { recursive: true })
    const scriptPath = path.join(scriptsDir, SCRIPT_NAME)
    fs.writeFileSync(scriptPath, '#!/bin/sh\necho hi\n')

    assert.equal(installedAgentInstallScript(home), scriptPath)
    assert.equal(installedAgentInstallScript(null), null, 'null home -> null')
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('existing checkout detection requires git metadata', () => {
  const home = mkTmpHome()

  try {
    const activeRoot = path.join(home, 'hermes-agent')
    assert.equal(hasExistingGitCheckout(activeRoot), false)

    fs.mkdirSync(path.join(activeRoot, '.git'), { recursive: true })
    assert.equal(hasExistingGitCheckout(activeRoot), true)
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('fresh bootstrap args include the packaged commit pin', () => {
  const installStamp = { commit: 'a'.repeat(40), branch: 'main' }

  assert.deepEqual(buildPinArgs(installStamp), ['-Commit', installStamp.commit, '-Branch', 'main'])
  assert.deepEqual(
    buildPosixPinArgs({
      installStamp,
      activeRoot: '/tmp/hermes-agent',
      hermesHome: '/tmp/hermes'
    }),
    ['--dir', '/tmp/hermes-agent', '--hermes-home', '/tmp/hermes', '--branch', 'main', '--commit', installStamp.commit]
  )
})

test('existing-checkout bootstrap args keep branch but skip the packaged commit pin', () => {
  const installStamp = { commit: 'a'.repeat(40), branch: 'main' }

  assert.deepEqual(buildPinArgs(installStamp, { pinCommit: false }), ['-Branch', 'main'])
  assert.deepEqual(
    buildPosixPinArgs({
      installStamp,
      activeRoot: '/tmp/hermes-agent',
      hermesHome: '/tmp/hermes',
      pinCommit: false
    }),
    ['--dir', '/tmp/hermes-agent', '--hermes-home', '/tmp/hermes', '--branch', 'main']
  )
})

test('fallback install stamps use an unpinned branch ref', () => {
  const stamp = { commit: ZERO_COMMIT, branch: 'main' }

  assert.equal(isPinnedCommit(ZERO_COMMIT), false)
  assert.deepEqual(installRefForStamp(stamp), {
    ref: 'main',
    cacheKey: 'fallback-main',
    pinned: false
  })
  // Must NOT pass -Commit / --commit for the all-zero placeholder.
  assert.deepEqual(buildPinArgs(stamp), ['-Branch', 'main'])
  assert.deepEqual(
    buildPosixPinArgs({
      installStamp: stamp,
      activeRoot: '/tmp/hermes',
      hermesHome: '/tmp/home'
    }),
    ['--dir', '/tmp/hermes', '--hermes-home', '/tmp/home', '--branch', 'main']
  )
})

test('resolveMarkerPinnedCommit prefers real HEAD over fallback stamp zeros', () => {
  const realHead = 'c'.repeat(40)
  assert.equal(
    resolveMarkerPinnedCommit({ commit: ZERO_COMMIT, branch: 'main' }, '/tmp/checkout', {
      resolveHead: () => realHead
    }),
    realHead
  )
  assert.equal(
    resolveMarkerPinnedCommit({ commit: 'd'.repeat(40), branch: 'main' }, '/tmp/checkout', {
      resolveHead: () => realHead
    }),
    'd'.repeat(40),
    'packaged real pin wins over checkout HEAD'
  )
  assert.equal(
    resolveMarkerPinnedCommit({ commit: ZERO_COMMIT, branch: 'main' }, '/tmp/missing', {
      resolveHead: () => null
    }),
    null
  )
})

test('resolveInstallScript downloads fallback stamps by branch instead of zero commit', async () => {
  const home = mkTmpHome()

  try {
    const logs = []
    const refs = []

    const result = await resolveInstallScript({
      installStamp: { commit: ZERO_COMMIT, branch: 'main' },
      sourceRepoRoot: null,
      hermesHome: home,
      emit: ev => logs.push(ev),
      _download: async (ref, destPath) => {
        refs.push(ref)
        fs.mkdirSync(path.dirname(destPath), { recursive: true })
        fs.writeFileSync(destPath, '#!/bin/sh\necho fallback branch\n')

        return destPath
      }
    })

    assert.deepEqual(refs, ['main'])
    assert.equal(result.source, 'download')
    assert.equal(result.commit, null)
    assert.equal(result.path, cachedScriptPath(home, 'fallback-main'))
    assert.ok(
      logs.some(ev => /fallback, unpinned/.test(ev.line || '')),
      'emits an unpinned fallback log line'
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript prefers a cached script without touching the network', async () => {
  const home = mkTmpHome()

  try {
    const commit = 'a'.repeat(40)
    const cached = cachedScriptPath(home, commit)
    fs.mkdirSync(path.dirname(cached), { recursive: true })
    fs.writeFileSync(cached, '#!/bin/sh\necho cached\n')

    const logs = []

    const result = await resolveInstallScript({
      installStamp: { commit },
      sourceRepoRoot: null,
      hermesHome: home,
      emit: ev => logs.push(ev)
    })

    assert.equal(result.source, 'cache')
    assert.equal(result.path, cached)
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript falls back to the installed agent checkout on a 404', async () => {
  const home = mkTmpHome()

  try {
    const commit = 'a'.repeat(40)
    // Seed the installed agent checkout so the fallback has something to resolve.
    const scriptsDir = path.join(home, 'hermes-agent', 'scripts')
    fs.mkdirSync(scriptsDir, { recursive: true })
    const installed = path.join(scriptsDir, SCRIPT_NAME)
    fs.writeFileSync(installed, '#!/bin/sh\necho fallback\n')

    const logs = []

    const result = await resolveInstallScript({
      installStamp: { commit },
      sourceRepoRoot: null,
      hermesHome: home,
      emit: ev => logs.push(ev),
      // Simulate GitHub returning a 404 for the pinned commit.
      _download: async () => {
        throw new Error('Failed to download install.sh: HTTP 404')
      }
    })

    assert.equal(result.source, 'installed-agent')
    // It should have copied the installer into the bootstrap cache.
    assert.equal(result.path, cachedScriptPath(home, commit))
    assert.ok(fs.existsSync(result.path), 'fallback script copied into cache')
    assert.ok(
      logs.some(ev => /falling back to installed agent/.test(ev.line || '')),
      'emits a fallback log line'
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript rethrows when the 404 fallback is unavailable', async () => {
  const home = mkTmpHome()

  try {
    const commit = 'a'.repeat(40)
    // No installed agent checkout seeded -> nothing to fall back to.
    await assert.rejects(
      resolveInstallScript({
        installStamp: { commit },
        sourceRepoRoot: null,
        hermesHome: home,
        emit: () => {},
        _download: async () => {
          throw new Error('Failed to download install.sh: HTTP 404')
        }
      }),
      /HTTP 404|Failed to download/
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

// ---------------------------------------------------------------------------
// Offline installer delivery
//
// An air-gapped first install has no network and no previously installed agent
// checkout, so neither the downloader nor the installed-agent fallback can
// supply the installer. The offline media must be consulted BEFORE the network,
// and it must deliver the installer's sibling module too -- install.ps1
// dot-sources install-offline.ps1 via $PSScriptRoot, so staging only the main
// script yields an installer that dies at the repository stage.
// ---------------------------------------------------------------------------

test('offlineInstallerCandidates covers media root and media scripts/ layout', () => {
  assert.deepEqual(offlineInstallerCandidates('/media/bundle'), [
    '/media/bundle',
    path.join('/media/bundle', 'scripts')
  ])
  assert.deepEqual(offlineInstallerCandidates(''), [], 'no offline dir -> no candidates')
  assert.deepEqual(offlineInstallerCandidates(null), [], 'null offline dir -> no candidates')
})

test('installSiblingNames requires the offline module on PowerShell only', () => {
  const siblings = installSiblingNames()

  if (process.platform === 'win32') {
    assert.deepEqual(siblings, ['install-offline.ps1'])
  } else {
    assert.deepEqual(siblings, [], 'install.sh sources no siblings')
  }
})

test('hasInstallerSiblings requires every sibling next to the installer', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-siblings-'))

  try {
    const main = path.join(dir, SCRIPT_NAME)
    fs.writeFileSync(main, '#!/bin/sh\necho installer\n')

    const siblings = installSiblingNames()

    if (siblings.length === 0) {
      // install.sh needs no siblings; the check is satisfied by the main file.
      assert.equal(hasInstallerSiblings(main), true)
    } else {
      assert.equal(hasInstallerSiblings(main), false, 'missing sibling must fail')
      for (const name of siblings) {
        fs.writeFileSync(path.join(dir, name), '#!/bin/sh\necho module\n')
      }
      assert.equal(hasInstallerSiblings(main), true, 'siblings present must pass')
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

test('resolveInstallScript resolves from offline media without downloading', async () => {
  const home = mkTmpHome()
  const media = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-offline-media-'))

  try {
    const commit = 'c'.repeat(40)
    fs.writeFileSync(path.join(media, SCRIPT_NAME), '#!/bin/sh\necho offline\n')
    for (const name of installSiblingNames()) {
      fs.writeFileSync(path.join(media, name), '# module\n')
    }

    let downloadCalls = 0
    const logs = []

    const result = await resolveInstallScript({
      installStamp: { commit },
      sourceRepoRoot: null,
      hermesHome: home,
      emit: ev => logs.push(ev),
      offlineDir: media,
      _download: async () => {
        downloadCalls += 1

        throw new Error('downloader must not be reached in offline mode')
      }
    })

    assert.equal(result.source, 'offline')
    assert.equal(downloadCalls, 0, 'offline resolution must not touch the network')
    assert.ok(fs.existsSync(result.path), 'staged installer exists')
    assert.ok(
      logs.some(ev => /using offline/.test(ev.line || '')),
      'emits an offline log line'
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
    fs.rmSync(media, { recursive: true, force: true })
  }
})

test('resolveInstallScript stages siblings next to the cached installer', async () => {
  const home = mkTmpHome()
  const media = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-offline-media-'))

  try {
    const commit = 'd'.repeat(40)
    fs.writeFileSync(path.join(media, SCRIPT_NAME), '#!/bin/sh\necho offline\n')
    for (const name of installSiblingNames()) {
      fs.writeFileSync(path.join(media, name), '# module\n')
    }

    const result = await resolveInstallScript({
      installStamp: { commit },
      sourceRepoRoot: null,
      hermesHome: home,
      emit: () => {},
      offlineDir: media
    })

    assert.equal(result.path, cachedScriptPath(home, commit))
    assert.equal(
      hasInstallerSiblings(result.path),
      true,
      'a cached installer without its sibling cannot be used'
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
    fs.rmSync(media, { recursive: true, force: true })
  }
})

test('resolveInstallScript treats media without the sibling module as incomplete on PowerShell', async () => {
  const home = mkTmpHome()
  const media = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-offline-media-'))

  try {
    const commit = 'e'.repeat(40)
    // Main script present, sibling absent -> media is incomplete on PowerShell.
    fs.writeFileSync(path.join(media, SCRIPT_NAME), '#!/bin/sh\necho partial\n')

    let downloadCalls = 0
    const downloader = async () => {
      downloadCalls += 1

      throw new Error('network denied')
    }

    if (installSiblingNames().length === 0) {
      // install.sh has no siblings, so a lone install.sh IS complete media.
      const result = await resolveInstallScript({
        installStamp: { commit },
        sourceRepoRoot: null,
        hermesHome: home,
        emit: () => {},
        offlineDir: media,
        _download: downloader
      })

      assert.equal(result.source, 'offline')
      assert.equal(downloadCalls, 0)
    } else {
      // PowerShell: the missing install-offline.ps1 must send us to the network.
      await assert.rejects(
        resolveInstallScript({
          installStamp: { commit },
          sourceRepoRoot: null,
          hermesHome: home,
          emit: () => {},
          offlineDir: media,
          _download: downloader
        }),
        /network denied/
      )
      assert.equal(downloadCalls, 1)
    }
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
    fs.rmSync(media, { recursive: true, force: true })
  }
})

test('resolveInstallScript re-downloads when the cache lacks the sibling module', async () => {
  const home = mkTmpHome()

  try {
    const commit = 'f'.repeat(40)
    const cached = cachedScriptPath(home, commit)
    fs.mkdirSync(path.dirname(cached), { recursive: true })
    // Simulate a cache entry written by an older app version: main script only.
    fs.writeFileSync(cached, '#!/bin/sh\necho stale\n')

    let downloadCalls = 0

    const result = await resolveInstallScript({
      installStamp: { commit },
      sourceRepoRoot: null,
      hermesHome: home,
      emit: () => {},
      _download: async (_ref, destPath) => {
        downloadCalls += 1
        fs.writeFileSync(destPath, '#!/bin/sh\necho fresh\n')

        return destPath
      },
      _downloadSibling: async (_ref, destPath) => {
        fs.writeFileSync(destPath, '# module\n')

        return destPath
      }
    })

    if (installSiblingNames().length === 0) {
      assert.equal(result.source, 'cache')
      assert.equal(downloadCalls, 0)
    } else {
      assert.equal(result.source, 'download', 'stale cache without sibling must be refreshed')
      assert.equal(downloadCalls, 1)
      assert.equal(hasInstallerSiblings(result.path), true)
    }
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})
