// Every run belongs to a job. A job Trovis DERIVED from an agent's telemetry
// (nobody declared one) is shown as such, never graded, and offers exactly
// one step — describing it — which is the promotion path. These pin the
// client-side half of that contract; test_derived_jobs.py pins the server.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { WORKFLOW_STRINGS } from '../src/loops.js'

const src = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')
const strip = (t) => t.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
const work = strip(src('WorkTab.jsx'))
const editor = strip(src('WorkflowEditor.jsx'))
const page = strip(src('WorkflowPage.jsx'))
const app = strip(src('App.jsx'))

test('a derived job row says so and offers the one step that changes it', () => {
  const row = work.slice(work.indexOf('function JobRow'), work.indexOf('const STATUS_CHIPS'))
  // The flag is the server's — the client never infers "derived" from a name.
  assert.match(row, /const derived = Boolean\(grouped\.job\?\.derived\)/)
  assert.match(row, /className="wk-derived-tag">not yet described</)
  assert.match(row, /derived && onEditJob && \(/)
  assert.match(row, />\s*Describe this job\s*</)
  assert.match(row, /onClick=\{\(\) => onEditJob\(grouped\.job\.id\)\}/)
  // A derived job is observed, not graded: it still runs through the one
  // badge rule, which yields tone 'none' with no expectation — and the row
  // only draws loud badges, so a derived job can never read Healthy.
  assert.match(row, /const loud = badge && \(badge\.tone === 'error' \|\| badge\.tone === 'warning'\)/)
})

test('the job page explains a derived job in the operator\'s absence, and never restates a verdict', () => {
  const jobPage = work.slice(work.indexOf('function JobPage'), work.indexOf('function SituationStrip'))
  assert.match(jobPage, /job\?\.derived && \(/)
  assert.match(jobPage, /created this job from \{job\.derived_from\}/)
  assert.match(jobPage, /observed, not graded: no expectation, no verdict/)
  assert.match(jobPage, /onClick=\{\(\) => onEditJob\(job\.id\)\}/)
})

test('the editor is the promotion path: a derived job may be named there, a declared one may not', () => {
  assert.match(editor, /const derived = Boolean\(workflow\?\.derived\)/)
  // Name field: editable on create, on a derived job, and nowhere else.
  assert.match(editor, /disabled=\{editing && !derived\}/)
  // The version payload carries `name` ONLY on promotion and only when it
  // changed — a declared job's version never renames it (the server 400s).
  assert.match(editor, /const \{ name: newName, \.\.\.rest \} = payload/)
  assert.match(editor, /derived && newName !== workflow\.name \? \{ \.\.\.rest, name: newName \} : rest/)
  assert.match(editor, /WS\.derivedNote\(workflow\.derived_from\)/)
})

test('the workflow page reads Describe on a derived job, Edit steps otherwise', () => {
  assert.match(page, /wf\.derived \? WS\.describeJob : WS\.editStations/)
  assert.match(page, /wf\.derived && <span className="wfe-vchip is-derived">/)
})

test('Work is handed the real editor opener, so Describe lands in the same editor as Edit', () => {
  const tab = app.slice(app.indexOf('<WorkTab'), app.indexOf('</TabPane>', app.indexOf('<WorkTab')))
  assert.match(tab, /onEditWorkflow=\{\(id\) => id && setOverlay\(\{ kind: 'workflow-edit', id \}\)\}/)
  assert.match(work, /onEditJob=\{onEditWorkflow\}/)
})

test('no jargon in the derived-job copy', () => {
  const FORBIDDEN = /\b(loops?|workloops?|possession|segments?|stations?|handoffs?|telemetry)\b/i
  assert.ok(!FORBIDDEN.test(WORKFLOW_STRINGS.derivedTag), WORKFLOW_STRINGS.derivedTag)
  assert.ok(!FORBIDDEN.test(WORKFLOW_STRINGS.derivedNote('ops-bot')), WORKFLOW_STRINGS.derivedNote('ops-bot'))
  assert.ok(!FORBIDDEN.test(WORKFLOW_STRINGS.describeJob))
  for (const m of work.matchAll(/>([^<>{}]{3,})</g)) {
    assert.ok(!FORBIDDEN.test(m[1]), `Work ships jargon: ${JSON.stringify(m[1])}`)
  }
})
