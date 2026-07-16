import { useEffect, useState } from 'react'
import { api } from './api.js'
import { Spinner } from './ui.jsx'
import { relativeTime } from './utils.js'
import { ArrowLeftIcon, TrashIcon } from './Icons.jsx'

// Organization + account settings. Reachable from the account-badge dropdown.
//   - Everyone: org info + change-password.
//   - Business owners: members list, invite links, remove members.

export default function Settings({ me, onClose, onUpdated, onUpgrade }) {
  const user = me?.user
  const org = me?.org
  const isOwner = user?.role === 'owner'
  const isBusiness = org?.account_type === 'business'

  return (
    <div className="view settings-view">
      <header className="settings-header">
        <button type="button" className="back-btn" onClick={onClose}>
          <ArrowLeftIcon /> Back
        </button>
        <h2 className="section-label">Settings</h2>
      </header>

      <section className="settings-card">
        <h3 className="settings-card-title">Organization</h3>
        <div className="settings-rows">
          <Row label="Name" value={org?.name || org?.email || '—'} />
          <Row
            label="Type"
            value={<span className="org-type-badge">{org?.account_type}</span>}
          />
          <Row label="Owner email" value={org?.email} />
        </div>
      </section>

      <BillingCard onUpgrade={onUpgrade} />

      {user && <AlertsCard />}

      {user && <PasswordCard onUpdated={onUpdated} />}

      {isOwner && <ApiKeyCard />}

      {isBusiness && user && (
        <MembersCard isOwner={isOwner} currentUserId={user.id} />
      )}

      {!isBusiness && user && (
        <section className="settings-card">
          <h3 className="settings-card-title">Team members</h3>
          <p className="settings-note">
            Individual accounts are just you. Upgrade to a Business account to
            invite teammates with their own logins into the same workspace.
          </p>
        </section>
      )}
    </div>
  )
}

function Row({ label, value }) {
  return (
    <div className="settings-row">
      <span className="settings-row-label">{label}</span>
      <span className="settings-row-value">{value}</span>
    </div>
  )
}

// Billing & plan: current tier + agent usage. Free accounts get an "Upgrade"
// button (opens the plan-picker modal → Stripe Checkout). Paid accounts get
// "Manage billing" → the Stripe Customer Portal (upgrade/downgrade/cancel/
// invoices/payment method — all hosted by Stripe).
function BillingCard({ onUpgrade }) {
  const [usage, setUsage] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    api.getAccountUsage().then((u) => alive && setUsage(u)).catch(() => {})
    return () => { alive = false }
  }, [])

  const plan = usage?.plan || 'free'
  const isFree = plan === 'free'
  const limit = usage?.agent_limit
  const count = usage?.agent_count ?? 0
  const usageText = limit == null ? `${count} agents · unlimited` : `${count} of ${limit} agents`

  async function manage() {
    setError(null)
    setBusy(true)
    try {
      const res = await api.billingPortal()
      if (res?.portal_url) {
        window.location.href = res.portal_url
        return
      }
      setError('Could not open the billing portal.')
    } catch (e) {
      // The status is on e.status (api.js); e.message is the server's detail text.
      if (e?.status === 400) {
        // No Stripe customer yet → there's nothing to manage; go to checkout.
        onUpgrade?.()
      } else if (e?.status === 503 || /not configured/i.test(String(e?.message || ''))) {
        setError('Billing isn’t available just yet.')
      } else {
        setError('Could not open the billing portal. Please try again.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="settings-card">
      <h3 className="settings-card-title">Billing &amp; plan</h3>
      <div className="settings-rows">
        <Row label="Plan" value={<span className="org-type-badge">{plan}</span>} />
        <Row label="Agents" value={usage ? usageText : '…'} />
      </div>
      <p className="settings-note">
        {isFree
          ? 'You’re on the Free plan. Upgrade to view more of your fleet — every agent keeps recording regardless of plan.'
          : 'Manage your subscription, payment method, and invoices, or cancel anytime.'}
      </p>
      <div style={{ marginTop: 12 }}>
        {isFree ? (
          <button type="button" className="btn btn-primary" onClick={() => onUpgrade?.()}>
            Upgrade plan
          </button>
        ) : (
          <button type="button" className="btn btn-secondary" onClick={manage} disabled={busy}>
            {busy ? 'Opening…' : 'Manage billing'}
          </button>
        )}
      </div>
      {error && (
        <p className="settings-note" style={{ color: 'var(--error)', marginTop: 10 }}>{error}</p>
      )}
    </section>
  )
}

// Proactive alerts: which conditions push a notification, over which channels.
// The sweep runs server-side every ~15 min — nobody has to be watching the
// dashboard. Email goes to the account owner; Slack/webhook are opt-in URLs.
function AlertToggle({ label, hint, checked, onChange }) {
  return (
    <label className="alert-toggle">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="alert-toggle-text">
        <span className="alert-toggle-label">{label}</span>
        {hint && <span className="alert-toggle-hint">{hint}</span>}
      </span>
    </label>
  )
}

function AlertsCard() {
  const [s, setS] = useState(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState(null)
  const [testResult, setTestResult] = useState(null)
  const [testing, setTesting] = useState(false)

  useEffect(() => {
    let alive = true
    api.getAlerts().then((r) => alive && setS(r)).catch(() => alive && setError('Could not load alert settings.'))
    return () => { alive = false }
  }, [])

  function set(field, value) {
    setS((prev) => ({ ...prev, [field]: value }))
    setSaved(false)
  }

  async function save() {
    if (!s) return
    setSaving(true); setError(null); setSaved(false)
    try {
      const next = await api.updateAlerts(s)
      setS(next)
      setSaved(true)
    } catch (e) {
      setError(e.message || 'Could not save alert settings.')
    } finally {
      setSaving(false)
    }
  }

  async function sendTest() {
    setTesting(true); setTestResult(null)
    try {
      const r = await api.testAlert()
      setTestResult(
        r?.delivered
          ? `Test sent over: ${(r.channels || []).join(', ')}`
          : `Not sent — ${r?.reason || 'no channels enabled'}.`,
      )
    } catch (e) {
      setTestResult(e.message || 'Could not send a test.')
    } finally {
      setTesting(false)
    }
  }

  if (!s) {
    return (
      <section className="settings-card">
        <h3 className="settings-card-title">Alerts</h3>
        <div className="settings-note">{error || <><Spinner /> Loading…</>}</div>
      </section>
    )
  }

  return (
    <section className="settings-card">
      <h3 className="settings-card-title">Alerts</h3>
      <p className="settings-note">
        Trovis watches your fleet in the background and pushes an alert the
        moment something trips — so you find out without checking the dashboard.
      </p>

      <h4 className="settings-subtitle">What to alert on</h4>
      <div className="alert-toggles">
        <AlertToggle label="Behavioral drift" hint="an agent steps outside its declared job"
          checked={s.rule_drift} onChange={(v) => set('rule_drift', v)} />
        <AlertToggle label="Budget threshold" hint={`spend crosses ${s.budget_warn_pct}% and 100% of your monthly budget`}
          checked={s.rule_budget} onChange={(v) => set('rule_budget', v)} />
        <AlertToggle label="Runaway loop" hint={`one operation repeats ${s.loop_threshold}+ times in 30 min`}
          checked={s.rule_loop} onChange={(v) => set('rule_loop', v)} />
        <AlertToggle label="Repeated failures" hint="an agent errors several times in a short window"
          checked={s.rule_error} onChange={(v) => set('rule_error', v)} />
      </div>

      <h4 className="settings-subtitle">Thresholds</h4>
      <div className="alert-thresholds">
        <label className="field-label">Budget warning at (%)</label>
        <input className="text-input" type="number" min="1" max="100" value={s.budget_warn_pct}
          onChange={(e) => set('budget_warn_pct', Number(e.target.value))} />
        <label className="field-label">Loop trip count</label>
        <input className="text-input" type="number" min="2" value={s.loop_threshold}
          onChange={(e) => set('loop_threshold', Number(e.target.value))} />
      </div>

      <h4 className="settings-subtitle">Where to send them</h4>
      <div className="alert-channels">
        <AlertToggle label="Email" hint="to the account owner"
          checked={s.email_enabled} onChange={(v) => set('email_enabled', v)} />
        <label className="field-label">Slack incoming webhook URL</label>
        <input className="text-input" type="url" placeholder="https://hooks.slack.com/services/…"
          value={s.slack_webhook_url || ''} onChange={(e) => set('slack_webhook_url', e.target.value)} />
        <label className="field-label">Custom webhook URL</label>
        <input className="text-input" type="url" placeholder="https://your-endpoint.example.com/hook"
          value={s.webhook_url || ''} onChange={(e) => set('webhook_url', e.target.value)} />
      </div>

      {error && <p className="form-error" style={{ marginTop: 10 }}>{error}</p>}
      {saved && <p className="settings-success" style={{ marginTop: 10 }}>Saved.</p>}
      {testResult && <p className="settings-note" style={{ marginTop: 10 }}>{testResult}</p>}

      <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
        <button type="button" className="btn btn-primary btn-sm" onClick={save} disabled={saving}>
          {saving ? <><Spinner /> Saving…</> : 'Save changes'}
        </button>
        <button type="button" className="btn btn-secondary btn-sm" onClick={sendTest} disabled={testing}>
          {testing ? 'Sending…' : 'Send test alert'}
        </button>
      </div>
    </section>
  )
}

function PasswordCard({ onUpdated }) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [done, setDone] = useState(false)

  async function submit(e) {
    e.preventDefault()
    if (next.length < 10) {
      setError('New password must be at least 10 characters.')
      return
    }
    setSaving(true)
    setError(null)
    setDone(false)
    try {
      await api.setPassword({ current_password: current || null, new_password: next })
      setCurrent('')
      setNext('')
      setDone(true)
      onUpdated?.()
    } catch (err) {
      setError(err.message || 'Could not update password')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="settings-card">
      <h3 className="settings-card-title">Password</h3>
      <form className="settings-form" onSubmit={submit}>
        <label className="field-label">Current password</label>
        <input
          className="text-input"
          type="password"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
          placeholder="Leave blank if you haven't set one"
        />
        <label className="field-label">New password</label>
        <input
          className="text-input"
          type="password"
          value={next}
          onChange={(e) => setNext(e.target.value)}
          placeholder="At least 10 characters"
        />
        {error && <p className="form-error">{error}</p>}
        {done && <p className="settings-success">Password updated.</p>}
        <div>
          <button type="submit" className="btn btn-primary btn-sm" disabled={saving || !next}>
            {saving ? <><Spinner /> Saving…</> : 'Update password'}
          </button>
        </div>
      </form>
    </section>
  )
}

function ApiKeyCard() {
  const [revealing, setRevealing] = useState(false)
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const [keys, setKeys] = useState(null)
  const [copied, setCopied] = useState(null)

  async function reveal(e) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      const res = await api.revealApiKeys(password)
      setKeys(res.keys || [])
      setPassword('')
      setRevealing(false)
    } catch (err) {
      setError(err.message || 'Could not reveal key')
    } finally {
      setSubmitting(false)
    }
  }

  async function copy(key) {
    try {
      await navigator.clipboard.writeText(key)
      setCopied(key)
      setTimeout(() => setCopied(null), 1500)
    } catch {
      /* clipboard unavailable */
    }
  }

  return (
    <section className="settings-card">
      <h3 className="settings-card-title">API key</h3>
      <p className="settings-note">
        Use this to connect agents (the <code>api_key</code> in
        <code> trovis.init()</code>). It's a long-lived credential —
        revealing it requires your password.
      </p>

      {keys ? (
        keys.length === 0 ? (
          <p className="settings-note" style={{ marginTop: 12 }}>
            No active API keys on this account.
          </p>
        ) : (
          <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
            {keys.map((k) => (
              <div className="key-display" key={k.key}>
                <code className="key-text">{k.key}</code>
                <button type="button" className="copy-btn-inline" onClick={() => copy(k.key)}>
                  {copied === k.key ? '✓ Copied' : 'Copy'}
                </button>
              </div>
            ))}
            <button type="button" className="btn btn-link btn-sm" onClick={() => setKeys(null)} style={{ alignSelf: 'flex-start' }}>
              Hide
            </button>
          </div>
        )
      ) : revealing ? (
        <form className="settings-form" onSubmit={reveal} style={{ marginTop: 12 }}>
          <label className="field-label">Confirm your password</label>
          <input
            className="text-input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoFocus
          />
          {error && <p className="form-error">{error}</p>}
          <div style={{ display: 'flex', gap: 8 }}>
            <button type="submit" className="btn btn-primary btn-sm" disabled={submitting || !password}>
              {submitting ? <><Spinner /> Revealing…</> : 'Reveal key'}
            </button>
            <button type="button" className="btn btn-link btn-sm" onClick={() => { setRevealing(false); setError(null); setPassword('') }}>
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <div style={{ marginTop: 12 }}>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => setRevealing(true)}>
            Reveal API key
          </button>
        </div>
      )}
    </section>
  )
}

function MembersCard({ isOwner, currentUserId }) {
  const [members, setMembers] = useState([])
  const [invites, setInvites] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  async function reload() {
    try {
      const [m, inv] = await Promise.all([
        api.getMembers(),
        isOwner ? api.getInvites() : Promise.resolve([]),
      ])
      setMembers(m || [])
      setInvites(inv || [])
    } catch (e) {
      setError(e.message || 'Could not load members')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function removeMember(id) {
    try {
      await api.deleteMember(id)
      reload()
    } catch (e) {
      setError(e.message || 'Could not remove member')
    }
  }
  async function revoke(id) {
    try {
      await api.revokeInvite(id)
      reload()
    } catch (e) {
      setError(e.message || 'Could not revoke invite')
    }
  }

  return (
    <section className="settings-card">
      <h3 className="settings-card-title">Members</h3>
      {error && <p className="form-error">{error}</p>}
      {loading ? (
        <div className="settings-note"><Spinner /> Loading…</div>
      ) : (
        <>
          <ul className="member-list">
            {members.map((m) => (
              <li key={m.id} className="member-row">
                <span className="member-avatar">
                  {(m.name || m.email).slice(0, 1).toUpperCase()}
                </span>
                <span className="member-main">
                  <span className="member-name">{m.name || m.email}</span>
                  <span className="member-email">{m.email}</span>
                </span>
                <span className={`role-badge role-${m.role}`}>{m.role}</span>
                {isOwner && m.id !== currentUserId && (
                  <button
                    type="button"
                    className="btn-icon-sm"
                    title="Remove member"
                    onClick={() => removeMember(m.id)}
                  >
                    <TrashIcon />
                  </button>
                )}
              </li>
            ))}
          </ul>

          {isOwner && <InviteForm onInvited={reload} />}

          {isOwner && invites.length > 0 && (
            <div className="invite-pending">
              <h4 className="settings-subtitle">Pending invites</h4>
              <ul className="member-list">
                {invites.map((i) => (
                  <li key={i.id} className="member-row">
                    <span className="member-main">
                      <span className="member-name">{i.email}</span>
                      <span className="member-email">
                        invited {relativeTime(i.created_at)} · {i.role}
                      </span>
                    </span>
                    <button type="button" className="btn btn-link btn-sm" onClick={() => revoke(i.id)}>
                      Revoke
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </section>
  )
}

function InviteForm({ onInvited }) {
  const [email, setEmail] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const [link, setLink] = useState(null)
  const [copied, setCopied] = useState(false)

  async function submit(e) {
    e.preventDefault()
    if (!email.trim()) return
    setSubmitting(true)
    setError(null)
    setLink(null)
    try {
      const res = await api.createInvite({ email: email.trim(), role: 'member' })
      setLink(res.invite_url)
      setEmail('')
      onInvited?.()
    } catch (err) {
      setError(err.message || 'Could not create invite')
    } finally {
      setSubmitting(false)
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(link)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* clipboard unavailable */
    }
  }

  return (
    <div className="invite-form-wrap">
      <form className="invite-form" onSubmit={submit}>
        <input
          className="text-input"
          type="email"
          placeholder="teammate@company.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <button type="submit" className="btn btn-secondary btn-sm" disabled={submitting || !email.trim()}>
          {submitting ? <><Spinner /> …</> : 'Invite'}
        </button>
      </form>
      {error && <p className="form-error">{error}</p>}
      {link && (
        <div className="invite-link-out">
          <p className="settings-note">
            Share this one-time link with your teammate — it expires in 7 days:
          </p>
          <div className="key-display">
            <code className="key-text">{link}</code>
            <button type="button" className="copy-btn-inline" onClick={copy}>
              {copied ? '✓ Copied' : 'Copy'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
