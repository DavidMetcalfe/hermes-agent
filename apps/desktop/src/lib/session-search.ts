import { normalize } from '@/lib/text'
import type { SessionInfo, SessionSearchResult } from '@/types/hermes'

import { sessionTitle } from './chat-runtime'
import { sessionSourceSearchTerms } from './session-source'

export function sessionMatchesSearch(session: SessionInfo, query: string): boolean {
  const needle = normalize(query)

  if (!needle) {
    return true
  }

  return [
    session.id,
    session._lineage_root_id ?? '',
    sessionTitle(session),
    session.preview ?? '',
    session.cwd ?? '',
    session.git_branch ?? '',
    ...sessionSourceSearchTerms(session.source)
  ].some(value => value.toLowerCase().includes(needle))
}

// The backend's FTS layer wraps matched terms in literal '>>>' / '<<<'
// highlight markers (sqlite snippet() delimiters — see hermes_state_search.py).
// Rows render as plain text, so the markers must be stripped or a search for
// "foo" paints ">>>foo<<<".
export function stripFtsMarkers(snippet: string): string {
  return snippet.replaceAll('>>>', '').replaceAll('<<<', '')
}

/** Synthesize a SessionInfo for a server hit that is not in the loaded store,
 *  so it renders in the same row component (resume works by id; the snippet
 *  stands in for the preview). `session_id` is the live compression tip — the
 *  documented resume identity — so `id` must be it, not the lineage root. */
export function searchResultToSession(result: SessionSearchResult): SessionInfo {
  const ts = result.started_at ?? result.session_started ?? Date.now() / 1000

  return {
    archived: result.archived ?? false,
    cwd: null,
    ended_at: null,
    id: result.session_id,
    _lineage_root_id: result.lineage_root ?? null,
    input_tokens: 0,
    is_active: false,
    last_active: result.last_active ?? ts,
    message_count: 0,
    model: result.model ?? null,
    output_tokens: 0,
    preview: stripFtsMarkers(result.snippet ?? '').trim() || result.preview || null,
    source: result.source ?? null,
    started_at: ts,
    title: result.title ?? null,
    tool_call_count: 0
  }
}

/** Merge instant client-side matches with server FTS hits, deduped by session
 *  id *and* compression lineage: local rows win, then each server hit whose
 *  lineage is not already present becomes the already-loaded row when we have
 *  one (`loadedById`, looked up by tip then lineage root), else a synthesized
 *  row. Output order is local-then-server. */
export function mergeSessionSearchResults(
  localMatches: readonly SessionInfo[],
  serverMatches: readonly SessionSearchResult[],
  loadedById?: ReadonlyMap<string, SessionInfo>
): SessionInfo[] {
  const out = new Map<string, SessionInfo>()

  for (const session of localMatches) {
    out.set(session.id, session)
  }

  for (const match of serverMatches) {
    if (!match.session_id || out.has(match.session_id)) {
      continue
    }

    // One row per conversation: the store may hold the tip while the backend
    // matched the lineage root (or vice versa), so a hit for a conversation we
    // already listed must not become a second row.
    if (match.lineage_root && out.has(match.lineage_root)) {
      continue
    }

    const loaded =
      loadedById?.get(match.session_id) ?? (match.lineage_root ? loadedById?.get(match.lineage_root) : undefined)

    out.set(match.session_id, loaded ?? searchResultToSession(match))
  }

  return [...out.values()]
}
