// The Execution Graph API helper, and where it may be read from.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'

const srcDir = new URL('../src/', import.meta.url)
const src = (f) => readFileSync(new URL(f, srcDir), 'utf8')
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

test('41. api.getWorkItemExecution reads /work/items/:id/execution with an abort signal, like evidence and coverage', () => {
  const api = strip(src('api.js'))
  assert.match(api, /getWorkItemExecution:\s*\(id, \{ signal = undefined \} = \{\}\)/)
  assert.ok(api.includes('/work/items/${encodeURIComponent(id)}/execution'))
  assert.match(api, /getWorkItemExecution[\s\S]{0,200}timeoutMs: WORK_TIMEOUT_MS/)
})

test('42. only the Run page reads the Execution Graph — never Home, the Work home, the board or a job roll-up', () => {
  const allowed = new Set(['api.js', 'JobDetail.jsx'])
  const files = readdirSync(srcDir).filter((f) => /\.(jsx?|mjs)$/.test(f) && !allowed.has(f))
  for (const f of files) {
    const code = strip(src(f))
    assert.doesNotMatch(code, /getWorkItemExecution/, f)
  }
  const page = strip(src('JobDetail.jsx'))
  assert.match(page, /api\s*\.getWorkItemExecution\(item\.id, \{ signal \}\)/)
  assert.match(page, /if \(!isPage\) return undefined\s+setExecution\(null\)\s+setExecutionErr\(null\)\s+setSelectedNode\(null\)/,
    'page-only, and body + selection reset before every load')
  assert.match(page, /if \(pageView !== 'execution'\) return undefined/, 'fetched only while the Execution view is open')
})
