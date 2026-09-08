// Session bootstrap outcome. App.jsx uses this so a hung or dead API never
// leaves the auth gate on "Restoring session…".
//
// `validateSession` used to GET /auth/me via a bare fetch with no AbortSignal.
// `restoring` only cleared when that promise settled — so a hung Railway/API
// spun forever. This module is the decision table for the abort + fail-soft
// path.
//
//   ok           — /auth/me returned an identity; show Work/Dashboard
//   invalid      — credential rejected (401 → null payload)
//                  CLEAR tokens (they are expired/wrong), show login
//   unreachable  — timeout / network / 5xx after at most one retry
//                  KEEP tokens so Retry / a later reload can restore without
//                  re-typing a password; show "can't reach API — retry"
//
// Token choice is intentional: a dead API is not a logged-out user.

export const RESTORE_RETRY_DELAY_MS = 1000

export function sessionRestoreDecision({ payload = undefined, error = null } = {}) {
  if (error) {
    return {
      me: null,
      signedOut: true,
      clearCredentials: false,
      restoreFailed: true,
    }
  }
  if (!payload) {
    return {
      me: null,
      signedOut: true,
      clearCredentials: true,
      restoreFailed: false,
    }
  }
  return {
    me: payload,
    signedOut: false,
    clearCredentials: false,
    restoreFailed: false,
  }
}

/**
 * Run `validate()` (GET /auth/me). On timeout/network, wait `retryDelayMs`
 * and try once more. 401 (null payload) is not retried — the credential is
 * already known-bad.
 */
export async function restoreSession({
  validate,
  delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  retryDelayMs = RESTORE_RETRY_DELAY_MS,
} = {}) {
  try {
    const payload = await validate()
    return sessionRestoreDecision({ payload })
  } catch (error) {
    try {
      await delay(retryDelayMs)
      const payload = await validate()
      return sessionRestoreDecision({ payload })
    } catch (retryError) {
      return sessionRestoreDecision({ error: retryError })
    }
  }
}
