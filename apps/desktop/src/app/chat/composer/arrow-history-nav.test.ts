import { describe, expect, it } from 'vitest'

import { type HistoryStep, historyStepAllowed } from './arrow-history-nav'

const STEPS: HistoryStep[] = ['backward', 'forward']
const DRAFTS = ['', '   ', 'a typed draft']

describe('historyStepAllowed', () => {
  it('refuses both arrows in every composer state when the preference is off', () => {
    for (const step of STEPS) {
      for (const draft of DRAFTS) {
        for (const browsing of [false, true]) {
          expect(historyStepAllowed(step, { browsing, draft, enabled: false })).toBe(false)
        }
      }
    }
  })

  it('forward only walks an already-open ring', () => {
    expect(historyStepAllowed('forward', { browsing: false, draft: '', enabled: true })).toBe(false)
    expect(historyStepAllowed('forward', { browsing: true, draft: 'whatever is shown', enabled: true })).toBe(true)
  })

  it('backward opens the ring from an empty or whitespace-only draft', () => {
    expect(historyStepAllowed('backward', { browsing: false, draft: '', enabled: true })).toBe(true)
    expect(historyStepAllowed('backward', { browsing: false, draft: '   ', enabled: true })).toBe(true)
  })

  it('backward refuses a typed draft the user did not recall', () => {
    expect(historyStepAllowed('backward', { browsing: false, draft: 'half-written thought', enabled: true })).toBe(false)
  })

  it('backward keeps stepping while browsing even though the composer shows text', () => {
    expect(historyStepAllowed('backward', { browsing: true, draft: 'a recalled entry', enabled: true })).toBe(true)
  })
})
