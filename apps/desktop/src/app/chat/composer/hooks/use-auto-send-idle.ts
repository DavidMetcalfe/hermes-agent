import { useCallback, useEffect, useRef, useState } from 'react'

export interface UseAutoSendIdleArgs {
  /** Master switch from the Settings toggle. */
  enabled: boolean
  /** Idle window in milliseconds. */
  delayMs: number
  /** Live fire-time gate. Read at fire time through a ref, never a stale closure.
   *  The caller (composer) decides what "safe to send right now" means. */
  canAutoSend: () => boolean
  /** Live draft text. The caller owns the DOM read (composerPlainText(editorRef.current)). */
  readText: () => string
  /** The send itself — the composer's submitDraft (the same path Enter takes). */
  onFire: () => void
  /** Identity of the target session/queue scope. A change disarms. */
  resetKey: string
}

export interface AutoSendIdle {
  /** An editor input event: trusted = nativeEvent.isTrusted, inputType = InputEvent.inputType.
   *  Untrusted input, or an input that is not a dictation/typing insert, only DISARMS. */
  noteEdit: (trusted: boolean, inputType?: string) => void
  /** Arm for a committed IME composition (compositionend) — CJK dictation commits there. */
  noteCommittedComposition: () => void
  /** Disarm without arming (blur, busy, disabled, popover open, unmount). */
  cancel: () => void
  /** Whole seconds until the pending send, or null when disarmed. */
  armedInSeconds: null | number
}

export const ARM_INPUT_TYPES = ['insertText', 'insertReplacementText'] as const

/**
 * Hands-free send: auto-submits after the user stops typing or dictating.
 * Arms on trusted typing/dictation inserts or committed IME compositions.
 * Re-arms on newer inserts; cancels on non-insert inputs, untrusted writes,
 * session changes, or manual blur/busy/disabled gates.
 */
export function useAutoSendIdle(args: UseAutoSendIdleArgs): AutoSendIdle {
  // Mirror latest args in a ref so timer callbacks always read live gates
  // without capturing a stale render closure.
  const latestArgs = useRef(args)
  latestArgs.current = args

  const [armedInSeconds, setArmedInSeconds] = useState<null | number>(null)
  const armedInSecondsRef = useRef<null | number>(null)
  const fireTimerRef = useRef<null | number>(null)
  const intervalRef = useRef<null | number>(null)
  const deadlineRef = useRef<null | number>(null)
  const armedResetKeyRef = useRef<null | string>(null)
  const isMountedRef = useRef(true)

  const prevResetKeyRef = useRef(args.resetKey)
  const prevDelayMsRef = useRef(args.delayMs)
  const prevEnabledRef = useRef(args.enabled)

  const cancel = useCallback(() => {
    if (typeof window !== 'undefined') {
      if (fireTimerRef.current !== null) {
        window.clearTimeout(fireTimerRef.current)
        fireTimerRef.current = null
      }

      if (intervalRef.current !== null) {
        window.clearInterval(intervalRef.current)
        intervalRef.current = null
      }
    }

    deadlineRef.current = null
    armedResetKeyRef.current = null

    if (armedInSecondsRef.current !== null) {
      armedInSecondsRef.current = null

      if (isMountedRef.current) {
        setArmedInSeconds(null)
      }
    }
  }, [])

  const fire = useCallback(() => {
    const firedResetKey = armedResetKeyRef.current

    // Disarm FIRST before inspecting gates or invoking onFire so that a failed
    // gate or an onFire side-effect never leaves a hanging timer or double-fires.
    cancel()

    if (!isMountedRef.current) {
      return
    }

    const { canAutoSend, delayMs, enabled, onFire, readText, resetKey } = latestArgs.current

    if (!enabled || delayMs <= 0) {
      return
    }

    // Guard against firing into a session that switched under us.
    if (firedResetKey !== null && firedResetKey !== resetKey) {
      return
    }

    const text = readText()

    if (!text.trim()) {
      return
    }

    // Never auto-send a partially-typed slash command line.
    if (text.trimStart().startsWith('/')) {
      return
    }

    if (!canAutoSend()) {
      return
    }

    onFire()
  }, [cancel])

  const arm = useCallback(() => {
    if (typeof window === 'undefined') {
      return
    }

    const { delayMs, enabled, resetKey } = latestArgs.current

    if (!enabled || delayMs <= 0) {
      return
    }

    // Re-arm: a newer insert cancels the pending window and restarts it.
    if (fireTimerRef.current !== null) {
      window.clearTimeout(fireTimerRef.current)
      fireTimerRef.current = null
    }

    if (intervalRef.current !== null) {
      window.clearInterval(intervalRef.current)
      intervalRef.current = null
    }

    const deadline = Date.now() + delayMs
    deadlineRef.current = deadline
    armedResetKeyRef.current = resetKey

    const initialSeconds = Math.ceil(delayMs / 1000)
    armedInSecondsRef.current = initialSeconds
    setArmedInSeconds(initialSeconds)

    intervalRef.current = window.setInterval(() => {
      // The interval must never keep the hook armed on its own.
      if (fireTimerRef.current === null || deadlineRef.current === null) {
        if (typeof window !== 'undefined' && intervalRef.current !== null) {
          window.clearInterval(intervalRef.current)
          intervalRef.current = null
        }

        return
      }

      const remainingMs = deadlineRef.current - Date.now()

      if (remainingMs <= 0) {
        return
      }

      const nextSeconds = Math.ceil(remainingMs / 1000)

      if (nextSeconds !== armedInSecondsRef.current) {
        armedInSecondsRef.current = nextSeconds
        setArmedInSeconds(nextSeconds)
      }
    }, 1000)

    fireTimerRef.current = window.setTimeout(fire, delayMs)
  }, [fire])

  const noteEdit = useCallback(
    (trusted: boolean, inputType?: string) => {
      const isArmInput =
        trusted &&
        typeof inputType === 'string' &&
        (ARM_INPUT_TYPES as readonly string[]).includes(inputType)

      // Untrusted writes (draft restore, undo, queue edit) or non-insert inputs
      // (paste, backspace) must only cancel and never arm.
      if (!isArmInput) {
        cancel()

        return
      }

      arm()
    },
    [arm, cancel]
  )

  const noteCommittedComposition = useCallback(() => {
    arm()
  }, [arm])

  // Disarm immediately when enabled flips to false.
  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    if (prevEnabledRef.current && !args.enabled) {
      cancel()
    }

    prevEnabledRef.current = args.enabled
  }, [args.enabled, cancel])

  // Disarm immediately when delayMs changes while armed.
  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    if (prevDelayMsRef.current !== args.delayMs) {
      prevDelayMsRef.current = args.delayMs

      if (fireTimerRef.current !== null) {
        cancel()
      }
    }
  }, [args.delayMs, cancel])

  // Disarm immediately when target session/queue scope changes.
  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    if (prevResetKeyRef.current !== args.resetKey) {
      prevResetKeyRef.current = args.resetKey
      cancel()
    }
  }, [args.resetKey, cancel])

  // Clear timers on unmount so no callbacks outlive the component.
  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    isMountedRef.current = true

    return () => {
      isMountedRef.current = false
      cancel()
    }
  }, [cancel])

  return {
    armedInSeconds,
    cancel,
    noteCommittedComposition,
    noteEdit
  }
}
