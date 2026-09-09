// Password reset: the emailed link is single-use, so a consumed token must
// not outlive the reset — and a spent link must never dead-end.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const app = readFileSync(new URL('../src/App.jsx', import.meta.url), 'utf8')
const login = readFileSync(new URL('../src/Login.jsx', import.meta.url), 'utf8')

test('a consumed reset token is cleared from the URL, like the invite token', () => {
  // Both tokens are read once at mount, and a ?reset= token routes straight
  // to the reset panel. Leaving it in the URL pinned the tab to "Choose a new
  // password" the next time the session was gone, where the spent token
  // failed every submit — the user could not reach the login form.
  assert.match(app, /if \(inviteToken \|\| resetToken\) window\.history\.replaceState/)
})

test('reaching the login form does not depend on the reset panel succeeding', () => {
  // The panel gets both escape hatches wired from Login's mode state.
  assert.match(login, /onSignIn=\{\(\) => setMode\('login'\)\}/)
  assert.match(login, /onForgot=\{\(\) => setMode\('forgot'\)\}/)
  assert.match(login, /function ResetPanel\(\{ token, onSuccess, onSignIn, onForgot \}\)/)
  // A spent link swaps the form for a recovery screen, so "Update password"
  // is no longer the only control on it.
  assert.match(login, /if \(spent\)/)
  assert.match(login, /This reset link has expired\./)
})

test('only a bad link is treated as spent — a short password stays a form error', () => {
  // /auth/reset-password answers 400 for both a spent link and a password
  // under the 10-character floor; the two must not read the same.
  assert.match(login, /err\?\.status === 400 && \/reset link\/i\.test\(msg\)/)
  assert.match(login, /password\.length < 10/)
})
