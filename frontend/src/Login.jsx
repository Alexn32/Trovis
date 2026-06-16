import { useState } from 'react'
import { api, setApiKey, setSessionToken } from './api.js'
import { Spinner } from './ui.jsx'
import { TrovisLogo } from './Icons.jsx'

// Auth screen. Real logins now: email + password → a session token. Modes:
//   choose → login | signup | claim
//   signup → show-key (shows the initial agent API key once) → done
//   accept-invite (entered directly via a /accept-invite?token=… deep link)
// On success we call onAuthed({ user, org, auth }) after the api client has
// the session token set.

export default function Login({ onAuthed, initialMode = 'choose', inviteToken = null, onBackToLanding = null }) {
  const [mode, setMode] = useState(initialMode)
  const [fresh, setFresh] = useState(null) // { email, apiKey, me } after signup

  function finish(res) {
    // res = { token, user, org } from signup/login/claim/accept-invite
    setSessionToken(res.token)
    onAuthed({ user: res.user, org: res.org, auth: 'session' })
  }

  function handleSignup(res) {
    setSessionToken(res.token)
    if (res.api_key) setApiKey(res.api_key)
    setFresh({ email: res.user.email, apiKey: res.api_key, me: { user: res.user, org: res.org, auth: 'session' } })
    setMode('show-key')
  }

  return (
    <div className="login-shell">
      <div className="login-card">
        {onBackToLanding && mode !== 'show-key' && (
          <button
            type="button"
            className="btn btn-link login-back-home"
            onClick={onBackToLanding}
          >
            ← Back to home
          </button>
        )}
        <header className="login-header">
          <TrovisLogo />
        </header>

        {mode === 'choose' && (
          <ChoosePanel
            onLogin={() => setMode('login')}
            onSignup={() => setMode('signup')}
            onClaim={() => setMode('claim')}
          />
        )}
        {mode === 'login' && (
          <LoginPanel onSuccess={finish} onBack={() => setMode('choose')} />
        )}
        {mode === 'signup' && (
          <SignupPanel onSuccess={handleSignup} onBack={() => setMode('choose')} />
        )}
        {mode === 'claim' && (
          <ClaimPanel onSuccess={finish} onBack={() => setMode('choose')} />
        )}
        {mode === 'accept-invite' && (
          <AcceptInvitePanel token={inviteToken} onSuccess={finish} />
        )}
        {mode === 'show-key' && (
          <ShowKeyPanel
            email={fresh.email}
            apiKey={fresh.apiKey}
            onContinue={() => onAuthed(fresh.me)}
          />
        )}
      </div>
    </div>
  )
}

function ChoosePanel({ onLogin, onSignup, onClaim }) {
  return (
    <div className="login-body">
      <p className="login-prompt">
        Log in to monitor your AI agents, or create a new account.
      </p>
      <div className="login-actions">
        <button className="btn btn-primary btn-block" onClick={onLogin}>
          Log in
        </button>
        <button className="btn btn-secondary btn-block" onClick={onSignup}>
          Create an account
        </button>
        <button className="btn btn-link" onClick={onClaim}>
          Have an API key? Claim your account
        </button>
      </div>
    </div>
  )
}

function LoginPanel({ onSuccess, onBack }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  async function handleSubmit(e) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      onSuccess(await api.login({ email: email.trim(), password }))
    } catch (err) {
      setError(err.message)
      setSubmitting(false)
    }
  }

  return (
    <form className="login-body" onSubmit={handleSubmit}>
      <p className="login-prompt">Welcome back.</p>
      <label className="field-label">Email</label>
      <input
        className="text-input"
        type="email"
        placeholder="you@company.com"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        autoFocus
        required
      />
      <label className="field-label">Password</label>
      <input
        className="text-input"
        type="password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        required
      />
      {error && <p className="form-error">{error}</p>}
      <div className="login-actions">
        <button type="submit" className="btn btn-primary btn-block" disabled={submitting || !email.trim() || !password}>
          {submitting ? <><Spinner /> Logging in…</> : 'Log in'}
        </button>
        <button type="button" className="btn btn-link" onClick={onBack} disabled={submitting}>
          ← Back
        </button>
      </div>
    </form>
  )
}

function SignupPanel({ onSuccess, onBack }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [accountType, setAccountType] = useState('individual')
  const [orgName, setOrgName] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  async function handleSubmit(e) {
    e.preventDefault()
    if (password.length < 10) {
      setError('Password must be at least 10 characters.')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      onSuccess(
        await api.signup({
          email: email.trim(),
          password,
          name: name.trim() || null,
          account_type: accountType,
          org_name: accountType === 'business' ? orgName.trim() || null : null,
        }),
      )
    } catch (err) {
      setError(err.message)
      setSubmitting(false)
    }
  }

  return (
    <form className="login-body" onSubmit={handleSubmit}>
      <p className="login-prompt">Create your Trovis account.</p>

      <label className="field-label">Account type</label>
      <div className="auth-type-toggle">
        <button
          type="button"
          className={`auth-type-option ${accountType === 'individual' ? 'is-active' : ''}`}
          onClick={() => setAccountType('individual')}
        >
          <strong>Individual</strong>
          <span>Just you</span>
        </button>
        <button
          type="button"
          className={`auth-type-option ${accountType === 'business' ? 'is-active' : ''}`}
          onClick={() => setAccountType('business')}
        >
          <strong>Business</strong>
          <span>Invite your team</span>
        </button>
      </div>

      <label className="field-label">Your name</label>
      <input className="text-input" type="text" placeholder="Alex Nielsen" value={name} onChange={(e) => setName(e.target.value)} />

      {accountType === 'business' && (
        <>
          <label className="field-label">Organization name</label>
          <input className="text-input" type="text" placeholder="Acme Inc." value={orgName} onChange={(e) => setOrgName(e.target.value)} />
        </>
      )}

      <label className="field-label">Email</label>
      <input className="text-input" type="email" placeholder="you@company.com" value={email} onChange={(e) => setEmail(e.target.value)} required />

      <label className="field-label">Password</label>
      <input className="text-input" type="password" placeholder="At least 10 characters" value={password} onChange={(e) => setPassword(e.target.value)} required />

      {error && <p className="form-error">{error}</p>}
      <div className="login-actions">
        <button type="submit" className="btn btn-primary btn-block" disabled={submitting || !email.trim() || !password}>
          {submitting ? <><Spinner /> Creating account…</> : 'Create account'}
        </button>
        <button type="button" className="btn btn-link" onClick={onBack} disabled={submitting}>
          ← Back
        </button>
      </div>
    </form>
  )
}

function ClaimPanel({ onSuccess, onBack }) {
  const [apiKey, setApiKeyVal] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  async function handleSubmit(e) {
    e.preventDefault()
    if (password.length < 10) {
      setError('Password must be at least 10 characters.')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      onSuccess(
        await api.claim({
          api_key: apiKey.trim(),
          email: email.trim(),
          password,
          name: name.trim() || null,
        }),
      )
    } catch (err) {
      setError(err.message)
      setSubmitting(false)
    }
  }

  return (
    <form className="login-body" onSubmit={handleSubmit}>
      <p className="login-prompt">
        Claim an existing account: paste its API key, then set a password to
        log in with email going forward. (Your agents keep using the key.)
      </p>
      <label className="field-label">API key</label>
      <input className="text-input" type="text" placeholder="ov_sk_…" value={apiKey} onChange={(e) => setApiKeyVal(e.target.value)} autoComplete="off" spellCheck="false" required />
      <label className="field-label">Your name</label>
      <input className="text-input" type="text" placeholder="Alex Nielsen" value={name} onChange={(e) => setName(e.target.value)} />
      <label className="field-label">Email</label>
      <input className="text-input" type="email" placeholder="you@company.com" value={email} onChange={(e) => setEmail(e.target.value)} required />
      <label className="field-label">Password</label>
      <input className="text-input" type="password" placeholder="At least 10 characters" value={password} onChange={(e) => setPassword(e.target.value)} required />
      {error && <p className="form-error">{error}</p>}
      <div className="login-actions">
        <button type="submit" className="btn btn-primary btn-block" disabled={submitting || !apiKey.trim() || !email.trim() || !password}>
          {submitting ? <><Spinner /> Claiming…</> : 'Claim account'}
        </button>
        <button type="button" className="btn btn-link" onClick={onBack} disabled={submitting}>
          ← Back
        </button>
      </div>
    </form>
  )
}

function AcceptInvitePanel({ token, onSuccess }) {
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  async function handleSubmit(e) {
    e.preventDefault()
    if (password.length < 10) {
      setError('Password must be at least 10 characters.')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      onSuccess(await api.acceptInvite({ token, name: name.trim() || null, password }))
    } catch (err) {
      setError(err.message)
      setSubmitting(false)
    }
  }

  if (!token) {
    return (
      <div className="login-body">
        <p className="login-prompt">This invite link is missing its token.</p>
      </div>
    )
  }

  return (
    <form className="login-body" onSubmit={handleSubmit}>
      <p className="login-prompt">You've been invited to a team on Trovis. Set up your login.</p>
      <label className="field-label">Your name</label>
      <input className="text-input" type="text" placeholder="Alex Nielsen" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      <label className="field-label">Password</label>
      <input className="text-input" type="password" placeholder="At least 10 characters" value={password} onChange={(e) => setPassword(e.target.value)} required />
      {error && <p className="form-error">{error}</p>}
      <div className="login-actions">
        <button type="submit" className="btn btn-primary btn-block" disabled={submitting || !password}>
          {submitting ? <><Spinner /> Joining…</> : 'Join the team'}
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
      // clipboard unavailable in non-secure contexts
    }
  }
  return (
    <div className="login-body">
      <p className="login-prompt">
        Account created for <strong>{email}</strong>. Here's your API key for
        connecting agents. You can also reveal it later from Settings.
      </p>
      <div className="key-display">
        <code className="key-text">{apiKey}</code>
        <button type="button" className="copy-btn-inline" onClick={copy}>
          {copied ? '✓ Copied' : 'Copy'}
        </button>
      </div>
      <div className="callout callout-warning">
        Store this somewhere safe. You log in with your email + password; the
        API key is only for connecting agents.
      </div>
      <div className="login-actions">
        <button className="btn btn-primary btn-block" onClick={onContinue}>
          Continue to dashboard →
        </button>
      </div>
    </div>
  )
}
