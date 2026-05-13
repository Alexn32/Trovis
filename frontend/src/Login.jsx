import { useState } from 'react'
import { api, setApiKey } from './api.js'
import { Spinner } from './ui.jsx'

// Login screen with two paths: sign up (creates an account + key) or
// "I have a key" (validates an existing key against the backend). After
// success we hand the key back to App.jsx via onAuthed(key).
//
// State machine:
//   mode='choose' → 'signup' → 'show-key' (after signup succeeds) → done
//   mode='choose' → 'have-key' → done (after validation succeeds)

export default function Login({ onAuthed }) {
  const [mode, setMode] = useState('choose')
  // Stash the key we just minted on signup so we can show it before
  // entering the dashboard.
  const [freshKey, setFreshKey] = useState(null)
  const [freshEmail, setFreshEmail] = useState(null)

  function handleSignupSuccess(email, key) {
    setFreshEmail(email)
    setFreshKey(key)
    setMode('show-key')
  }

  function continueToDashboard() {
    setApiKey(freshKey)
    onAuthed(freshKey)
  }

  function handleKeyValidated(key) {
    onAuthed(key)
  }

  return (
    <div className="login-shell">
      <div className="login-card">
        <header className="login-header">
          <h1 className="logo">Oversee</h1>
          <p className="subtitle">Agent Management System</p>
        </header>

        {mode === 'choose' && (
          <ChoosePanel
            onSignup={() => setMode('signup')}
            onHaveKey={() => setMode('have-key')}
          />
        )}
        {mode === 'signup' && (
          <SignupPanel
            onSuccess={handleSignupSuccess}
            onBack={() => setMode('choose')}
          />
        )}
        {mode === 'show-key' && (
          <ShowKeyPanel
            email={freshEmail}
            apiKey={freshKey}
            onContinue={continueToDashboard}
          />
        )}
        {mode === 'have-key' && (
          <HaveKeyPanel
            onSuccess={handleKeyValidated}
            onBack={() => setMode('choose')}
          />
        )}
      </div>
    </div>
  )
}

function ChoosePanel({ onSignup, onHaveKey }) {
  return (
    <div className="login-body">
      <p className="login-prompt">
        Sign up to monitor your AI agents, or enter your existing API key.
      </p>
      <div className="login-actions">
        <button className="btn btn-primary btn-block" onClick={onSignup}>
          Sign up
        </button>
        <button className="btn btn-secondary btn-block" onClick={onHaveKey}>
          I have an API key
        </button>
      </div>
    </div>
  )
}

function SignupPanel({ onSuccess, onBack }) {
  const [email, setEmail] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  async function handleSubmit(e) {
    e.preventDefault()
    if (!email.trim()) return
    setSubmitting(true)
    setError(null)
    try {
      const res = await api.signup(email.trim())
      onSuccess(res.email, res.api_key)
    } catch (err) {
      setError(err.message)
      setSubmitting(false)
    }
  }

  return (
    <form className="login-body" onSubmit={handleSubmit}>
      <p className="login-prompt">
        Enter your email. We'll generate an API key for you.
      </p>
      <label className="field-label" htmlFor="signup-email">
        Email
      </label>
      <input
        id="signup-email"
        className="text-input"
        type="email"
        placeholder="you@company.com"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        autoFocus
        required
      />
      {error && <p className="form-error">{error}</p>}
      <div className="login-actions">
        <button
          type="submit"
          className="btn btn-primary btn-block"
          disabled={submitting || !email.trim()}
        >
          {submitting ? (
            <>
              <Spinner /> Creating account…
            </>
          ) : (
            'Create account'
          )}
        </button>
        <button
          type="button"
          className="btn btn-link"
          onClick={onBack}
          disabled={submitting}
        >
          ← Back
        </button>
      </div>
    </form>
  )
}

function ShowKeyPanel({ email, apiKey, onContinue }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard.writeText(apiKey)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard might not be available in non-secure contexts.
    }
  }
  return (
    <div className="login-body">
      <p className="login-prompt">
        Account created for <strong>{email}</strong>. Save this key — it
        won't be shown again.
      </p>
      <div className="key-display">
        <code className="key-text">{apiKey}</code>
        <button
          type="button"
          className="copy-btn-inline"
          onClick={copy}
        >
          {copied ? '✓ Copied' : 'Copy'}
        </button>
      </div>
      <div className="callout callout-warning">
        Store this somewhere safe (a password manager, a vault). If you lose
        it, you can generate a new one but the old one stays minted.
      </div>
      <div className="login-actions">
        <button className="btn btn-primary btn-block" onClick={onContinue}>
          Continue to dashboard →
        </button>
      </div>
    </div>
  )
}

function HaveKeyPanel({ onSuccess, onBack }) {
  const [key, setKey] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  async function handleSubmit(e) {
    e.preventDefault()
    const trimmed = key.trim()
    if (!trimmed) return
    setSubmitting(true)
    setError(null)
    // Set the key on the api client first, then validate by hitting a
    // protected endpoint. If validation fails we clear it.
    setApiKey(trimmed)
    try {
      const ok = await api.validateCurrentKey()
      if (ok) {
        onSuccess(trimmed)
      } else {
        setApiKey(null)
        setError('Invalid API key.')
        setSubmitting(false)
      }
    } catch (err) {
      setApiKey(null)
      setError(err.message || 'Could not validate key.')
      setSubmitting(false)
    }
  }

  return (
    <form className="login-body" onSubmit={handleSubmit}>
      <p className="login-prompt">
        Paste the API key from your Oversee account.
      </p>
      <label className="field-label" htmlFor="have-key">
        API key
      </label>
      <input
        id="have-key"
        className="text-input"
        type="text"
        placeholder="ov_sk_…"
        value={key}
        onChange={(e) => setKey(e.target.value)}
        autoComplete="off"
        spellCheck="false"
        autoFocus
        required
      />
      {error && <p className="form-error">{error}</p>}
      <div className="login-actions">
        <button
          type="submit"
          className="btn btn-primary btn-block"
          disabled={submitting || !key.trim()}
        >
          {submitting ? (
            <>
              <Spinner /> Verifying…
            </>
          ) : (
            'Continue'
          )}
        </button>
        <button
          type="button"
          className="btn btn-link"
          onClick={onBack}
          disabled={submitting}
        >
          ← Back
        </button>
      </div>
    </form>
  )
}
