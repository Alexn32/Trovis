import { useState } from 'react'
import { api } from './api.js'

// Path A → Path B: turning a one-seat workspace into a company.
//
// This lives in its own file because it has to appear in more than one place,
// and the first version did not. It was offered only on the Org page, which
// is the one surface a solo user has no reason to open — the whole point of
// Path A is that they never built a chart. The invitation to grow has to
// reach someone who has never thought about org structure, so it shows up
// where they already are: Home, and Settings.
//
// It stays an invitation. An individual who ignores it keeps the entire
// product, and nothing here is a gate. Graduation touches only the org layer
// — agents, jobs, API keys and Connect all hang off account_id, which does
// not change — so the copy says so plainly: that is the actual worry someone
// has before clicking.

export function useGraduate(onGraduated) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function graduate(payload) {
    if (busy) return false
    setBusy(true)
    setError('')
    try {
      const res = await api.graduateOrg(payload)
      // The caller re-reads /auth/me. account_type, the new root role and the
      // founder's seat all change at once, and merging only the org would
      // leave nav and the Org page reading a stale seat until a reload.
      if (onGraduated) await onGraduated(res)
      return true
    } catch (e) {
      setError(e?.message || 'Could not set that up')
      return false
    } finally {
      setBusy(false)
    }
  }

  return { graduate, busy, error }
}

/**
 * The full card: a pitch that opens into a two-field form.
 *
 * `variant` only changes the chrome — 'panel' for the Org page and Settings,
 * 'banner' for Home, where it sits above the day's work and has to stay
 * quiet. The words and the behavior are identical everywhere; a CTA that
 * argued differently depending on the page would be two features.
 */
export default function GraduateCard({
  orgName = '',
  onGraduated,
  variant = 'panel',
  onDismiss = null,
}) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState(orgName)
  const [rootTitle, setRootTitle] = useState('Founder')
  const { graduate, busy, error } = useGraduate(onGraduated)

  if (!open) {
    return (
      <div className={`org-graduate org-graduate-${variant}`}>
        <div className="org-graduate-main">
          <h3 className="org-graduate-title">Working with other people?</h3>
          <p className="org-graduate-sub">
            Turn this workspace into a company: invite colleagues, map who
            reports to whom, and decide how much of the work each person sees.
            Your agents, work and connections all stay exactly as they are.
          </p>
        </div>
        <div className="org-graduate-actions">
          <button type="button" className="btn btn-primary" onClick={() => setOpen(true)}>
            Invite your company
          </button>
          {onDismiss && (
            <button type="button" className="btn btn-link" onClick={onDismiss}>
              Not now
            </button>
          )}
        </div>
      </div>
    )
  }

  return (
    <form
      className={`org-graduate org-graduate-${variant} is-open`}
      onSubmit={async (e) => {
        e.preventDefault()
        const ok = await graduate({
          org_name: name.trim() || null,
          root_role_title: rootTitle.trim() || null,
        })
        if (ok) setOpen(false)
      }}
    >
      <h3 className="org-graduate-title">Set up your company</h3>
      <div className="org-inline-form">
        <input
          className="text-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Company name"
          aria-label="Company name"
          disabled={busy}
        />
        <input
          className="text-input"
          value={rootTitle}
          onChange={(e) => setRootTitle(e.target.value)}
          placeholder="Your role"
          aria-label="Your role"
          disabled={busy}
        />
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? 'Setting up…' : 'Continue'}
        </button>
        <button type="button" className="btn" onClick={() => setOpen(false)} disabled={busy}>
          Not now
        </button>
      </div>
      {error && <p className="form-error">{error}</p>}
    </form>
  )
}
