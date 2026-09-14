import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import type { SessionInfo, SessionSearchResult } from '@/hermes'
import { $sessions } from '@/store/session'

import { CommandCenterView } from './index'

// #51694: the Command Center's Sessions search filtered only the sidebar's
// loaded page (`$sessions`, 50 rows, archived excluded) with a substring test
// on title+id — so it never did full-text search, never showed archived
// sessions, and never found anything older than the page. It must call the
// server FTS endpoint (searchSessions) on a debounce and merge the hits behind
// the instant client-side matches, exactly like the sidebar does.

const mocks = vi.hoisted(() => ({
  searchSessions: vi.fn()
}))

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  getActionStatus: vi.fn(() => Promise.resolve({ running: false })),
  getLogs: vi.fn(() => Promise.resolve({ lines: [] })),
  getStatus: vi.fn(() => Promise.resolve({})),
  getUsageAnalytics: vi.fn(() => Promise.resolve({})),
  restartGateway: vi.fn(),
  searchSessions: mocks.searchSessions,
  updateHermes: vi.fn()
}))
vi.mock('@/lib/session-export', () => ({ exportSession: vi.fn() }))
vi.mock('./maintenance', () => ({ MaintenancePanel: () => null }))

afterEach(cleanup)

const SESSION = {
  archived: false,
  ended_at: null,
  id: 'sess-local-1',
  input_tokens: 0,
  is_active: false,
  last_active: 1_756_600_000,
  message_count: 3,
  model: null,
  output_tokens: 0,
  started_at: 1_756_500_000,
  title: 'Precious conversation'
} as unknown as SessionInfo

function serverHit(overrides: Partial<SessionSearchResult> & { session_id: string }): SessionSearchResult {
  return {
    archived: false,
    last_active: 1_756_400_000,
    model: null,
    preview: null,
    role: null,
    snippet: 'a snippet',
    session_started: 1_756_300_000,
    source: null,
    started_at: 1_756_300_000,
    title: null,
    ...overrides
  }
}

function renderCommandCenter() {
  return render(
    <MemoryRouter>
      <CommandCenterView
        initialSection="sessions"
        onClose={() => {}}
        onDeleteSession={vi.fn(() => Promise.resolve())}
        onOpenSession={() => {}}
      />
    </MemoryRouter>
  )
}

async function typeSearch(value: string) {
  fireEvent.change(screen.getByPlaceholderText('Search sessions, views, and actions'), {
    target: { value }
  })
}

describe('Command Center sessions server search (#51694)', () => {
  beforeEach(() => {
    mocks.searchSessions.mockReset()
    $sessions.set([SESSION])
  })

  it('calls the server search endpoint and lists a server-only hit', async () => {
    mocks.searchSessions.mockResolvedValue({
      results: [
        serverHit({
          archived: false,
          session_id: 'server-only-1',
          snippet: '...mentions >>>zzqmarker<<< here...',
          title: 'Frozen tundra notes'
        })
      ]
    })
    renderCommandCenter()

    await typeSearch('zzqmarker')

    await waitFor(() => expect(mocks.searchSessions).toHaveBeenCalledWith('zzqmarker'), { timeout: 3000 })
    expect(await screen.findByText('Frozen tundra notes', {}, { timeout: 3000 })).toBeTruthy()
  })

  it('marks an archived server hit with the archived badge', async () => {
    mocks.searchSessions.mockResolvedValue({
      results: [
        serverHit({
          archived: true,
          session_id: 'server-archived-1',
          snippet: 'buried >>>zzqfrozen<<< conversation',
          title: 'Frozen archive thread'
        })
      ]
    })
    renderCommandCenter()

    await typeSearch('zzqfrozen')

    await waitFor(() => expect(mocks.searchSessions).toHaveBeenCalledWith('zzqfrozen'), { timeout: 3000 })
    // The archived marker is a visible text badge, not a tooltip on a glyph.
    expect(await screen.findByText('Frozen archive thread', {}, { timeout: 3000 })).toBeTruthy()
    expect(await screen.findByText('Archived', {}, { timeout: 3000 })).toBeTruthy()
  })

  it('strips the backend FTS highlight markers from rendered text', async () => {
    mocks.searchSessions.mockResolvedValue({
      results: [
        serverHit({
          session_id: 'server-snippet-1',
          snippet: 'mentions >>>zzqalpha<<< here',
          title: null
        })
      ]
    })
    renderCommandCenter()

    await typeSearch('zzqalpha')

    await waitFor(() => expect(mocks.searchSessions).toHaveBeenCalledWith('zzqalpha'), { timeout: 3000 })
    // title is null → the row falls back to the (marker-stripped) snippet.
    expect(await screen.findByText('mentions zzqalpha here', {}, { timeout: 3000 })).toBeTruthy()
    expect(screen.queryByText(/>>>|<<</)).toBeNull()
  })

  it('does not call the server with no query and still lists loaded sessions', async () => {
    renderCommandCenter()

    expect(await screen.findByText('Precious conversation', {}, { timeout: 3000 })).toBeTruthy()
    expect(mocks.searchSessions).not.toHaveBeenCalled()
  })
})
