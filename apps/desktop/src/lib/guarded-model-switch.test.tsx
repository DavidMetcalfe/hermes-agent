import { beforeEach, describe, expect, it, vi } from 'vitest'

import { type GuardedModelSwitchResult, surfaceModelSwitchConfirm } from './guarded-model-switch'

const confirmMock = vi.fn()
const notify = vi.fn()
const notifyError = vi.fn()

vi.mock('@/store/confirm', () => ({
  confirm: (...args: Parameters<typeof confirmMock>) => confirmMock(...args)
}))

vi.mock('@/store/notifications', () => ({
  notify: (...args: Parameters<typeof notify>) => notify(...args),
  notifyError: (...args: Parameters<typeof notifyError>) => notifyError(...args)
}))

type SwitchResult = GuardedModelSwitchResult & { value: string }

function buildOptions(overrides: Record<string, unknown> = {}) {
  return {
    failureMessage: 'Model switch failed',
    model: 'perplexity/sonar-deep-research',
    requestConfirmed: vi.fn(async (): Promise<SwitchResult> => ({ value: 'perplexity/sonar-deep-research' })),
    ...overrides
  }
}

/** A guard message shaped like the gateway's: blank lines separate blocks. */
const GUARD_MESSAGE = 'This session holds ~206,172 tokens of context.\nSwitching re-reads it.\n\nContext window shrinks (1,000,000 → 256,000).'

beforeEach(() => {
  confirmMock.mockReset()
  notify.mockReset()
  notifyError.mockReset()
})

describe('surfaceModelSwitchConfirm', () => {
  it('asks through the confirm dialog and names both choices', async () => {
    confirmMock.mockResolvedValueOnce(false)
    const options = buildOptions()

    await expect(surfaceModelSwitchConfirm(options)).resolves.toBe(false)

    expect(confirmMock).toHaveBeenCalledWith({
      cancelLabel: 'Keep current model',
      confirmLabel: 'Switch anyway',
      description: expect.any(Array),
      destructive: true,
      title: 'Switch to perplexity/sonar-deep-research?'
    })
    // Nothing is applied, so a decline has nothing to repaint or roll back.
    expect(options.requestConfirmed).not.toHaveBeenCalled()
    expect(notify).not.toHaveBeenCalled()
  })

  it('titles a switch whose model the caller cannot name', async () => {
    confirmMock.mockResolvedValueOnce(false)

    await surfaceModelSwitchConfirm(buildOptions({ model: undefined }))

    expect(confirmMock).toHaveBeenCalledWith(expect.objectContaining({ title: 'Switch models?' }))
  })

  it('renders the gateway warning as paragraphs, keeping its line breaks', async () => {
    confirmMock.mockResolvedValueOnce(false)

    await surfaceModelSwitchConfirm(buildOptions({ confirmMessage: GUARD_MESSAGE }))

    const description = confirmMock.mock.calls[0][0].description as {
      props: { children: string; className: string }
      type: string
    }[]

    expect(description).toHaveLength(2)
    // Blocks are spans: Radix renders the description itself as a <p>.
    expect(description.map(block => block.type)).toEqual(['span', 'span'])
    // A single newline inside a block is the gateway's own line break — kept.
    expect(description[0].props.children).toBe(
      'This session holds ~206,172 tokens of context.\nSwitching re-reads it.'
    )
    expect(description[1].props.children).toBe('Context window shrinks (1,000,000 → 256,000).')
  })

  it('still explains itself when the gateway sent no message', async () => {
    confirmMock.mockResolvedValueOnce(false)

    await surfaceModelSwitchConfirm(buildOptions())

    const description = confirmMock.mock.calls[0][0].description as { props: { children: string } }[]

    expect(description[0].props.children).toBe('This model switch needs confirmation.')
  })

  it('resends only once the user confirms, then reports success', async () => {
    confirmMock.mockResolvedValueOnce(true)
    const repaint = vi.fn()
    const finish = vi.fn()
    const options = buildOptions({ finish, repaint })

    await expect(surfaceModelSwitchConfirm(options)).resolves.toBe(true)

    expect(repaint).toHaveBeenCalled()
    expect(options.requestConfirmed).toHaveBeenCalledTimes(1)
    expect(finish).toHaveBeenCalledWith({ value: 'perplexity/sonar-deep-research' })
    expect(notifyError).not.toHaveBeenCalled()
  })

  it('declines with a notice when the session moved on under the dialog', async () => {
    confirmMock.mockResolvedValueOnce(true)
    const repaint = vi.fn()
    const options = buildOptions({ isStale: () => true, repaint })

    await expect(surfaceModelSwitchConfirm(options)).resolves.toBe(false)

    expect(options.requestConfirmed).not.toHaveBeenCalled()
    expect(repaint).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledWith({
      kind: 'info',
      message: 'Selection changed — the model switch was not applied.'
    })
  })

  it('keeps a switch the backend accepted when the follow-up work fails', async () => {
    confirmMock.mockResolvedValueOnce(true)
    const rollback = vi.fn()

    const options = buildOptions({
      finish: () => {
        throw new Error('invalidate failed')
      },
      rollback
    })

    await expect(surfaceModelSwitchConfirm(options)).resolves.toBe(true)

    // The switch itself succeeded, so the rolled-back selection stays undone
    // and only the follow-up failure is reported — with copy that does not
    // claim the switch failed, because it did not.
    expect(rollback).not.toHaveBeenCalled()
    expect(notifyError).toHaveBeenCalledWith(expect.any(Error), 'Model switched, but the model list could not be refreshed.')
  })

  it('treats a second confirm_required as a failure and rolls back', async () => {
    confirmMock.mockResolvedValueOnce(true)
    const rollback = vi.fn()
    const finish = vi.fn()

    const options = buildOptions({
      finish,
      requestConfirmed: vi.fn(async () => ({ confirm_message: 'still a large context', confirm_required: true })),
      rollback
    })

    await expect(surfaceModelSwitchConfirm(options)).resolves.toBe(false)

    expect(rollback).toHaveBeenCalled()
    expect(finish).not.toHaveBeenCalled()
    expect(notifyError).toHaveBeenCalledWith(expect.any(Error), 'Model switch failed')
    expect((notifyError.mock.calls[0][0] as Error).message).toBe('still a large context')
  })
})
