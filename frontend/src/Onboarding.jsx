import { useEffect, useState } from 'react'
import { api } from './api.js'
import AddAgent from './AddAgent.jsx'
import { SparkleIcon, CheckCircleIcon } from './Icons.jsx'

// Post-signup onboarding wizard. Shown once to the org owner (gated in App.jsx
// on `!me.org.onboarded_at`). Linear steps: name → connect first agent →
// invite team (business only) → done. Finishing or skipping marks the account
// onboarded so it never reappears.
export default function Onboarding({ me, onDone }) {
  const isBusiness = me?.org?.account_type === 'business'
  const firstName = (me?.user?.name || '').trim().split(/\s+/)[0] || 'there'

  // Build the step list from account type.
  const steps = ['name', 'connect', ...(isBusiness ? ['invite'] : []), 'done']
  const [idx, setIdx] = useState(0)
  const stepKey = steps[idx]

  const [workspace, setWorkspace] = useState(me?.org?.name || '')
  const [saving, setSaving] = useState(false)
  const [agentConnected, setAgentConnected] = useState(false)
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteUrl, setInviteUrl] = useState('')
  const [inviteErr, setInviteErr] = useState('')
  const [copied, setCopied] = useState(false)
  const [finishing, setFinishing] = useState(false)

  // While on the connect step, poll for the first agent so we can show a live
  // "✓ connected" badge. Non-blocking — the user can continue regardless.
  useEffect(() => {
    if (stepKey !== 'connect' || agentConnected) return
    let alive = true
    const check = async () => {
      try {
        const list = await api.listAgents()
        if (alive && Array.isArray(list) && list.length > 0) setAgentConnected(true)
      } catch {
        /* ignore */
      }
    }
    check()
    const t = setInterval(check, 5000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [stepKey, agentConnected])

  async function finish() {
    if (finishing) return
    setFinishing(true)
    try {
      await api.completeOnboarding()
    } catch {
      /* best-effort — don't trap the user in onboarding */
    }
    onDone()
  }

  async function nameNext() {
    setSaving(true)
    try {
      if (workspace.trim()) await api.updateOrg({ name: workspace.trim() })
    } catch {
      /* ignore — name is optional */
    } finally {
      setSaving(false)
    }
    setIdx((i) => i + 1)
  }

  async function sendInvite() {
    const email = inviteEmail.trim()
    if (!email) return
    setInviteErr('')
    setSaving(true)
    try {
      const res = await api.createInvite({ email, role: 'member' })
      setInviteUrl(res.invite_url || '')
      setInviteEmail('')
    } catch (e) {
      setInviteErr(e?.message?.includes('400') ? 'Could not create invite.' : 'Something went wrong.')
    } finally {
      setSaving(false)
    }
  }

  async function copyInvite() {
    try {
      await navigator.clipboard.writeText(inviteUrl)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* ignore */
    }
  }

  const back = idx > 0 && stepKey !== 'done' ? () => setIdx((i) => i - 1) : null
  const next = () => setIdx((i) => i + 1)

  return (
    <div className="onboard">
      <div className="onboard-top">
        <span className="onboard-brand">
          <span className="onboard-dot" /> oversee
        </span>
        <button type="button" className="onboard-skip-all" onClick={finish} disabled={finishing}>
          Skip setup
        </button>
      </div>

      <div className="onboard-card">
        <div className="onboard-dots" aria-hidden="true">
          {steps.map((s, i) => (
            <span key={s} className={`onboard-dot-step ${i <= idx ? 'is-on' : ''}`} />
          ))}
        </div>

        {stepKey === 'name' && (
          <div className="onboard-step">
            <div className="onboard-mark"><SparkleIcon size={22} /></div>
            <h1 className="onboard-title">Welcome, {firstName}.</h1>
            <p className="onboard-sub">Let’s get Oversee set up. First, name your workspace.</p>
            <label className="onboard-field">
              <span>Workspace name</span>
              <input
                className="text-input"
                value={workspace}
                onChange={(e) => setWorkspace(e.target.value)}
                placeholder="e.g. Hammocks.com"
                autoFocus
              />
            </label>
            <div className="onboard-foot">
              <span />
              <div className="onboard-foot-right">
                <button type="button" className="btn btn-link" onClick={next}>Skip</button>
                <button type="button" className="btn btn-primary" onClick={nameNext} disabled={saving}>
                  {saving ? 'Saving…' : 'Continue'}
                </button>
              </div>
            </div>
          </div>
        )}

        {stepKey === 'connect' && (
          <div className="onboard-step">
            <h1 className="onboard-title">Connect your first agent</h1>
            <p className="onboard-sub">
              Pick your platform and follow the steps. Telemetry starts flowing once your
              agent runs — you can keep setting up in the meantime.
            </p>
            {agentConnected && (
              <div className="onboard-connected">
                <CheckCircleIcon size={16} /> First agent connected — nice.
              </div>
            )}
            <div className="onboard-addagent">
              <AddAgent embedded />
            </div>
            <div className="onboard-foot">
              {back ? (
                <button type="button" className="btn btn-link" onClick={back}>← Back</button>
              ) : <span />}
              <div className="onboard-foot-right">
                <button type="button" className="btn btn-link" onClick={next}>
                  Skip — I’ll connect later
                </button>
                <button type="button" className={`btn ${agentConnected ? 'btn-primary' : 'btn-secondary'}`} onClick={next}>
                  Continue
                </button>
              </div>
            </div>
          </div>
        )}

        {stepKey === 'invite' && (
          <div className="onboard-step">
            <h1 className="onboard-title">Invite your team</h1>
            <p className="onboard-sub">
              Send teammates a link to join this workspace. You can always add more later in Settings.
            </p>
            <label className="onboard-field">
              <span>Teammate email</span>
              <div className="onboard-invite-row">
                <input
                  className="text-input"
                  type="email"
                  value={inviteEmail}
                  onChange={(e) => setInviteEmail(e.target.value)}
                  placeholder="teammate@company.com"
                  onKeyDown={(e) => e.key === 'Enter' && sendInvite()}
                />
                <button type="button" className="btn btn-secondary" onClick={sendInvite} disabled={saving || !inviteEmail.trim()}>
                  {saving ? '…' : 'Create link'}
                </button>
              </div>
            </label>
            {inviteErr && <p className="form-error">{inviteErr}</p>}
            {inviteUrl && (
              <div className="onboard-invite-link">
                <code>{inviteUrl}</code>
                <button type="button" className="copy-btn-inline" onClick={copyInvite}>
                  {copied ? '✓ Copied' : 'Copy'}
                </button>
              </div>
            )}
            <div className="onboard-foot">
              {back ? (
                <button type="button" className="btn btn-link" onClick={back}>← Back</button>
              ) : <span />}
              <div className="onboard-foot-right">
                <button type="button" className="btn btn-link" onClick={next}>Skip</button>
                <button type="button" className="btn btn-primary" onClick={next}>Continue</button>
              </div>
            </div>
          </div>
        )}

        {stepKey === 'done' && (
          <div className="onboard-step onboard-done">
            <div className="onboard-mark"><CheckCircleIcon size={22} /></div>
            <h1 className="onboard-title">You’re all set.</h1>
            <p className="onboard-sub">
              Your dashboard fills in automatically as your agents send telemetry.
              Oversee tracks costs for you — set a budget limit anytime in Settings.
            </p>
            <button type="button" className="btn btn-primary onboard-done-btn" onClick={finish} disabled={finishing}>
              {finishing ? 'Finishing…' : 'Go to dashboard'}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
