// Hard request timeouts. Native `fetch` has no deadline — a hung TCP/proxy
// (e.g. Railway not answering) leaves the promise pending forever, which is
// what pinned the dashboard on "Restoring session…". Every API call goes
// through `fetchWithTimeout` so that cannot happen.

export const DEFAULT_TIMEOUT_MS = 15_000
// /auth/me must be fast. Don't make a returning user stare at the restore
// shell for the full default just because the API is dead.
export const RESTORE_TIMEOUT_MS = 10_000
// Claude-backed endpoints (Ask, briefing, describe, drafts) routinely take
// longer than a fleet GET. A 15s cap would abort real answers.
export const LLM_TIMEOUT_MS = 120_000

export function timeoutError(timeoutMs) {
  const secs = timeoutMs > 0 ? Math.round(timeoutMs / 1000) : 0
  const err = new Error(
    secs > 0
      ? `Request timed out after ${secs}s`
      : 'Request timed out',
  )
  err.name = 'TimeoutError'
  err.code = 'timeout'
  err.status = 0
  return err
}

export function isTimeoutError(err) {
  return err?.code === 'timeout' || err?.name === 'TimeoutError'
}

/**
 * Fetch with a hard deadline. Aborts the underlying request when `timeoutMs`
 * elapses and rejects with a TimeoutError — even if `fetchImpl` ignores
 * AbortSignal (so a hung mock or a stuck TCP can't spin forever).
 */
export async function fetchWithTimeout(
  url,
  options = {},
  timeoutMs = DEFAULT_TIMEOUT_MS,
  fetchImpl = globalThis.fetch,
) {
  const { signal: userSignal, ...rest } = options
  if (!(timeoutMs > 0) || timeoutMs === Infinity) {
    return fetchImpl(url, options)
  }

  const controller = new AbortController()
  const onUserAbort = () => controller.abort()
  if (userSignal) {
    if (userSignal.aborted) {
      const err = new Error('Aborted')
      err.name = 'AbortError'
      throw err
    }
    userSignal.addEventListener('abort', onUserAbort, { once: true })
  }

  let timeoutId
  try {
    const fetchPromise = fetchImpl(url, { ...rest, signal: controller.signal })
    // If we time out first, a late AbortError from fetch must not become an
    // unhandled rejection.
    void fetchPromise.catch(() => {})
    const timeoutPromise = new Promise((_, reject) => {
      timeoutId = setTimeout(() => {
        controller.abort()
        reject(timeoutError(timeoutMs))
      }, timeoutMs)
    })
    return await Promise.race([fetchPromise, timeoutPromise])
  } finally {
    clearTimeout(timeoutId)
    if (userSignal) userSignal.removeEventListener('abort', onUserAbort)
  }
}
