// The Execution Graph API helper exists; nothing renders it yet (PR 214 will).
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

test('42. no component fetches or renders the Execution Graph yet — the helper is plumbing only', () => {
  const files = readdirSync(srcDir).filter((f) => /\.(jsx?|mjs)$/.test(f) && f !== 'api.js')
  for (const f of files) {
    const code = strip(src(f))
    assert.doesNotMatch(code, /getWorkItemExecution|\/execution\b|Execution Graph/, f)
  }
})
