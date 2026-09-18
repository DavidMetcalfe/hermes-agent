import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { capabilityScoped, hermesApi, type ProfileScope } from '@/api/client'
import { type ChatMessage, toChatMessages } from '@/lib/chat-messages'
import type { SessionMessagesResponse } from '@/types/hermes'

export const HISTORY_WINDOW_LIMIT = 120

/** Mirrors the reveal path's bound: a bridge that never answers must not hold the
 *  page's only in-flight slot, or the affordance would never come back. */
const OLDER_READ_TIMEOUT_MS = 15_000

/** `newer` is the jump page at a prompt; `older` reads backwards from a row. */
export type HistoryDirection = 'newer' | 'older'

/** The around route is intentionally isolated from tail/backfill bookkeeping. */
export interface HistoryWindowResponse extends SessionMessagesResponse {
  pagination: NonNullable<SessionMessagesResponse['pagination']> & {
    has_older: boolean
    has_newer: boolean
  }
}

interface HistoryPage {
  messages: ChatMessage[]
  olderAvailable: boolean
  newerAvailable: boolean
}

/** Older rows join the display page only. The around read excludes its own
 *  anchor, so consecutive windows meet without overlap. */
function withOlderPage(page: HistoryPage, older: HistoryPage): HistoryPage {
  return {
    messages: [...older.messages, ...page.messages],
    olderAvailable: older.olderAvailable,
    newerAvailable: page.newerAvailable
  }
}

export async function fetchHistoryWindow(
  storedId: string,
  rowId: number,
  scope: ProfileScope,
  signal: AbortSignal,
  direction: HistoryDirection = 'newer'
): Promise<HistoryPage> {
  signal.throwIfAborted()
  const route = capabilityScoped(scope)
  const query = new URLSearchParams({ row_id: String(rowId), limit: String(HISTORY_WINDOW_LIMIT) })

  if (direction === 'older') {query.set('direction', 'older')}

  if (route.profile) {query.set('profile', route.profile)}

  // The Electron REST bridge cannot transfer AbortSignal over IPC. Cancellation
  // below releases the caller immediately and fences the eventual bounded read;
  // it does not pretend to cancel backend I/O or fall back to a full transcript.
  const response = await hermesApi<HistoryWindowResponse>({
    ...route,
    ...(typeof scope === 'object' && scope?.connectionId === 'local' ? { connectionId: 'local' } : {}),
    method: 'GET',
    path: `/api/sessions/${encodeURIComponent(storedId)}/messages/around?${query}`
  })

  signal.throwIfAborted()

  if (!Array.isArray(response.messages) || response.messages.length > HISTORY_WINDOW_LIMIT) {
    throw new Error('History response exceeds the bounded page size.')
  }

  return {
    messages: toChatMessages(response.messages),
    olderAvailable: response.pagination.has_older === true,
    newerAvailable: response.pagination.has_newer === true
  }
}

interface HistoryWindowOptions {
  /** Include runtime, durable id, owner connection/profile, and suppression. */
  scopeKey: string
  storedId: string | null
  scope: ProfileScope
  isCurrent: () => boolean
}

/** An isolated display page, paged in both directions, never merged into the
 *  live message store. */
export function useHistoryWindow({ scopeKey, storedId, scope, isCurrent }: HistoryWindowOptions) {
  const lifetime = useMemo(() => ({ scopeKey }), [scopeKey])
  const latest = useRef({ lifetime, storedId, scope, isCurrent })
  latest.current = { lifetime, storedId, scope, isCurrent }
  const pending = useRef<AbortController | null>(null)
  const [selection, setSelection] = useState<{ lifetime: object; page: HistoryPage } | null>(null)
  const page = selection?.lifetime === lifetime ? selection.page : null
  const active = useRef(selection)
  active.current = selection

  const cancel = useCallback(() => {
    pending.current?.abort()
    pending.current = null
  }, [])

  useEffect(() => cancel, [cancel, lifetime])

  const returnToLatest = useCallback(() => {
    cancel()
    setSelection(null)
  }, [cancel])

  const revealRow = useCallback(async (rowId: number, signal: AbortSignal): Promise<string | null> => {
    cancel()

    if (signal.aborted || !Number.isSafeInteger(rowId) || rowId <= 0) {return null}
    const captured = latest.current

    if (!captured.storedId || !captured.isCurrent()) {return null}
    const controller = new AbortController()
    pending.current = controller
    const abort = () => controller.abort()
    signal.addEventListener('abort', abort, { once: true })
    let release!: () => void

    const aborted = new Promise<null>(resolve => {
      release = () => resolve(null)
      controller.signal.addEventListener('abort', release, { once: true })
    })

    try {
      const next = await Promise.race([
        fetchHistoryWindow(captured.storedId, rowId, captured.scope, controller.signal),
        aborted
      ])

      if (!next || controller.signal.aborted || latest.current.lifetime !== captured.lifetime || !captured.isCurrent()) {
        return null
      }

      const target = next.messages.find(message => message.rowId === rowId)

      if (!target) {return null}
      setSelection({ lifetime: captured.lifetime, page: next })

      return target.id
    } catch {
      // Missing/older backend, unreadable row, and failed reads preserve the
      // current page. The caller reports failure and can retry explicitly.
      return null
    } finally {
      signal.removeEventListener('abort', abort)
      controller.signal.removeEventListener('abort', release)

      if (pending.current === controller) {pending.current = null}
    }
  }, [cancel])

  /** Continue the display page backwards. The live store is never touched, so
   *  a failed read leaves the selected page exactly as it was. */
  const prependOlder = useCallback(async (beforePrepend?: () => void): Promise<boolean> => {
    const captured = latest.current
    const selected = active.current

    if (!captured.storedId || !captured.isCurrent()) {return false}

    if (!selected || selected.lifetime !== captured.lifetime) {return false}
    const anchor = selected.page.messages[0]?.rowId

    if (anchor === undefined || !selected.page.olderAvailable) {return false}

    // Single-flight: viewport-top auto-paging can ask again while a read is out,
    // and aborting to restart it would starve the page — worse, the shared
    // controller is also what a rail jump is waiting on.
    if (pending.current) {return false}
    const controller = new AbortController()
    pending.current = controller
    // Mirrors the reveal path's bound: a bridge that never answers must not hold
    // this page's only in-flight slot, or the affordance would never come back.
    const timeout = window.setTimeout(() => controller.abort(), OLDER_READ_TIMEOUT_MS)
    let release!: () => void

    const aborted = new Promise<null>(resolve => {
      release = () => resolve(null)
      controller.signal.addEventListener('abort', release, { once: true })
    })

    try {
      const older = await Promise.race([
        fetchHistoryWindow(captured.storedId, anchor, captured.scope, controller.signal, 'older'),
        aborted
      ])

      if (!older || controller.signal.aborted || latest.current.lifetime !== captured.lifetime || !captured.isCurrent()) {
        return false
      }

      const current = active.current

      if (!current || current.lifetime !== captured.lifetime) {return false}
      const grew = older.messages.length > 0
      const retired = older.olderAvailable !== current.page.olderAvailable

      // Nothing older to add and nothing to retire: keep the page object as-is.
      if (!grew && !retired) {return false}

      if (grew) {
        // Network latency is not scroll intent: capture the reader at arrival,
        // immediately before the page grows.
        beforePrepend?.()
      }

      setSelection({
        lifetime: captured.lifetime,
        page: grew
          ? withOlderPage(current.page, older)
          : { ...current.page, olderAvailable: older.olderAvailable }
      })

      return grew
    } catch {
      // Unreadable older page: keep the page we have, and the affordance with it.
      return false
    } finally {
      window.clearTimeout(timeout)
      controller.signal.removeEventListener('abort', release)

      if (pending.current === controller) {pending.current = null}
    }
  }, [])

  return { page, revealRow, returnToLatest, prependOlder }
}
