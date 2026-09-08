// Fleet / Dashboard empty-state: timeout and abort must not read as "no agents".
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { isTimeoutError, isUnreachableError, timeoutError } from '../src/httpTimeout.js'

test('504 / TimeoutError is a timeout, not an empty fleet signal', () => {
  const gateway = new Error('agents list timed out')
  gateway.status = 504
  gateway.code = 'timeout'
  assert.equal(isTimeoutError(gateway), true)
  assert.equal(isTimeoutError(timeoutError(15_000)), true)
  const abort = new Error('Aborted')
  abort.name = 'AbortError'
  abort.code = 'aborted'
  assert.equal(isUnreachableError(abort), true)
})

test('Fleet shows timeout copy + Retry before the true-empty "No agents yet"', () => {
  const fleet = readFileSync(new URL('../src/Fleet.jsx', import.meta.url), 'utf8')
  const errorIdx = fleet.indexOf("Couldn't load agents")
  const emptyIdx = fleet.indexOf('No agents yet')
  assert.ok(errorIdx > 0, 'timeout/error heading is present')
  assert.ok(emptyIdx > errorIdx, 'true-empty heading comes after the error branch')
  assert.match(fleet, /isTimeoutError\(error\) \|\| isUnreachableError\(error\)/)
  assert.match(fleet, /Trovis didn't respond\. Retry/)
  assert.match(fleet, />\s*Retry\s*</)
  // Catch stores the error object, not a blank groups=[] that would hit empty.
  assert.match(fleet, /setError\(e\)/)
  assert.doesNotMatch(fleet, /setGroups\(\[\]\)/)
})

// Home v2 removed the Dashboard fleet grid — Fleet is tab 2, and Home must
// not first-paint GET /agents. The old "abort is not an empty fleet" rule for
// that grid is therefore gone; what replaces it is the rule below, which is
// the same idea applied to every Home section: a failed or aborted fetch
// renders Retry, never an empty state that reads as "you have nothing".
test('Home has no fleet grid to mis-empty', () => {
  const dash = readFileSync(new URL('../src/Dashboard.jsx', import.meta.url), 'utf8')
  const code = dash.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  assert.doesNotMatch(code, /listAgents/)
  assert.doesNotMatch(code, /dash-fleet-grid/)
})

test('a failed Home section shows Retry, never an empty state', () => {
  const dash = readFileSync(new URL('../src/Dashboard.jsx', import.meta.url), 'utf8')
  const code = dash.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  // `failed` is only true with nothing to show, so a refetch blip keeps the
  // last good data on screen instead of blanking the section.
  assert.match(code, /failed:\s*data === null && !!err/)
  assert.match(code, /loading:\s*data === null && !err/)
  // The work pair survives one of its two calls failing.
  assert.match(code, /failed:\s*overview === null && items === null && !!err/)
  // Every section that can fail offers a way back.
  for (const retry of [/onRetry=\{work\.retry\}/, /onClick=\{feed\.retry\}/, /onClick=\{briefing\.retry\}/]) {
    assert.match(code, retry)
  }
})

test('an empty look-at section disappears rather than announcing itself', () => {
  const dash = readFileSync(new URL('../src/Dashboard.jsx', import.meta.url), 'utf8')
  const code = dash.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  assert.match(code, /if \(rows\.length === 0\) return null/)
  // Loading must not reserve alarm-coloured space either.
  assert.match(code, /if \(work\.items === null\) return null/)
})
