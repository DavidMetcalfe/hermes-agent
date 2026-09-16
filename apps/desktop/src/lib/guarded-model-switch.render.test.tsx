import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ConfirmHost } from '@/components/confirm-host'

import { type GuardedModelSwitchResult, surfaceModelSwitchConfirm } from './guarded-model-switch'

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

afterEach(cleanup)

/** The gateway's shape: blank lines separate blocks, single newlines wrap. */
const GUARD_MESSAGE =
  'This session holds ~206,172 tokens of context.\nSwitching re-reads it.\n\nContext window shrinks (1,000,000 → 256,000).'

type SwitchResult = GuardedModelSwitchResult & { value: string }

function ask() {
  const requestConfirmed = vi.fn(async (): Promise<SwitchResult> => ({ value: 'perplexity/sonar-deep-research' }))

  const answer = surfaceModelSwitchConfirm({
    confirmMessage: GUARD_MESSAGE,
    failureMessage: 'Model switch failed',
    model: 'perplexity/sonar-deep-research',
    requestConfirmed
  })

  return { answer, requestConfirmed }
}

// The applier outside its unit test: the real `confirm()` atom, the real
// ConfirmHost mount, the real ConfirmDialog and the real i18n copy — so the
// labels a user actually sees are asserted, not a mocked request object.
describe('surfaceModelSwitchConfirm through the shell dialog', () => {
  it('offers a decline that keeps the current model', async () => {
    render(<ConfirmHost />)

    const { answer, requestConfirmed } = ask()

    expect(await screen.findByText('Switch to perplexity/sonar-deep-research?')).toBeTruthy()
    expect(screen.getByText(/Context window shrinks/)).toBeTruthy()

    fireEvent.click(await screen.findByRole('button', { name: 'Keep current model' }))

    await expect(answer).resolves.toBe(false)
    expect(requestConfirmed).not.toHaveBeenCalled()
  })

  it('declines on Escape, the way every other dismissable overlay closes', async () => {
    render(<ConfirmHost />)

    const { answer, requestConfirmed } = ask()

    await screen.findByText('Switch to perplexity/sonar-deep-research?')

    // eslint-disable-next-line no-restricted-globals -- asserting real focus requires the live document
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })

    await expect(answer).resolves.toBe(false)
    expect(requestConfirmed).not.toHaveBeenCalled()
  })

  it('resends only once the switch is confirmed', async () => {
    render(<ConfirmHost />)

    const { answer, requestConfirmed } = ask()

    fireEvent.click(await screen.findByRole('button', { name: 'Switch anyway' }))

    await expect(answer).resolves.toBe(true)
    expect(requestConfirmed).toHaveBeenCalledTimes(1)
  })

  it('falls back to a generic title when the caller cannot name the model', async () => {
    render(<ConfirmHost />)

    const requestConfirmed = vi.fn(async (): Promise<SwitchResult> => ({ value: 'sonar' }))

    const answer = surfaceModelSwitchConfirm({
      confirmMessage: GUARD_MESSAGE,
      failureMessage: 'Model switch failed',
      requestConfirmed
    })

    expect(await screen.findByText('Switch models?')).toBeTruthy()

    fireEvent.click(await screen.findByRole('button', { name: 'Keep current model' }))

    await expect(answer).resolves.toBe(false)
    expect(requestConfirmed).not.toHaveBeenCalled()
  })
})
