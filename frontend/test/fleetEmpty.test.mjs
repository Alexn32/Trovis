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

test('Dashboard Fleet grid does not treat abort/timeout as zero agents', () => {
  const dash = readFileSync(new URL('../src/Dashboard.jsx', import.meta.url), 'utf8')
  const code = dash.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  assert.doesNotMatch(code, /setAgents\(\[\]\)/)
  assert.match(code, /setLoadError\(e\)/)
  assert.match(code, /Couldn't load agents/)
  assert.match(code, /No agents reporting telemetry yet/)
  const errIdx = code.indexOf("Couldn't load agents")
  const emptyIdx = code.indexOf('No agents reporting telemetry yet')
  assert.ok(errIdx > 0 && emptyIdx > errIdx)
  assert.match(code, /isTimeoutError\(e\) \|\| isUnreachableError\(e\)/)
})
