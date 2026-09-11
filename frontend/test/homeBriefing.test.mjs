// The daily briefing is the insight on Home: above Ask, never collapsed,
// generated, and stamped with the reader's own clock.
//
// Two kinds of check here. The clock and the lead are pure functions, tested
// as such. The layout rules (order, no disclosure, fetch without a click) are
// decisions that live in JSX, so they are read off the source — the same
// approach the rest of the Home suites take.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { asOfLabel, briefingLead, parseServerTime, viewerClock } from '../src/home.js'

const apiSrc = readFileSync(new URL('../src/api.js', import.meta.url), 'utf8')

// --- layout ----------------------------------------------------------------

test('a UTC stamp reads on the machine clock, not as a UTC hour relabelled', () => {
  // The bug: generated_at comes off a TIMESTAMP column as UTC with nothing
  // saying so, and Date.parse treats an offset-less date-time as LOCAL — so
  // 21:47 UTC printed as "9:47 PM" for a reader in Chicago.
  const CHI = 'America/Chicago'
  assert.equal(asOfLabel('2026-09-09T21:47:00Z', 'en-US', CHI), 'As of 4:47 PM')
  // The two shapes the server actually sends, both naive, both UTC.
  assert.equal(asOfLabel('2026-09-09 21:47:00', 'en-US', CHI), 'As of 4:47 PM')
  assert.equal(asOfLabel('2026-09-09T21:47:00', 'en-US', CHI), 'As of 4:47 PM')
  // Explicit offsets are respected rather than re-pinned.
  assert.equal(asOfLabel('2026-09-09T21:47:00+00:00', 'en-US', CHI), 'As of 4:47 PM')
  assert.equal(asOfLabel('2026-09-09T16:47:00-05:00', 'en-US', CHI), 'As of 4:47 PM')
  // Same instant, a different reader.
  assert.equal(asOfLabel('2026-09-09T21:47:00Z', 'en-US', 'Europe/Berlin'), 'As of 11:47 PM')
})

test('a missing or unreadable stamp prints no footer', () => {
  assert.equal(asOfLabel(null), '')
  assert.equal(asOfLabel(undefined), '')
  assert.equal(asOfLabel(''), '')
  assert.equal(asOfLabel('not a date'), '')
  assert.equal(parseServerTime(null), null)
  assert.equal(parseServerTime('nonsense'), null)
  assert.equal(parseServerTime('2026-09-09T21:47:00Z'), Date.parse('2026-09-09T21:47:00Z'))
  // A naive stamp is the same instant as its Z-suffixed twin.
  assert.equal(parseServerTime('2026-09-09 21:47:00'), Date.parse('2026-09-09T21:47:00Z'))
})

test('the request carries the reader hour and zone, so the model cannot guess', () => {
  const clock = viewerClock(new Date('2026-09-09T21:47:00Z'))
  assert.equal(typeof clock.localHour, 'number')
  assert.ok(clock.localHour >= 0 && clock.localHour <= 23)
  // getHours() is the machine's hour for that instant, whatever this box is set to.
  assert.equal(clock.localHour, new Date('2026-09-09T21:47:00Z').getHours())
  assert.ok(clock.timeZone === null || typeof clock.timeZone === 'string')
  // api.js turns them into the optional query the endpoint accepts.
  assert.match(apiSrc, /qs\.set\('local_hour', String\(localHour\)\)/)
  assert.match(apiSrc, /qs\.set\('tz', timeZone\)/)
  assert.match(apiSrc, /Number\.isInteger\(localHour\)/, 'hour 0 must not be dropped as falsy')
})

// --- the lead --------------------------------------------------------------

test('the lead still carries no digits, no money, and no jargon', () => {
  const FORBIDDEN = /\bloops?\b|\bhandoffs?\b|\bspans?\b|\btelemetry\b|possession/i
  for (const needs_you of [0, 1, 5]) {
    for (const needs_attention of [0, 1, 4]) {
      for (const open of [0, 1, 9]) {
        for (const moving of [0, 3]) {
          const s = briefingLead({ needs_you, needs_attention, open }, { moving })
          assert.doesNotMatch(s, /\d/, `lead has a digit: ${s}`)
          assert.doesNotMatch(s, /[$€£]/, `lead has money: ${s}`)
          assert.ok(!FORBIDDEN.test(s), `lead ships jargon: ${s}`)
          if (moving === 0) {
            assert.doesNotMatch(s, /in progress/, `claims progress with 0 moving: ${s}`)
          }
        }
      }
    }
  }
})
