import { translateNow } from '@/i18n'
import { confirm } from '@/store/confirm'
import { notify, notifyError } from '@/store/notifications'

/** The gateway's model-switch handshake shape — shared by `config.set model`
 *  and `profiles.configure` (Bots editor). `confirm_required: true` means the
 *  switch was intentionally NOT applied and the gateway is waiting for a
 *  resend that carries `confirm_expensive_model: true`. */
export interface GuardedModelSwitchResult {
  confirm_message?: string
  confirm_required?: boolean
  deferred?: boolean
}

export interface SurfaceModelSwitchConfirmOptions<T extends GuardedModelSwitchResult> {
  /** The model the switch targets — named in the dialog title when known. */
  model?: string
  /** The gateway's `confirm_message`, when present. */
  confirmMessage?: string
  /** Error-toast copy when the confirmed resend still fails. */
  failureMessage: string
  /** Runs after the confirmed resend succeeds (cache invalidation etc.). */
  finish?: (result: T | undefined) => void
  /** Staleness guard — the session/model can move on while the dialog is
   *  open. Return true to make the answered confirm a no-op (with a notice
   *  saying so) instead of clobbering the newer choice. */
  isStale?: () => boolean
  /** Optimistically repaint the pending selection before the resend. */
  repaint?: () => void
  /** Resend the switch WITH `confirm_expensive_model: true`. */
  requestConfirmed: () => Promise<T | undefined>
  /** Undo the optimistic repaint when the confirmed resend fails. */
  rollback?: () => void
}

/** The gateway composes its warning as plain text: blank lines separate the
 *  blocks, single newlines break a sentence in two. Each block becomes its own
 *  paragraph — the toast this replaced flattened all of it into one run-on
 *  line — and the block's own line breaks are preserved verbatim.
 *
 *  Blocks are `span`s, not `p`s: Radix renders the dialog description as a
 *  `<p>`, and a nested `<p>` is invalid DOM. */
function messageParagraphs(message?: string) {
  const content = (message ?? '').trim() || translateNow('desktop.modelSwitchConfirmBody')

  return content
    .split(/\n{2,}/)
    .map(block => block.trim())
    .filter(Boolean)
    .map((block, index) => (
      <span className={index === 0 ? 'block whitespace-pre-line' : 'mt-2 block whitespace-pre-line'} key={index}>
        {block}
      </span>
    ))
}

/**
 * THE confirm flow for guarded model switches — every surface that can
 * receive `confirm_required` from a model switch (core picker via
 * `config.set`, Bots editor via `profiles.configure`, future surfaces) routes
 * it through here so there is exactly one applier and no forked confirm
 * logic per surface (#95293).
 *
 * A refused switch is a decision, not a notification: it asks through
 * `confirm()`, the app's own `ConfirmDialog`, so there is a real decline
 * button, `Esc`/backdrop/✕ all mean the same thing, and the dialog owns focus
 * while it waits. Declining is free — the switch is not applied until the user
 * answers, and only then is the resend with `confirm_expensive_model: true`
 * sent (the gateway's guard is not bypassed before that). A resend that STILL
 * answers `confirm_required` is treated as a failure — the gateway asked
 * twice, something is wrong; never loop.
 *
 * Resolves `true` when the switch was applied, `false` when it was declined,
 * went stale, or failed (a failure surfaces its own toast).
 */
export async function surfaceModelSwitchConfirm<T extends GuardedModelSwitchResult>(
  options: SurfaceModelSwitchConfirmOptions<T>
): Promise<boolean> {
  const accepted = await confirm({
    cancelLabel: translateNow('desktop.modelSwitchKeepLabel'),
    confirmLabel: translateNow('desktop.modelSwitchConfirmLabel'),
    description: messageParagraphs(options.confirmMessage),
    destructive: true,
    title: options.model
      ? translateNow('desktop.modelSwitchConfirmTitle', options.model)
      : translateNow('desktop.modelSwitchConfirmTitleFallback')
  })

  if (!accepted) {
    return false
  }

  if (options.isStale?.()) {
    // The answer landed, but the session or model moved on underneath it, so
    // the switch it asked for no longer exists. Say so: a silent no-op reads
    // as a broken button.
    notify({ kind: 'info', message: translateNow('desktop.modelSwitchStaleNotice') })

    return false
  }

  let result: T | undefined

  try {
    options.repaint?.()
    result = await options.requestConfirmed()

    if (result?.confirm_required) {
      throw new Error(result.confirm_message?.trim() || options.failureMessage)
    }
  } catch (err) {
    options.rollback?.()
    notifyError(err, options.failureMessage)

    return false
  }

  // Follow-up work failing must never undo a switch the backend accepted, so
  // it is reported on its own and the applied switch stands.
  try {
    options.finish?.(result)
  } catch (err) {
    notifyError(err, translateNow('desktop.modelSwitchRefreshFailed'))
  }

  return true
}
