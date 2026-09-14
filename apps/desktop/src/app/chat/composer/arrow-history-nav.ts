/** Which direction the composer's history ring is being walked. */
export type HistoryStep = 'backward' | 'forward'

export interface HistoryStepArgs {
  /** True while the composer is already stepping through sent-message history. */
  browsing: boolean
  /** Live composer text. */
  draft: string
  /** The ↑/↓ recall preference (Settings → Chat). */
  enabled: boolean
}

/**
 * Whether an arrow press may walk sent-message history.
 *
 * ↓ only ever walks back to the present, so it needs an open ring. ↑ opens the
 * ring from an untouched composer and keeps stepping while browsing, but never
 * hijacks a typed draft the user did not recall. Both are off when the user has
 * turned the arrows off in Settings.
 */
export function historyStepAllowed(step: HistoryStep, args: HistoryStepArgs): boolean {
  if (!args.enabled) {
    return false
  }

  if (step === 'forward') {
    return args.browsing
  }

  return args.browsing || args.draft.trim().length === 0
}
