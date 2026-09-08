// Logout / Dashboard abort: hung briefing (120s) must not pin Log out or a
// tab switch. node:test, no React renderer — same style as sessionRestore.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  fetchWithTimeout,
  abortInFlightRequests,
  abortError,
  isAbortError,
  isTimeoutError,
  inFlightCount,
  LLM_TIMEOUT_MS,
} from '../src/httpTimeout.js'
import { startAbortable } from '../src/abortable.js'
import { performLogout } from '../src/sessionLogout.js'

function hang(_url, _opts) {
  return new Promise(() => {})
}

test('LLM_TIMEOUT_MS is the 120s briefing cap', () => {
  assert.equal(LLM_TIMEOUT_MS, 120_000)
})

test('abortInFlightRequests unblocks a hung fetch immediately', async () => {
  const p = fetchWithTimeout('http://example.invalid/dashboard/briefing', {}, LLM_TIMEOUT_MS, hang)
  assert.ok(inFlightCount() >= 1)
  const t0 = Date.now()
  abortInFlightRequests()
  await assert.rejects(p, (err) => isAbortError(err))
  const elapsed = Date.now() - t0
  assert.ok(elapsed < 1000, `aborted in ${elapsed}ms, must not wait out 120s`)
  assert.equal(inFlightCount(), 0)
})

test('caller AbortSignal unblocks a hung fetch (Dashboard unmount)', async () => {
  const ac = new AbortController()
  const p = fetchWithTimeout(
    'http://example.invalid/dashboard/briefing',
    { signal: ac.signal },
    LLM_TIMEOUT_MS,
    hang,
  )
  const t0 = Date.now()
  ac.abort()
  await assert.rejects(p, (err) => isAbortError(err))
  assert.ok(Date.now() - t0 < 1000)
})

test('timeout still wins when nobody aborts (hang ignores signal)', async () => {
  const t0 = Date.now()
  await assert.rejects(
    () => fetchWithTimeout('http://x', {}, 40, hang),
    (err) => isTimeoutError(err),
  )
  assert.ok(Date.now() - t0 < 1000)
})

test('abortError is identifiable and not a timeout', () => {
  const err = abortError()
  assert.equal(isAbortError(err), true)
  assert.equal(isTimeoutError(err), false)
})

test('startAbortable cleanup aborts the signal (Dashboard unmount)', () => {
  let signal
  let isAlive
  const cleanup = startAbortable(({ signal: s, isAlive: alive }) => {
    signal = s
    isAlive = alive
  })
  assert.equal(isAlive(), true)
  assert.equal(signal.aborted, false)
  cleanup()
  assert.equal(signal.aborted, true)
  assert.equal(isAlive(), false)
})

test('performLogout does not wait on a hung logout POST', () => {
  let aborted = false
  let cleared = 0
  let navigated = false
  const t0 = Date.now()
  performLogout({
    abort: () => {
      aborted = true
    },
    logoutRequest: () => new Promise(() => {}),
    clearSessionToken: () => {
      cleared += 1
    },
    clearApiKey: () => {
      cleared += 1
    },
    clearPersistedView: () => {
      cleared += 1
    },
    navigate: () => {
      navigated = true
    },
  })
  const elapsed = Date.now() - t0
  assert.ok(elapsed < 50, `logout took ${elapsed}ms`)
  assert.equal(aborted, true)
  assert.equal(cleared, 3)
  assert.equal(navigated, true)
})

test('performLogout aborts in-flight GETs then navigates without awaiting them', async () => {
  const hung = fetchWithTimeout('http://example.invalid/dashboard/briefing', {}, LLM_TIMEOUT_MS, hang)
  let navigated = false
  const t0 = Date.now()
  performLogout({
    logoutRequest: () => new Promise(() => {}),
    clearSessionToken: () => {},
    clearApiKey: () => {},
    clearPersistedView: () => {},
    navigate: () => {
      navigated = true
    },
  })
  assert.equal(navigated, true)
  assert.ok(Date.now() - t0 < 1000)
  await assert.rejects(hung, (err) => isAbortError(err))
})

test('App.jsx logout does not await api.logout (uses performLogout)', () => {
  const app = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8')
  assert.match(app, /performLogout/)
  assert.doesNotMatch(app, /await api\.logout/)
  // First paint lands on Work so we don't fire six Dashboard GETs on boot.
  assert.match(app, /legacyTab \|\| 'work'/)
})

test('Dashboard unmount aborts briefing/attention/work-feed/cost; no 15s waiting poll', () => {
  const dash = readFileSync(new URL('../src/Dashboard.jsx', import.meta.url), 'utf8')
  const code = dash.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  assert.match(code, /startAbortable/)
  assert.match(code, /getBriefing\(\{\s*signal\s*\}\)/)
  assert.match(code, /getAttention\(\{\s*signal\s*\}\)/)
  assert.match(code, /getCost\(\{\s*signal\s*\}\)/)
  assert.match(code, /getWorkFeed\(\{\s*signal\s*\}\)/)
  assert.doesNotMatch(code, /setInterval/)
  assert.doesNotMatch(code, /15000/)
  assert.doesNotMatch(code, /15_000/)
})

test('dashboard API methods forward an AbortSignal', () => {
  const api = readFileSync(new URL('../src/api.js', import.meta.url), 'utf8')
  assert.match(api, /getBriefing:\s*\(opts = \{\}\) =>/)
  assert.match(api, /getAttention:\s*\(opts = \{\}\) =>/)
  assert.match(api, /getCost:\s*\(opts = \{\}\) =>/)
  assert.match(api, /getWorkFeed:\s*\(opts = \{\}\) =>/)
  assert.match(api, /keepalive:\s*true/)
})
