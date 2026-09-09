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

const dash = readFileSync(new URL('../src/Dashboard.jsx', import.meta.url), 'utf8')
const apiSrc = readFileSync(new URL('../src/api.js', import.meta.url), 'utf8')

// --- layout ----------------------------------------------------------------

test('the briefing sits above Ask in the Home tree', () => {
  const brief = dash.indexOf('<Briefing')
  const ask = dash.indexOf('<AskSection')
  assert.ok(brief > 0 && ask > 0, 'both sections are rendered')
  assert.ok(brief < ask, 'the insight comes before the way to go deeper')
})

test('nothing collapses the briefing — no More/Less, no chevron', () => {
  const section = dash.slice(dash.indexOf('function Briefing('), dash.indexOf('// --- first run'))
  assert.doesNotMatch(section, /showMore|home-brief-more/, 'no disclosure state or control')
  assert.doesNotMatch(section, /Chevron/, 'no disclosure affordance')
  assert.doesNotMatch(section, /aria-expanded/)
  // The generated narrative is rendered directly, not behind a flag.
  assert.match(section, /briefing\.data\?\.summary \? \(/)
  assert.match(section, /home-brief-narrative/)
  // And the chevrons are no longer imported for it.
  assert.doesNotMatch(dash, /ChevronDownIcon|ChevronRightIcon/)
})

test('the briefing fetch starts on render, not on a click', () => {
  const hook = dash.slice(dash.indexOf('function useBriefing('), dash.indexOf('function usePacket('))
  // The old shape took an `open` flag and bailed out until something set it.
  assert.doesNotMatch(hook, /if \(!open\) return/, 'no lazy-on-disclose gate')
  assert.doesNotMatch(dash, /useLazyBriefing/)
  assert.match(hook, /const \{ localHour, timeZone \} = viewerClock\(\)/)
  assert.match(hook, /api\s*\.getBriefing\(\{ signal, localHour, timeZone \}\)/)
  // Still an independent, abortable section fetch — Home must not wait on it.
  assert.match(hook, /startAbortable/)
  // And no new polling: the only refetches are the shared refreshKey and Retry.
  assert.doesNotMatch(hook, /setInterval|setTimeout/)
  assert.match(hook, /\}, \[armed, refreshKey, nonce\]\)/)
  // Armed by Home being SHOWN, not by a click — and it latches, so switching
  // back to Home does not refetch what is already on the page.
  assert.match(hook, /if \(active\) setArmed\(true\)/)
  assert.match(hook, /if \(!armed\) return undefined/)
})

test('a failed briefing keeps the lead and offers a retry', () => {
  const section = dash.slice(dash.indexOf('function Briefing('), dash.indexOf('// --- first run'))
  assert.match(section, /home-brief-lead/, 'the templated lead renders regardless')
  assert.match(section, /briefing\.failed \?/)
  assert.match(section, /onClick=\{briefing\.retry\}/)
})

test('first run still shows no briefing at all', () => {
  // Nothing connected: no fleet, no strip, and no generated story about a
  // fleet that does not exist. Briefing lives in the connected branch only.
  const firstRunBranch = dash.slice(dash.indexOf('{firstRun ? ('), dash.indexOf('<FleetPulse'))
  assert.doesNotMatch(firstRunBranch, /<Briefing/)
  assert.match(firstRunBranch, /<FirstRun/)
})

// --- the reader's clock ----------------------------------------------------

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

test('the footer says "As of" and nothing about UTC', () => {
  const label = asOfLabel('2026-09-09T21:47:00Z', 'en-US', 'America/Chicago')
  assert.match(label, /^As of /)
  assert.doesNotMatch(label, /UTC|GMT|Z\b/)
  const section = dash.slice(dash.indexOf('function Briefing('), dash.indexOf('// --- first run'))
  assert.match(section, /asOfLabel\(briefing\.data\?\.generated_at\)/)
  assert.doesNotMatch(section, /timeZone:/, 'the product never forces a zone')
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

test('the lead claims progress only off a moving count it actually read', () => {
  // "Everything open is in progress" above a strip reading 0 moving / 3
  // waiting is the same class of lie as the old shape-of-the-day line.
  assert.equal(
    briefingLead({ needs_you: 0, needs_attention: 0, open: 4 }, { moving: 0, waiting: 4 }),
    'Nothing needs you right now.',
  )
  assert.equal(
    briefingLead({ needs_you: 0, needs_attention: 0, open: 4 }, { moving: 2, waiting: 2 }),
    'Nothing needs you. Everything open is in progress.',
  )
  // No proof counts at all → no progress claim.
  assert.equal(
    briefingLead({ needs_you: 0, needs_attention: 0, open: 4 }),
    'Nothing needs you right now.',
  )
  // "The rest is in progress" follows the same rule.
  assert.equal(
    briefingLead({ needs_you: 2, needs_attention: 0, open: 9 }, { moving: 0 }),
    'Today, work is waiting on you.',
  )
  assert.equal(
    briefingLead({ needs_you: 2, needs_attention: 0, open: 9 }, { moving: 5 }),
    'Today, work is waiting on you. The rest is in progress.',
  )
  // Home passes the strip's own counts, so the two cannot disagree.
  assert.match(dash, /briefingLead\(work\.overview, counts\)/)
  assert.match(dash, /<Briefing work=\{work\} counts=\{counts\}/)
})

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
