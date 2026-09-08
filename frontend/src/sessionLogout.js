import { abortInFlightRequests } from './httpTimeout.js'

/**
 * Log out without waiting on hung Dashboard GETs (briefing up to 120s).
 *
 * Abort in-flight fetches first so the browser connection pool is free,
 * fire the logout POST without awaiting it, then clear credentials and
 * navigate. The POST is best-effort (`keepalive` on the wire); local
 * sign-out must not wait on it.
 */
export function performLogout({
  abort = abortInFlightRequests,
  logoutRequest,
  clearSessionToken,
  clearApiKey,
  clearPersistedView,
  navigate,
} = {}) {
  abort()
  if (typeof logoutRequest === 'function') {
    try {
      const result = logoutRequest()
      if (result && typeof result.then === 'function') {
        void result.catch(() => {})
      }
    } catch {
      /* best-effort */
    }
  }
  clearSessionToken?.()
  clearApiKey?.()
  clearPersistedView?.()
  navigate?.()
}
