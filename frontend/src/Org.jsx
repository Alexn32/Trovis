import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { Spinner } from './ui.jsx'
import { PlusIcon, TrashIcon, UserIcon } from './Icons.jsx'
import {
  buildTree,
  emptyChartCopy,
  peopleInRole,
  personLabel,
  roleActions,
  scopeSummary,
  unplacedMembers,
} from './org.js'

// The Org surface: the chart, who is in it, what each role can see, and who
// is still waiting on an invite.
//
// One page, one truth. Roles, reporting lines, people and invites all live
// here — there is deliberately no second place in the product to invite
// someone into this company.
//
// Every affordance below is drawn from what the SERVER said about each role
// (can_edit / can_add_child on /org/chart). This component never works out
// who may do what: it renders the answer it was given, and the API refuses
// anything that slips through anyway. That ordering matters — if this file
// ever starts computing permissions, the client becomes a second authority
// and the two will drift.

export default function Org({ seat, org, onGraduated }) {
  const [chart, setChart] = useState(null)
  const [invites, setInvites] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    setError(null)
    try {
      const data = await api.getOrgChart()
      setChart(data)
      // Invites are scoped the same way the chart is, and fail soft: not
      // being allowed to list them must not blank the whole page.
      try {
        setInvites((await api.getInvites()) || [])
      } catch {
        setInvites([])
      }
      setSelectedId((prev) => {
        const roles = data?.roles || []
        if (prev && roles.some((r) => r.id === prev)) return prev
        return roles.length ? roles[0].id : null
      })
    } catch (e) {
      setError(e?.message || 'Could not load your organization')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function act(fn, successMessage) {
    setNotice('')
    try {
      await fn()
      await load()
      if (successMessage) setNotice(successMessage)
      return true
    } catch (e) {
      // Surface the server's own reason. It is the authority on refusals,
      // and its wording ("you can only edit roles below your own") explains
      // the ladder better than anything invented here.
      setNotice(e?.message || 'That did not work')
      return false
    }
  }

  if (loading) {
    return (
      <div className="view">
        <div className="state-card">
          <Spinner /> Loading your organization…
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="view">
        <div className="state-card error">
          <h2>Couldn't load your organization</h2>
          <p>{error}</p>
          <button type="button" className="btn" onClick={load}>
            Try again
          </button>
        </div>
      </div>
    )
  }

  const roles = chart?.roles || []
  const members = chart?.members || []
  const levels = chart?.scope_levels || []
  const tree = buildTree(roles)
  const selected = roles.find((r) => r.id === selectedId) || null
  const unplaced = unplacedMembers(roles, members)
  const empty = emptyChartCopy(chart?.can_edit_chart)

  return (
    <div className="view org-view">
      <header className="org-header">
        <h2 className="section-label">Org</h2>
        <p className="org-subtitle">
          Who does what, who they report to, and how much of the work each
          person sees. Everyone here is looking at the same work — a role
          decides how much of it is on your screen.
        </p>
      </header>

      {notice && (
        <div className="org-notice" role="status">
          {notice}
          <button type="button" className="org-notice-x" onClick={() => setNotice('')}>
            ×
          </button>
        </div>
      )}

      {org?.account_type === 'individual' && (
        <GraduateCard
          orgName={org?.name || ''}
          onGraduate={async (payload) => {
            const ok = await act(
              async () => {
                const res = await api.graduateOrg(payload)
                if (onGraduated) onGraduated(res?.org)
              },
              'Your workspace is a company now. Invite people into roles below.',
            )
            return ok
          }}
        />
      )}

      {tree.length === 0 ? (
        <div className="state-card">
          <h2>{empty.title}</h2>
          <p>{empty.body}</p>
          {chart?.can_edit_chart && (
            <AddRoleForm
              parentId={null}
              levels={levels}
              onSubmit={(payload) => act(() => api.createRole(payload), 'Role added.')}
              label="Add the first role"
            />
          )}
        </div>
      ) : (
        <div className="org-body">
          <div className="org-tree" role="tree" aria-label="Org chart">
            {tree.map(({ role, depth }) => (
              <button
                key={role.id}
                type="button"
                role="treeitem"
                aria-selected={role.id === selectedId}
                aria-level={depth + 1}
                className={`org-node ${role.id === selectedId ? 'is-selected' : ''}`}
                style={{ paddingLeft: `${12 + depth * 18}px` }}
                onClick={() => setSelectedId(role.id)}
              >
                <span className="org-node-title">{role.title}</span>
                <span className="org-node-people">
                  {peopleInRole(role, members).map((m) => (
                    <span key={m.id} className="org-chip">
                      {personLabel(m)}
                    </span>
                  ))}
                  {role.user_ids?.length === 0 && (
                    <span className="org-chip org-chip-empty">Vacant</span>
                  )}
                </span>
              </button>
            ))}
            {chart?.can_edit_chart && (
              <AddRoleForm
                parentId={null}
                levels={levels}
                onSubmit={(payload) => act(() => api.createRole(payload), 'Role added.')}
                label="Add a top-level role"
                compact
              />
            )}
          </div>

          <div className="org-detail">
            {selected ? (
              <RoleDetail
                role={selected}
                roles={roles}
                members={members}
                levels={levels}
                invites={invites.filter((i) => i.role_id === selected.id)}
                act={act}
              />
            ) : (
              <p className="org-empty-detail">Pick a role to see its details.</p>
            )}
          </div>
        </div>
      )}

      <People
        members={members}
        unplaced={unplaced}
        orgBuilder={chart?.org_builder}
        seat={seat}
        act={act}
      />

      {invites.length > 0 && (
        <section className="org-section">
          <h3 className="org-section-title">Waiting on an invite</h3>
          <ul className="org-list">
            {invites.map((i) => (
              <li key={i.id} className="org-row">
                <span className="org-row-main">
                  <span className="org-row-name">{i.email}</span>
                  <span className="org-row-sub">
                    {roles.find((r) => r.id === i.role_id)?.title || 'No role yet'}
                  </span>
                </span>
                <button
                  type="button"
                  className="icon-btn"
                  title="Revoke invite"
                  onClick={() => act(() => api.revokeInvite(i.id), 'Invite revoked.')}
                >
                  <TrashIcon />
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

// Path A → Path B, in the one place it makes sense to offer it. A one-seat
// workspace keeps everything it already has — agents, jobs, API keys and
// Connect all hang off the account, which graduation doesn't touch — so this
// is an invitation, not a migration, and it is never forced: an individual
// who ignores it keeps the full product.
function GraduateCard({ orgName, onGraduate }) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState(orgName)
  const [rootTitle, setRootTitle] = useState('Founder')
  const [busy, setBusy] = useState(false)

  if (!open) {
    return (
      <div className="org-graduate">
        <div className="org-graduate-main">
          <h3 className="org-graduate-title">Working with other people?</h3>
          <p className="org-graduate-sub">
            Turn this workspace into a company: invite colleagues, map who
            reports to whom, and decide how much of the work each person sees.
            Your agents, work and connections all stay exactly as they are.
          </p>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setOpen(true)}>
          Invite your company
        </button>
      </div>
    )
  }

  return (
    <form
      className="org-graduate is-open"
      onSubmit={async (e) => {
        e.preventDefault()
        if (busy) return
        setBusy(true)
        await onGraduate({
          org_name: name.trim() || null,
          root_role_title: rootTitle.trim() || null,
        })
        setBusy(false)
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
    </form>
  )
}

function RoleDetail({ role, roles, members, levels, invites, act }) {
  const can = roleActions(role)
  const level = levels.find((l) => l.id === role.scope_level_id) || null
  const people = peopleInRole(role, members)
  const parent = roles.find((r) => r.id === role.parent_role_id) || null
  const [renaming, setRenaming] = useState(false)
  const [title, setTitle] = useState(role.title)

  useEffect(() => {
    setTitle(role.title)
    setRenaming(false)
  }, [role.id, role.title])

  const unassigned = members.filter((m) => !(role.user_ids || []).includes(m.id))

  return (
    <div className="org-detail-card">
      <div className="org-detail-head">
        {renaming ? (
          <form
            className="org-inline-form"
            onSubmit={async (e) => {
              e.preventDefault()
              if (!title.trim()) return
              const ok = await act(
                () => api.updateRole(role.id, { title: title.trim() }),
                'Role renamed.',
              )
              if (ok) setRenaming(false)
            }}
          >
            <input
              className="text-input"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              aria-label="Role title"
            />
            <button type="submit" className="btn btn-primary">
              Save
            </button>
            <button type="button" className="btn" onClick={() => setRenaming(false)}>
              Cancel
            </button>
          </form>
        ) : (
          <>
            <h3 className="org-detail-title">{role.title}</h3>
            {can.canRename && (
              <button type="button" className="btn" onClick={() => setRenaming(true)}>
                Rename
              </button>
            )}
            {can.canDelete && (
              <button
                type="button"
                className="btn btn-danger"
                onClick={() =>
                  act(() => api.deleteRole(role.id), 'Role removed.')
                }
              >
                Remove
              </button>
            )}
          </>
        )}
      </div>

      <p className="org-detail-line">
        {parent ? `Reports to ${parent.title}` : 'Top of the chart'}
      </p>

      <section className="org-detail-block">
        <h4 className="org-detail-label">What this role sees</h4>
        <p className="org-detail-line">{scopeSummary(level)}</p>
        {can.canSetScope ? (
          <select
            className="text-input"
            value={role.scope_level_id ?? ''}
            onChange={(e) =>
              act(
                () =>
                  api.updateRole(role.id, {
                    scope_level_id: e.target.value === '' ? null : Number(e.target.value),
                  }),
                'Seat updated.',
              )
            }
            aria-label="Scope level"
          >
            <option value="">No seat (full access)</option>
            {levels.map((l) => (
              <option key={l.id} value={l.id}>
                {l.name}
              </option>
            ))}
          </select>
        ) : (
          <p className="org-detail-muted">
            Only someone above this role can change what it sees.
          </p>
        )}
      </section>

      <section className="org-detail-block">
        <h4 className="org-detail-label">People in this role</h4>
        {people.length === 0 && <p className="org-detail-muted">Nobody yet.</p>}
        <ul className="org-list">
          {people.map((m) => (
            <li key={m.id} className="org-row">
              <span className="org-row-main">
                <span className="org-row-name">{personLabel(m)}</span>
                <span className="org-row-sub">{m.email}</span>
              </span>
              {can.canAssignPeople && (
                <button
                  type="button"
                  className="icon-btn"
                  title="Take out of this role"
                  onClick={() =>
                    act(
                      () => api.removeRoleMember(role.id, m.id),
                      'Removed from the role.',
                    )
                  }
                >
                  <TrashIcon />
                </button>
              )}
            </li>
          ))}
        </ul>
        {can.canAssignPeople && unassigned.length > 0 && (
          <select
            className="text-input"
            value=""
            onChange={(e) => {
              const id = Number(e.target.value)
              if (id) act(() => api.addRoleMember(role.id, id), 'Person placed.')
            }}
            aria-label="Add someone to this role"
          >
            <option value="">Move someone into this role…</option>
            {unassigned.map((m) => (
              <option key={m.id} value={m.id}>
                {personLabel(m)}
              </option>
            ))}
          </select>
        )}
      </section>

      {can.canInvite && (
        <section className="org-detail-block">
          <h4 className="org-detail-label">Invite into this role</h4>
          <InviteToRoleForm roleId={role.id} act={act} />
          {invites.length > 0 && (
            <p className="org-detail-muted">
              {invites.length} invite{invites.length === 1 ? '' : 's'} still open.
            </p>
          )}
        </section>
      )}

      {can.canAddChild && (
        <section className="org-detail-block">
          <h4 className="org-detail-label">Add a role under this one</h4>
          <AddRoleForm
            parentId={role.id}
            levels={levels}
            onSubmit={(payload) => act(() => api.createRole(payload), 'Role added.')}
            label="Add role"
            compact
          />
        </section>
      )}
    </div>
  )
}

function AddRoleForm({ parentId, levels, onSubmit, label, compact = false }) {
  const [title, setTitle] = useState('')
  const [levelId, setLevelId] = useState('')
  const [busy, setBusy] = useState(false)

  return (
    <form
      className={`org-inline-form ${compact ? 'is-compact' : ''}`}
      onSubmit={async (e) => {
        e.preventDefault()
        if (!title.trim() || busy) return
        setBusy(true)
        const ok = await onSubmit({
          title: title.trim(),
          parent_role_id: parentId,
          scope_level_id: levelId === '' ? null : Number(levelId),
        })
        setBusy(false)
        if (ok) {
          setTitle('')
          setLevelId('')
        }
      }}
    >
      <input
        className="text-input"
        placeholder="Role title"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        aria-label="New role title"
        disabled={busy}
      />
      <select
        className="text-input"
        value={levelId}
        onChange={(e) => setLevelId(e.target.value)}
        aria-label="Scope level for the new role"
        disabled={busy}
      >
        <option value="">No seat yet</option>
        {levels.map((l) => (
          <option key={l.id} value={l.id}>
            {l.name}
          </option>
        ))}
      </select>
      <button type="submit" className="btn btn-primary" disabled={busy || !title.trim()}>
        <PlusIcon /> {label}
      </button>
    </form>
  )
}

function InviteToRoleForm({ roleId, act }) {
  const [email, setEmail] = useState('')
  const [link, setLink] = useState('')
  const [busy, setBusy] = useState(false)

  return (
    <>
      <form
        className="org-inline-form is-compact"
        onSubmit={async (e) => {
          e.preventDefault()
          if (!email.trim() || busy) return
          setBusy(true)
          let created = null
          const ok = await act(async () => {
            created = await api.createInvite({
              email: email.trim(),
              role: 'member',
              role_id: roleId,
            })
          }, 'Invite sent.')
          setBusy(false)
          if (ok) {
            setEmail('')
            // The link is shown as well as emailed: email delivery is
            // fail-soft on the server, so a copyable link is the only thing
            // that makes an invite reliable when email isn't configured.
            setLink(created?.invite_url || '')
          }
        }}
      >
        <input
          className="text-input"
          type="email"
          placeholder="name@company.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          aria-label="Invite email"
          disabled={busy}
        />
        <button type="submit" className="btn btn-primary" disabled={busy || !email.trim()}>
          Invite
        </button>
      </form>
      {link && (
        <p className="org-detail-muted org-invite-link">
          Share this link if the email doesn't arrive: <code>{link}</code>
        </p>
      )}
    </>
  )
}

function People({ members, unplaced, orgBuilder, seat, act }) {
  return (
    <section className="org-section">
      <h3 className="org-section-title">People</h3>
      {unplaced.length > 0 && (
        <p className="org-detail-muted">
          {unplaced.length} {unplaced.length === 1 ? 'person is' : 'people are'} not
          on the chart yet, so they see everything. Put them in a role to narrow
          that down.
        </p>
      )}
      <ul className="org-list">
        {members.map((m) => (
          <li key={m.id} className="org-row">
            <span className="org-row-avatar">
              <UserIcon size={14} />
            </span>
            <span className="org-row-main">
              <span className="org-row-name">
                {personLabel(m)}
                {m.org_builder && <span className="org-tag">Org builder</span>}
              </span>
              <span className="org-row-sub">{m.email}</span>
            </span>
            {orgBuilder && (
              <button
                type="button"
                className="btn"
                onClick={() =>
                  act(
                    () => api.setOrgBuilder(m.id, !m.org_builder),
                    m.org_builder ? 'Org builder removed.' : 'Org builder granted.',
                  )
                }
              >
                {m.org_builder ? 'Remove builder' : 'Make org builder'}
              </button>
            )}
          </li>
        ))}
      </ul>
      {!orgBuilder && (
        <p className="org-detail-muted">
          {seat?.role_title
            ? `You're in ${seat.role_title}.`
            : "You're not in a role yet."}{' '}
          An org builder can change who edits the chart.
        </p>
      )}
    </section>
  )
}
