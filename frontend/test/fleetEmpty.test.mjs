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
