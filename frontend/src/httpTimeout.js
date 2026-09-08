// Hard request timeouts. Native `fetch` has no deadline — a hung TCP/proxy
// (e.g. Railway not answering) leaves the promise pending forever, which is
// what pinned the dashboard on "Restoring session…". Every API call goes
// through `fetchWithTimeout` so that cannot happen.

export const DEFAULT_TIMEOUT_MS = 15_000
// /auth/me must be fast. Don't make a returning user stare at the restore
// shell for the full default just because the API is dead.
export const RESTORE_TIMEOUT_MS = 10_000
// Work L1 (overview/items) and leftover L2 (board/summary) — same hang as
// restore: a bare fetch left "Loading…" forever. 15s abort; UI fail-softs.
export const WORK_TIMEOUT_MS = 15_000
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

/** Hung TCP, aborted fetch, or the browser's raw "Failed to fetch". */
export function isUnreachableError(err) {
  if (!err) return false
  if (isTimeoutError(err)) return true
  if (err.name === 'AbortError' || err.code === 'ABORT_ERR') return true
  if (err.code === 'network' || err.status === 0) return true
  const msg = String(err.message || '')
  return /failed to fetch|networkerror|load failed|network request failed|operation was aborted|the user aborted a request/i.test(msg)
}

/** Login / auth gate copy. Never show AbortError or "Failed to fetch". */
export const AUTH_UNREACHABLE_MESSAGE = "Can't reach Trovis — retry"
export const UNREACHABLE_MESSAGE = "Trovis didn't respond"

export function unreachableMessage(path) {
  if (typeof path === 'string' && path.startsWith('/auth/')) {
    return AUTH_UNREACHABLE_MESSAGE
  }
  return UNREACHABLE_MESSAGE
}

/** Map a login/auth failure to copy a person can act on. */
export function authErrorMessage(err) {
  if (isTimeoutError(err) || isUnreachableError(err)) {
    return AUTH_UNREACHABLE_MESSAGE
  }
  const msg = String(err?.message || '').trim()
  if (!msg || /abort|failed to fetch|networkerror|timed out|trovis didn't respond/i.test(msg)) {
    return AUTH_UNREACHABLE_MESSAGE
  }
  return msg
}

export function abortError() {
  const err = new Error('Aborted')
  err.name = 'AbortError'
  err.code = 'aborted'
  return err
}

export function isAbortError(err) {
  return err?.name === 'AbortError' || err?.code === 'aborted' || err?.code === 'ABORT_ERR'
}

// Every in-flight fetchWithTimeout registers here so logout / leaving a page
// can abort them instead of waiting out LLM_TIMEOUT (120s) on a hung briefing.
const inflightControllers = new Set()

export function inFlightCount() {
  return inflightControllers.size
}

/** Abort every tracked request. Settles hangers even if fetchImpl ignores signal. */
export function abortInFlightRequests() {
  for (const controller of [...inflightControllers]) {
    try {
      controller.abort()
    } catch {
      /* ignore */
    }
  }
}

/**
 * Fetch with a hard deadline. Aborts the underlying request when `timeoutMs`
 * elapses and rejects with a TimeoutError — even if `fetchImpl` ignores
 * AbortSignal (so a hung mock or a stuck TCP can't spin forever).
 *
 * A caller AbortSignal (Dashboard unmount) or `abortInFlightRequests()`
 * (logout) likewise settles the promise immediately, instead of waiting
 * for the timeout.
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
  inflightControllers.add(controller)
  let timedOut = false
  const onUserAbort = () => controller.abort()
  if (userSignal) {
    if (userSignal.aborted) {
      inflightControllers.delete(controller)
      throw abortError()
    }
    userSignal.addEventListener('abort', onUserAbort, { once: true })
  }

  let timeoutId
  try {
    const fetchPromise = fetchImpl(url, { ...rest, signal: controller.signal })
    // If we time out / abort first, a late AbortError from fetch must not
    // become an unhandled rejection.
    void fetchPromise.catch(() => {})

    const abortPromise = new Promise((_, reject) => {
      const onAbort = () => {
        if (!timedOut) reject(abortError())
      }
      if (controller.signal.aborted) {
        onAbort()
        return
      }
      controller.signal.addEventListener('abort', onAbort, { once: true })
    })
    void abortPromise.catch(() => {})

    const timeoutPromise = new Promise((_, reject) => {
      timeoutId = setTimeout(() => {
        timedOut = true
        controller.abort()
        reject(timeoutError(timeoutMs))
      }, timeoutMs)
    })
    return await Promise.race([fetchPromise, timeoutPromise, abortPromise])
  } finally {
    inflightControllers.delete(controller)
    clearTimeout(timeoutId)
    if (userSignal) userSignal.removeEventListener('abort', onUserAbort)
  }
}
