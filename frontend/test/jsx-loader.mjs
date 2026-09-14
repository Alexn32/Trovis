// An ESM loader that compiles .jsx for `node --test`.
//
// Vite does this in the app build; the test runner needs the same. esbuild is
// already a Vite dependency, so this adds no new package — it is 20 lines
// instead of a second test runner.
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { transform } from 'esbuild'

export async function load(url, context, nextLoad) {
  if (!url.endsWith('.jsx')) return nextLoad(url, context)
  const source = await readFile(fileURLToPath(url), 'utf8')
  const { code } = await transform(source, {
    loader: 'jsx', format: 'esm', target: 'node20',
    // The automatic runtime, matching @vitejs/plugin-react: the components do
    // not import React themselves, so the classic runtime would not find it.
    jsx: 'automatic',
    sourcefile: fileURLToPath(url),
  })
  return { format: 'module', source: code, shortCircuit: true }
}
