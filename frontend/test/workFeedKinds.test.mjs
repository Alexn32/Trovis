// Work Feed record kinds — the difference between "we have no transcript"
// and "the agent only announced itself".
//
// The first real Grok Bot produced a feed that read, eight times over,
// "Registered with the fleet and declared its identity". It was doing real
// work; the feed filed every record with no captured exchange as a
// registration. Three surfaces had to agree to produce that, and these lock
// the two on this side: the row's kind, and the body it renders.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const detail = readFileSync(new URL('../src/AgentDetail.jsx', import.meta.url), 'utf8')

test('only a real registration is a system row', () => {
  // `kind === 'system' || !r.exchange` is the bug: it swept every
  // transcript-less record into System, where an operator never looks for
  // what their agent did.
  const feedItem = detail.slice(
    detail.indexOf('function FeedItem'),
    detail.indexOf('function WorkFeed'),
  )
  assert.match(feedItem, /const isSystem = r\.kind === 'system'\s*$/m)
  assert.doesNotMatch(feedItem, /isSystem = r\.kind === 'system' \|\| !r\.exchange/)
  assert.match(feedItem, /const isReport =/)
})

test('a reported job renders its own body, not the registration line', () => {
  const feedItem = detail.slice(
    detail.indexOf('function FeedItem'),
    detail.indexOf('function WorkFeed'),
  )
  // The registration wording must be reachable ONLY for a system record.
  assert.match(feedItem, /isSystem\s*\n?\s*\?\s*'System record/)
  assert.match(feedItem, /reported this job/)
})

test('the expanded body branches on the transcript, never on the kind', () => {
  // `!isSystem ? (... r.exchange.user ...)` reads a property off null for a
  // report record, which blanks the page — the exact failure mode the
  // no-undef lint exists to catch and cannot see here.
  const feedItem = detail.slice(
    detail.indexOf('function FeedItem'),
    detail.indexOf('function WorkFeed'),
  )
  assert.match(feedItem, /\{r\.exchange \? \(/)
  assert.doesNotMatch(feedItem, /\{!isSystem \? \(/)
})

test('a reported job is filed with the work, not under System', () => {
  const cat = detail.slice(
    detail.indexOf('function feedCategory'),
    detail.indexOf('function WorkFeed'),
  )
  assert.match(cat, /if \(r\.kind === 'system'\) return 'system'/)
  assert.doesNotMatch(cat, /!r\.exchange\) return 'system'/)
})
