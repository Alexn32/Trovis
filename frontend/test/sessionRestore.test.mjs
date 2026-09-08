// Hard request timeouts + session-restore fail-soft.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  fetchWithTimeout,
  timeoutError,
  isTimeoutError,
  isUnreachableError,
  RESTORE_TIMEOUT_MS,
  WORK_TIMEOUT_MS,
} from '../src/httpTimeout.js'
import {
  sessionRestoreDecision,
  restoreSession,
  RESTORE_RETRY_DELAY_MS,
} from '../src/sessionRestore.js'

// --- fetchWithTimeout -------------------------------------------------------

test('RESTORE_TIMEOUT_MS is a 10–15s hard cap for /auth/me', () => {
  assert.ok(RESTORE_TIMEOUT_MS >= 10_000)
  assert.ok(RESTORE_TIMEOUT_MS <= 15_000)
})

test('hanging fetch rejects with timeout instead of spinning', async () => {
  const hang = () => new Promise(() => {})
  const t0 = Date.now()
  await assert.rejects(
    () => fetchWithTimeout('http://example.invalid/auth/me', {}, 40, hang),
    (err) => isTimeoutError(err) && /timed out/i.test(err.message),
  )
  const elapsed = Date.now() - t0
  assert.ok(elapsed < 1000, `settled in ${elapsed}ms, must not hang`)
})

test('timeout aborts the underlying fetch (AbortSignal)', async () => {
  let signal
  const hang = (_url, opts) => {
    signal = opts.signal
    return new Promise(() => {})
  }
  await assert.rejects(() => fetchWithTimeout('http://x', {}, 20, hang))
  assert.equal(signal.aborted, true)
})

test('completing fetch returns before the deadline', async () => {
  const ok = async () => ({ ok: true, status: 200 })
  const res = await fetchWithTimeout('http://example', {}, 1000, ok)
  assert.equal(res.ok, true)
})

test('timeoutError is identifiable', () => {
  const err = timeoutError(10_000)
  assert.equal(err.code, 'timeout')
  assert.equal(err.status, 0)
  assert.equal(isTimeoutError(err), true)
})

// --- sessionRestoreDecision -------------------------------------------------

test('healthy /auth/me payload restores the session', () => {
  const me = { user: { email: 'a@b.com' }, org: { id: 1 }, auth: 'session' }
  const d = sessionRestoreDecision({ payload: me })
  assert.equal(d.me, me)
  assert.equal(d.signedOut, false)
  assert.equal(d.clearCredentials, false)
  assert.equal(d.restoreFailed, false)
})

test('401 / null payload clears the token and signs out', () => {
  const d = sessionRestoreDecision({ payload: null })
  assert.equal(d.me, null)
  assert.equal(d.signedOut, true)
  assert.equal(d.clearCredentials, true)
  assert.equal(d.restoreFailed, false)
})

test('timeout keeps the token (intentional) and fail-softs', () => {
  const d = sessionRestoreDecision({ error: timeoutError(10_000) })
  assert.equal(d.me, null)
  assert.equal(d.signedOut, true)
  // Keep token: a dead API is not a logged-out user. Retry can reuse it.
  assert.equal(d.clearCredentials, false)
  assert.equal(d.restoreFailed, true)
})

test('network failure keeps the token and fail-softs', () => {
  const err = new TypeError('Failed to fetch')
  const d = sessionRestoreDecision({ error: err })
  assert.equal(d.clearCredentials, false)
  assert.equal(d.restoreFailed, true)
  assert.equal(d.signedOut, true)
})

// --- restoreSession: one retry with backoff ---------------------------------

test('restoreSession does not retry a 401 (null payload)', async () => {
  let calls = 0
  const d = await restoreSession({
    validate: async () => {
      calls += 1
      return null
    },
    delay: async () => {
      throw new Error('delay must not run on 401')
    },
  })
  assert.equal(calls, 1)
  assert.equal(d.clearCredentials, true)
  assert.equal(d.restoreFailed, false)
})

test('restoreSession retries once after timeout then succeeds', async () => {
  let calls = 0
  let delayed = 0
  const me = { user: { id: 1 }, org: { id: 1 }, auth: 'session' }
  const d = await restoreSession({
    validate: async () => {
      calls += 1
      if (calls === 1) throw timeoutError(10_000)
      return me
    },
    delay: async (ms) => {
      delayed = ms
    },
    retryDelayMs: 250,
  })
  assert.equal(calls, 2)
  assert.equal(delayed, 250)
  assert.equal(d.me, me)
  assert.equal(d.restoreFailed, false)
})

test('restoreSession fail-softs after retry also times out — never spins', async () => {
  let calls = 0
  const d = await restoreSession({
    validate: async () => {
      calls += 1
      throw timeoutError(10_000)
    },
    delay: async () => {},
  })
  assert.equal(calls, 2)
  assert.equal(d.restoreFailed, true)
  assert.equal(d.clearCredentials, false)
  assert.equal(d.me, null)
})

test('RESTORE_RETRY_DELAY_MS is a short backoff, not another hang', () => {
  assert.ok(RESTORE_RETRY_DELAY_MS >= 200)
  assert.ok(RESTORE_RETRY_DELAY_MS <= 3_000)
})

test('a TCP hang (server accepts, never responds) aborts — the Railway-dead case', async () => {
  const { createServer } = await import('node:http')
  const server = createServer(() => {
    /* never write a response */
  })
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address()
  const t0 = Date.now()
  try {
    await assert.rejects(
      () => fetchWithTimeout(`http://127.0.0.1:${port}/auth/me`, {}, 80),
      (err) => isTimeoutError(err),
    )
    const elapsed = Date.now() - t0
    assert.ok(elapsed < 1500, `TCP hang settled in ${elapsed}ms`)
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})

// --- Work L1 / L2: same hang, fail-soft with Retry --------------------------

test('WORK_TIMEOUT_MS is a 10–15s hard cap for /work/summary and /work/board', () => {
  assert.ok(WORK_TIMEOUT_MS >= 10_000)
  assert.ok(WORK_TIMEOUT_MS <= 15_000)
})

test('getWorkBoard, getWorkSummary, and getLoop pass the work timeout', async () => {
  const { readFileSync } = await import('node:fs')
  const src = readFileSync(new URL('../src/api.js', import.meta.url), 'utf8')
  assert.match(src, /getWorkBoard[\s\S]{0,280}timeoutMs:\s*WORK_TIMEOUT_MS/)
  assert.match(src, /getWorkSummary[\s\S]{0,160}timeoutMs:\s*WORK_TIMEOUT_MS/)
  assert.match(src, /getLoop[\s\S]{0,80}timeoutMs:\s*WORK_TIMEOUT_MS/)
})

test('Work L1, L2, and TaskPanel render a Retry empty state, not stuck Loading', async () => {
  const { readFileSync } = await import('node:fs')
  const tab = readFileSync(new URL('../src/WorkTab.jsx', import.meta.url), 'utf8')
  const board = readFileSync(new URL('../src/Board.jsx', import.meta.url), 'utf8')
  const ui = readFileSync(new URL('../src/ui.jsx', import.meta.url), 'utf8')
  const code = ui.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  assert.match(tab, /WorkLoadFailed/)
  assert.match(board, /WorkLoadFailed/)
  assert.match(board, /StoryLoadFailed/)
  assert.match(code, /Can't load this work/)
  assert.match(code, /Can't load this task/)
  assert.match(code, />\s*Retry\s*</)
  assert.doesNotMatch(code, /Failed to fetch/)
  assert.doesNotMatch(code, /\b(loops?|possession|segments?|stations?|handoffs?)\b/i)
})

test('isUnreachableError catches Failed to fetch and timeouts, not 401s', () => {
  assert.equal(isUnreachableError(new TypeError('Failed to fetch')), true)
  assert.equal(isUnreachableError(timeoutError(15_000)), true)
  const auth = new Error('unauthorized')
  auth.status = 401
  assert.equal(isUnreachableError(auth), false)
})
