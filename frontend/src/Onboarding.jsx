import { useEffect, useState } from 'react'
import { api } from './api.js'
import AddAgent from './AddAgent.jsx'
import { TrovisMark, CheckCircleIcon, TrovisLogo } from './Icons.jsx'
import { WorksWithStrip } from './BrandMarks.jsx'

// Post-signup onboarding wizard. Shown once to the org owner (gated in App.jsx
// on `!me.org.onboarded_at`). Finishing or skipping marks the account
// onboarded so it never reappears.
//
// Two paths, chosen at signup:
//
//   Path A (Individual) — name → connect → done. No chart, ever. A one-seat
//     workspace does not need an org structure to be useful, and demanding
//     one before the product does anything would be a tax on the person
//     least able to pay it. They graduate later, from the soft banner on
//     Home or from Settings — never from Org, which is the one surface a
//     solo user has no reason to open.
//
//   Path B (Company) — name → chart → connect → invite → done. The chart step
//     is deliberately minimal: one root role and one report is a usable
//     company. Anything more is easier to do on the Org page, with the whole
//     tree visible, than in a wizard.
//
// Every step is skippable. An org that skips the chart lands with no roles,
// which is exactly the state an existing org is in — everyone gets the full
// seat until someone draws one.
export default function Onboarding({ me, onDone }) {
  const isBusiness = me?.org?.account_type === 'business'
  const firstName = (me?.user?.name || '').trim().split(/\s+/)[0] || 'there'

  // Build the step list from account type.
  const steps = isBusiness
    ? ['name', 'chart', 'connect', 'invite', 'done']
    : ['name', 'connect', 'done']
  const [idx, setIdx] = useState(0)
  const stepKey = steps[idx]

  const [workspace, setWorkspace] = useState(me?.org?.name || '')
  const [saving, setSaving] = useState(false)
  const [agentConnected, setAgentConnected] = useState(false)
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteUrl, setInviteUrl] = useState('')
  const [inviteErr, setInviteErr] = useState('')
  // Chart step (Path B only): the roles sketched so far, and the presets to
  // hang on them. Loaded lazily so Path A never pays for it.
  const [levels, setLevels] = useState([])
  const [roles, setRoles] = useState([])
  const [roleTitle, setRoleTitle] = useState('')
  const [roleLevel, setRoleLevel] = useState('')
  const [roleParent, setRoleParent] = useState('')
  const [chartErr, setChartErr] = useState('')
  // Which role a teammate is invited into. '' = no role: the invite still
  // works, the person just lands unplaced (and on the full seat) until
  // someone puts them in a box.
  const [inviteRole, setInviteRole] = useState('')
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

  // Presets + any roles already drawn, for the chart and invite steps.
  useEffect(() => {
    if (!isBusiness || (stepKey !== 'chart' && stepKey !== 'invite')) return
    let alive = true
    Promise.all([api.getScopeLevels(), api.getOrgChart()])
      .then(([lv, chart]) => {
        if (!alive) return
        setLevels(lv || [])
        setRoles(chart?.roles || [])
      })
      .catch(() => {
        /* fail soft — the wizard must never trap someone */
      })
    return () => {
      alive = false
    }
  }, [isBusiness, stepKey])

  async function addRole() {
    const title = roleTitle.trim()
    if (!title) return
    setChartErr('')
    setSaving(true)
    try {
      const created = await api.createRole({
        title,
        parent_role_id: roleParent === '' ? null : Number(roleParent),
        scope_level_id: roleLevel === '' ? null : Number(roleLevel),
      })
      setRoles((prev) => [...prev, created])
      setRoleTitle('')
      setRoleLevel('')
      // Default the next role under the one just added: the common shape is
      // a root then its reports, and re-picking the parent every time is the
      // step's main friction.
      setRoleParent(String(created.id))
    } catch (e) {
      setChartErr(e?.message || 'Could not add that role.')
    } finally {
      setSaving(false)
    }
  }

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
      const res = await api.createInvite({
        email,
        role: 'member',
        role_id: inviteRole === '' ? null : Number(inviteRole),
      })
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
        <TrovisLogo />
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
            <div className="onboard-mark"><TrovisMark size={22} /></div>
            <h1 className="onboard-title">Welcome, {firstName}.</h1>
            <p className="onboard-sub">Let’s get Trovis set up. First, name your workspace.</p>
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

        {stepKey === 'chart' && (
          <div className="onboard-step">
            <h1 className="onboard-title">Sketch your company</h1>
            <p className="onboard-sub">
              Add a role or two — who reports to whom. A role decides how much
              of the work the person in it sees. You can finish the chart on
              the Org page later; one role is enough to start.
            </p>

            {roles.length > 0 && (
              <ul className="onboard-chart-list">
                {roles.map((r) => (
                  <li key={r.id} className="onboard-chart-row">
                    <span className="onboard-chart-title">{r.title}</span>
                    <span className="onboard-chart-sub">
                      {r.parent_role_id
                        ? `reports to ${
                            roles.find((p) => p.id === r.parent_role_id)?.title || '—'
                          }`
                        : 'top of the chart'}
                      {r.scope_level_name ? ` · ${r.scope_level_name}` : ''}
                    </span>
                  </li>
                ))}
              </ul>
            )}

            <div className="onboard-chart-form">
              <input
                className="text-input"
                value={roleTitle}
                onChange={(e) => setRoleTitle(e.target.value)}
                placeholder={roles.length === 0 ? 'e.g. CEO' : 'e.g. Support Manager'}
                aria-label="Role title"
                onKeyDown={(e) => e.key === 'Enter' && addRole()}
              />
              <select
                className="text-input"
                value={roleParent}
                onChange={(e) => setRoleParent(e.target.value)}
                aria-label="Reports to"
                disabled={roles.length === 0}
              >
                <option value="">Top of the chart</option>
                {roles.map((r) => (
                  <option key={r.id} value={r.id}>
                    Reports to {r.title}
                  </option>
                ))}
              </select>
              <select
                className="text-input"
                value={roleLevel}
                onChange={(e) => setRoleLevel(e.target.value)}
                aria-label="What this role sees"
              >
                <option value="">What they see…</option>
                {levels.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={addRole}
                disabled={saving || !roleTitle.trim()}
              >
                {saving ? '…' : 'Add role'}
              </button>
            </div>
            {chartErr && <p className="form-error">{chartErr}</p>}

            <div className="onboard-foot">
              {back ? (
                <button type="button" className="btn btn-link" onClick={back}>← Back</button>
              ) : <span />}
              <div className="onboard-foot-right">
                <button type="button" className="btn btn-link" onClick={next}>
                  Skip — I’ll map it later
                </button>
                <button type="button" className="btn btn-primary" onClick={next}>
                  Continue
                </button>
              </div>
            </div>
          </div>
        )}

        {stepKey === 'connect' && (
          <div className="onboard-step">
            <h1 className="onboard-title">Connect your first agent</h1>
            <p className="onboard-sub">
              Pick a door that works today, or send traces over OpenTelemetry.
              Tools you already use are recognized when they show up in work —
              a direct connect for those is coming.
            </p>
            <WorksWithStrip className="onboard-works-with" />
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
              Send teammates a link to join this workspace. Put them in a role
              and they arrive already seeing the right slice of the work. You
              can add more later on the Org page.
            </p>
            {roles.length > 0 && (
              <label className="onboard-field">
                <span>Which role?</span>
                <select
                  className="text-input"
                  value={inviteRole}
                  onChange={(e) => setInviteRole(e.target.value)}
                >
                  <option value="">No role yet — they’ll see everything</option>
                  {roles.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.title}
                    </option>
                  ))}
                </select>
              </label>
            )}
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
              Home fills in automatically as your agents send telemetry.
              Trovis tracks costs for you — set a budget limit anytime in Settings.
            </p>
            <button type="button" className="btn btn-primary onboard-done-btn" onClick={finish} disabled={finishing}>
              {finishing ? 'Finishing…' : 'Go to Home'}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
