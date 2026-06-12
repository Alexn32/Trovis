import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'

/* ─────────────────────────────────────────────
   TROVIS — Agent Detail Page
   Organizing rule: plain English first, telemetry behind a click.
     1. Who is this & is it OK?   (header + status-with-reason + Ask)
     2. How's it performing?      (This Week strip)
     3. What has it been doing?   (Work Feed — 3 depths, paginated)
     4. Is it what it says it is?  (Identity & Drift + what it touches)
   Self-contained, light-themed, hardcoded tokens per the design spec.
   ───────────────────────────────────────────── */

const C = {
  linen: '#F5F1EB', cream: '#FBF8F3', subtle: '#ECE8E1', border: '#DDD7CE',
  ink: '#2C2418', body: '#4A4137', muted: '#8C8378', faint: '#B8B0A4',
  teal: '#5A7B7B', ok: '#2A9D6E', warn: '#D4792A', err: '#C43528',
}
const F = {
  disp: "'Space Grotesk', sans-serif",
  body: "'DM Sans', sans-serif",
  mono: "'JetBrains Mono', monospace",
}

// status → dot color. Only these three states; never a dot without a reason.
const STATUS_COLOR = { healthy: C.ok, attention: C.warn, error: C.err }

/* ── formatters (match the prototype's look) ── */
function fmtRel(iso) {
  if (!iso) return ''
  const ms = Date.now() - Date.parse(iso)
  if (Number.isNaN(ms)) return ''
  const m = Math.floor(ms / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}
function fmtDate(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}
function fmtDur(ms) {
  const v = Number(ms) || 0
  if (v < 1000) return `${Math.round(v)}ms`
  return `${(v / 1000).toFixed(1)}s`
}
function fmtCost(usd) {
  if (usd == null) return '—'
  const n = Number(usd) || 0
  if (n === 0) return '$0.00'
  if (n < 0.01) return `$${parseFloat(n.toPrecision(2))}`
  return `$${n.toFixed(2)}`
}
function fmtTokens(n) {
  const v = Number(n) || 0
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
  if (v >= 1000) return `${(v / 1000).toFixed(1)}K`
  return `${v}`
}

function TMark({ size = 16, color = C.teal }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <line x1="3" y1="5" x2="21" y2="5" stroke={color} strokeWidth="2.4" strokeLinecap="round" />
      <line x1="12" y1="5" x2="12" y2="21" stroke={color} strokeWidth="2.4" strokeLinecap="round" />
      <line x1="6.5" y1="12" x2="9.5" y2="12" stroke={color} strokeWidth="2.4" strokeLinecap="round" />
      <line x1="14.5" y1="12" x2="17.5" y2="12" stroke={color} strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  )
}

function Card({ children, style }) {
  return (
    <div style={{ background: C.cream, border: `1px solid ${C.border}`, borderRadius: 14, ...style }}>
      {children}
    </div>
  )
}
function Label({ children }) {
  return (
    <div style={{
      fontFamily: F.mono, fontSize: 11, fontWeight: 500, letterSpacing: '0.08em',
      textTransform: 'uppercase', color: C.muted, marginBottom: 10,
    }}>{children}</div>
  )
}
function Chip({ children }) {
  return (
    <span style={{
      display: 'inline-block', padding: '4px 10px', margin: '0 6px 6px 0',
      background: C.subtle, borderRadius: 999, fontSize: 12.5, color: C.body, fontFamily: F.body,
    }}>{children}</span>
  )
}
function Tag({ children }) {
  return (
    <span style={{ fontFamily: F.mono, fontSize: 11.5, background: C.subtle, padding: '2px 8px', borderRadius: 6 }}>
      {children}
    </span>
  )
}
function Pill({ color, children }) {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 7, padding: '5px 12px',
      background: C.cream, border: `1px solid ${C.border}`, borderRadius: 999,
      fontSize: 12.5, color: C.body, fontFamily: F.body,
    }}>
      <span style={{ width: 8, height: 8, borderRadius: '50%', background: color, flexShrink: 0 }} />
      {children}
    </span>
  )
}

/* ── 1. Header ── */
function Header({ summary, registration, account, onBack }) {
  const [descOpen, setDescOpen] = useState(false)
  const name = summary.display_name || summary.service_name
  const owner = summary.owner_name || account?.userName
  const model = registration?.model
  const status = summary.status || 'healthy'
  const reason = summary.status_reason || 'Active'
  return (
    <div>
      <button onClick={onBack} style={{
        background: 'none', border: 'none', color: C.muted, fontSize: 13.5,
        fontFamily: F.body, cursor: 'pointer', padding: 0, marginBottom: 18,
      }}>← Back to fleet</button>

      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <h1 style={{ fontFamily: F.mono, fontWeight: 500, fontSize: 26, margin: 0, color: C.ink, letterSpacing: '-0.01em' }}>
          {name}
        </h1>
        <Pill color={STATUS_COLOR[status] || C.ok}>{reason}</Pill>
      </div>

      <div style={{
        display: 'flex', gap: 14, alignItems: 'center', flexWrap: 'wrap',
        margin: '10px 0 12px', fontSize: 13, color: C.muted, fontFamily: F.body,
      }}>
        {owner && <span>Owner: <span style={{ color: C.body, fontWeight: 500 }}>{owner}</span></span>}
        {summary.platform && <Tag>{summary.platform}</Tag>}
        {model && <Tag>{model}</Tag>}
        {summary.agent_id && summary.agent_id !== 'main' && <Tag>{summary.agent_id}</Tag>}
        <span>First seen {fmtDate(summary.first_seen)}</span>
      </div>

      {summary.description && (
        <p style={{ fontSize: 15, lineHeight: 1.55, color: C.body, margin: 0, maxWidth: 720, fontFamily: F.body }}>
          {summary.description}
          {summary.description_long && (!descOpen ? (
            <button onClick={() => setDescOpen(true)} style={{
              background: 'none', border: 'none', color: C.teal, cursor: 'pointer',
              fontSize: 14, fontFamily: F.body, padding: '0 0 0 6px',
            }}>More</button>
          ) : (
            <span style={{ color: C.muted }}>
              {' '}{summary.description_long}
              <button onClick={() => setDescOpen(false)} style={{
                background: 'none', border: 'none', color: C.teal, cursor: 'pointer',
                fontSize: 14, fontFamily: F.body, padding: '0 0 0 6px',
              }}>Less</button>
            </span>
          ))}
        </p>
      )}
    </div>
  )
}

/* ── 2. Ask bar (scoped to this agent) ── */
function AskBar({ serviceName, agentId }) {
  const [q, setQ] = useState('')
  const [answer, setAnswer] = useState(null)
  const [pending, setPending] = useState(false)
  const chips = [
    'What did this agent do today?',
    'Is it behaving as configured?',
    'Why is it flagged for attention?',
  ]
  const ask = async (text) => {
    const t = (text ?? q).trim()
    if (!t || pending) return
    setQ(t)
    setPending(true)
    setAnswer(null)
    try {
      const r = await api.askAboutAgent(serviceName, [{ role: 'user', content: t }], agentId)
      setAnswer(r.answer)
    } catch (e) {
      setAnswer(
        String(e?.message || '').includes('503')
          ? 'AI is unavailable right now — the backend needs an ANTHROPIC_API_KEY.'
          : 'Something went wrong answering that. Please try again.',
      )
    } finally {
      setPending(false)
    }
  }
  return (
    <Card style={{ padding: '16px 18px', marginTop: 20 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
        <TMark size={14} />
        <span style={{ fontFamily: F.mono, fontSize: 11, letterSpacing: '0.08em', textTransform: 'uppercase', color: C.muted }}>
          Ask about this agent
        </span>
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
        {chips.map((c) => (
          <button key={c} onClick={() => ask(c)} disabled={pending} style={{
            padding: '7px 14px', background: C.linen, border: `1px solid ${C.border}`,
            borderRadius: 999, fontSize: 13, color: C.body, cursor: pending ? 'default' : 'pointer', fontFamily: F.body,
          }}>{c}</button>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && ask(q)}
          placeholder={`Ask anything about ${serviceName}…`}
          style={{
            flex: 1, padding: '11px 14px', fontSize: 14, fontFamily: F.body,
            background: C.linen, border: `1px solid ${C.border}`, borderRadius: 10, color: C.ink, outline: 'none',
          }}
        />
        <button onClick={() => ask(q)} disabled={pending || !q.trim()} style={{
          padding: '11px 18px', background: C.teal, color: C.cream, border: 'none',
          borderRadius: 10, fontSize: 14, fontWeight: 600, fontFamily: F.disp,
          cursor: pending || !q.trim() ? 'default' : 'pointer', opacity: pending || !q.trim() ? 0.6 : 1,
        }}>{pending ? '…' : 'Ask'}</button>
      </div>
      {answer && (
        <p style={{
          margin: '14px 2px 2px', fontSize: 14.5, lineHeight: 1.6, color: C.body,
          fontFamily: F.body, borderLeft: `2px solid ${C.teal}`, paddingLeft: 12, whiteSpace: 'pre-wrap',
        }}>{answer}</p>
      )}
    </Card>
  )
}

/* ── 3. This Week strip ── */
function WeekStrip({ weekly, costDays }) {
  const stats = [
    ['Runs', weekly?.runs != null ? String(weekly.runs) : '0'],
    ['Success', weekly?.success_rate != null ? `${Math.round(weekly.success_rate)}%` : '—'],
    ['Avg response', weekly?.avg_duration_ms ? fmtDur(weekly.avg_duration_ms) : '—'],
    ['Cost this week', fmtCost(weekly?.cost)],
    ['Tokens', fmtTokens(weekly?.tokens || 0)],
  ]
  const max = Math.max(0.0001, ...costDays)
  const brief =
    weekly?.summary && !weekly?.summary_unavailable
      ? weekly.summary
      : weekly?.runs
        ? `${weekly.runs} run${weekly.runs === 1 ? '' : 's'} this week with ${
            weekly.success_rate != null ? `${Math.round(weekly.success_rate)}% success` : 'no errors'
          }.`
        : 'No activity recorded this week yet.'
  return (
    <Card style={{ padding: '18px 20px', marginTop: 16 }}>
      <Label>This week</Label>
      <p style={{ margin: '0 0 16px', fontSize: 15, lineHeight: 1.55, color: C.body, fontFamily: F.body, maxWidth: 720 }}>
        {brief}
      </p>
      <div style={{ display: 'flex', gap: 0, flexWrap: 'wrap', alignItems: 'flex-end' }}>
        {stats.map(([k, v], i) => (
          <div key={k} style={{
            padding: '0 28px 0 0', marginRight: 28,
            borderRight: i < stats.length - 1 ? `1px solid ${C.subtle}` : 'none',
          }}>
            <div style={{ fontFamily: F.mono, fontSize: 10.5, letterSpacing: '0.07em', textTransform: 'uppercase', color: C.muted, marginBottom: 4 }}>{k}</div>
            <div style={{ fontFamily: F.disp, fontWeight: 700, fontSize: 22, color: C.ink }}>{v}</div>
          </div>
        ))}
        {/* 14-day cost sparkline: empty days are faint ticks, never walls */}
        <div style={{ flex: 1, minWidth: 160, display: 'flex', alignItems: 'flex-end', gap: 3, height: 46, paddingBottom: 2 }}>
          {costDays.map((v, i) => (
            <div key={i} title={v > 0 ? fmtCost(v) : 'no activity'} style={{
              flex: 1, borderRadius: 2,
              height: v > 0 ? Math.max(8, (v / max) * 40) : 3,
              background: v > 0 ? C.teal : C.subtle,
            }} />
          ))}
        </div>
      </div>
    </Card>
  )
}

/* ── 4. Work Feed (3 depths) ── */
function FeedItem({ r }) {
  const [depth, setDepth] = useState(0) // 0 collapsed · 1 exchange · 2 spans
  const isSystem = r.kind === 'system' || !r.exchange
  const dot = r.error ? C.err : isSystem ? C.faint : C.ok
  // A small type tag so each row reads at a glance — and its color matches the
  // status dot, so the dot's meaning is self-explanatory. Errors win; then
  // system events; then the interaction's direction (both sides, agent-only,
  // or user-only).
  const tag = r.error
    ? { label: 'Error', color: C.err }
    : isSystem
      ? { label: 'System', color: C.muted }
      : r.exchange?.user && r.exchange?.agent
        ? { label: 'Interaction', color: C.teal }
        : r.exchange?.agent
          ? { label: 'Agent output', color: C.teal }
          : r.exchange?.user
            ? { label: 'Message received', color: C.muted }
            : { label: 'Activity', color: C.muted }
  return (
    <div style={{ borderTop: `1px solid ${C.subtle}` }}>
      <button onClick={() => setDepth(depth === 0 ? 1 : 0)} style={{
        display: 'flex', alignItems: 'center', gap: 14, width: '100%',
        padding: '15px 18px', background: 'none', border: 'none', cursor: 'pointer', textAlign: 'left', fontFamily: F.body,
      }}>
        <span style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0, background: dot }} />
        <span style={{ fontSize: 14.5, color: C.ink, flex: 1, minWidth: 0 }}>{r.summary}</span>
        <span style={{
          fontFamily: F.mono, fontSize: 10, fontWeight: 500, letterSpacing: '0.04em',
          textTransform: 'uppercase', color: tag.color, background: C.subtle,
          padding: '2px 8px', borderRadius: 6, flexShrink: 0, whiteSpace: 'nowrap',
        }}>{tag.label}</span>
        <span style={{ fontFamily: F.mono, fontSize: 12, color: C.muted, flexShrink: 0 }}>{fmtCost(r.cost_usd)}</span>
        <span style={{ fontFamily: F.mono, fontSize: 12, color: C.muted, flexShrink: 0 }}>{fmtRel(r.time)}</span>
        <span style={{ color: C.faint, fontSize: 12, transform: depth > 0 ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s' }}>›</span>
      </button>

      {depth > 0 && (
        <div style={{ padding: '0 18px 16px 40px' }}>
          {!isSystem ? (
            <div style={{ maxWidth: 700 }}>
              {r.exchange.user && (
                <div style={{ marginBottom: 10 }}>
                  <div style={{ fontFamily: F.mono, fontSize: 10.5, letterSpacing: '0.07em', textTransform: 'uppercase', color: C.muted, marginBottom: 4 }}>User</div>
                  <div style={{ fontSize: 14, lineHeight: 1.55, color: C.body, background: C.linen, border: `1px solid ${C.subtle}`, borderRadius: 10, padding: '10px 14px', whiteSpace: 'pre-wrap' }}>{r.exchange.user}</div>
                </div>
              )}
              {r.exchange.agent && (
                <div>
                  <div style={{ fontFamily: F.mono, fontSize: 10.5, letterSpacing: '0.07em', textTransform: 'uppercase', color: C.muted, marginBottom: 4 }}>Agent</div>
                  <div style={{ fontSize: 14, lineHeight: 1.55, color: C.ink, background: C.linen, border: `1px solid ${C.subtle}`, borderRadius: 10, padding: '10px 14px', whiteSpace: 'pre-wrap' }}>{r.exchange.agent}</div>
                </div>
              )}
              <div style={{ display: 'flex', gap: 18, marginTop: 10, fontFamily: F.mono, fontSize: 11.5, color: C.muted }}>
                <span>{fmtDur(r.duration_ms)}</span>
                <span>{(r.tokens || 0).toLocaleString()} tokens</span>
                <span>{fmtCost(r.cost_usd)}</span>
              </div>
            </div>
          ) : (
            <div style={{ fontSize: 13.5, color: C.muted, fontFamily: F.body }}>
              System record — the agent registered and declared its identity. No exchange to show.
            </div>
          )}

          {r.spans.length > 0 && (
            <button onClick={() => setDepth(depth === 2 ? 1 : 2)} style={{
              marginTop: 12, background: 'none', border: 'none', color: C.teal,
              fontSize: 13, fontFamily: F.body, cursor: 'pointer', padding: 0,
            }}>{depth === 2 ? 'Hide spans' : `View ${r.spans.length} spans`}</button>
          )}

          {depth === 2 && (
            <div style={{ marginTop: 8, border: `1px solid ${C.subtle}`, borderRadius: 10, overflow: 'hidden', maxWidth: 700 }}>
              {r.spans.map((s, i) => (
                <div key={i} style={{
                  display: 'flex', justifyContent: 'space-between', padding: '8px 14px',
                  borderTop: i === 0 ? 'none' : `1px solid ${C.subtle}`,
                  fontFamily: F.mono, fontSize: 12, color: s.status === 'error' ? C.err : C.body, background: C.linen,
                }}>
                  <span>{s.operation}</span><span style={{ color: C.muted }}>{s.duration}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function WorkFeed({ serviceName, agentId }) {
  const [records, setRecords] = useState(null)
  const [cursor, setCursor] = useState(null)
  const [expanded, setExpanded] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)

  useEffect(() => {
    let alive = true
    setRecords(null)
    setExpanded(false)
    api
      .getAgentRecords(serviceName, { agentId, limit: 20 })
      .then((d) => {
        if (!alive) return
        setRecords(Array.isArray(d.records) ? d.records : [])
        setCursor(d.next_cursor || null)
      })
      .catch(() => alive && setRecords([]))
    return () => { alive = false }
  }, [serviceName, agentId])

  const loadMore = async () => {
    if (loadingMore || !cursor) return
    setLoadingMore(true)
    try {
      const d = await api.getAgentRecords(serviceName, { agentId, limit: 20, cursor })
      setRecords((prev) => [...(prev || []), ...(d.records || [])])
      setCursor(d.next_cursor || null)
    } catch {
      /* keep what we have */
    } finally {
      setLoadingMore(false)
    }
  }

  const recs = records || []
  const visible = expanded ? recs : recs.slice(0, 4)
  const countLabel = `${recs.length}${cursor ? '+' : ''} record${recs.length === 1 ? '' : 's'}`

  return (
    <Card style={{ marginTop: 16, overflow: 'hidden' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', padding: '16px 18px 12px' }}>
        <Label>Work feed</Label>
        {records !== null && (
          <span style={{ fontFamily: F.mono, fontSize: 11.5, color: C.muted }}>{countLabel}</span>
        )}
      </div>

      {records === null ? (
        <div style={{ padding: '20px 18px', color: C.muted, fontFamily: F.body, fontSize: 14, borderTop: `1px solid ${C.subtle}` }}>Loading…</div>
      ) : recs.length === 0 ? (
        <div style={{ padding: '20px 18px', color: C.muted, fontFamily: F.body, fontSize: 14, borderTop: `1px solid ${C.subtle}` }}>
          No activity yet — this page fills in as the agent runs.
        </div>
      ) : (
        visible.map((r) => <FeedItem key={r.id} r={r} />)
      )}

      {records !== null && recs.length > 0 && (recs.length > 4 || cursor) && (
        <button
          onClick={() => {
            if (!expanded) setExpanded(true)
            else if (cursor) loadMore()
            else setExpanded(false)
          }}
          disabled={loadingMore}
          style={{
            display: 'block', width: '100%', padding: '13px 18px', background: C.linen,
            border: 'none', borderTop: `1px solid ${C.subtle}`, color: C.teal,
            fontSize: 13.5, fontWeight: 500, fontFamily: F.body, cursor: 'pointer',
          }}
        >
          {!expanded
            ? `Show all ${countLabel}`
            : loadingMore
              ? 'Loading…'
              : cursor
                ? 'Load 20 more'
                : 'Show recent 4'}
        </button>
      )}
    </Card>
  )
}

/* ── 5. Identity & Drift ── */
function IdentityCard({ summary, capabilities, registration }) {
  const [soulOpen, setSoulOpen] = useState(false)
  const soul = registration?.soul || registration?.identity || registration?.operating_manual || ''
  const reads = capabilities?.reads_from || []
  const writes = capabilities?.writes_to || []
  const cando = capabilities?.can_do || []
  const hasCaps = reads.length || writes.length || cando.length
  // No drift engine yet — report the honest default, consistent with status.
  const drifting = summary.status === 'error'
  return (
    <Card style={{ padding: '18px 20px', marginTop: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 10 }}>
        <Label>Identity &amp; drift</Label>
        <Pill color={drifting ? C.warn : C.ok}>
          {drifting ? 'Recent failure — review against declared behavior' : 'Behaving as declared — no drift events this week'}
        </Pill>
      </div>

      {soul ? (
        <>
          <div style={{
            marginTop: 14, background: C.linen, border: `1px solid ${C.subtle}`, borderRadius: 10,
            padding: '12px 16px', fontFamily: F.mono, fontSize: 12.5, lineHeight: 1.6, color: C.body,
            whiteSpace: 'pre-wrap', maxHeight: soulOpen ? 'none' : 96, overflow: 'hidden', position: 'relative',
          }}>
            {soul}
            {!soulOpen && <div style={{ position: 'absolute', bottom: 0, left: 0, right: 0, height: 40, background: `linear-gradient(transparent, ${C.linen})` }} />}
          </div>
          <button onClick={() => setSoulOpen(!soulOpen)} style={{
            marginTop: 8, background: 'none', border: 'none', color: C.teal,
            fontSize: 13, fontFamily: F.body, cursor: 'pointer', padding: 0,
          }}>{soulOpen ? 'Collapse declared identity' : 'Show full declared identity'}</button>
        </>
      ) : (
        <div style={{ marginTop: 14, fontSize: 13.5, color: C.muted, fontFamily: F.body }}>
          This agent hasn't published a declared identity (SOUL.md / system prompt).
        </div>
      )}

      {hasCaps ? (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 16, marginTop: 18 }}>
          {[['Reads from', reads], ['Writes to', writes], ['Can do', cando]].map(([k, items]) => (
            <div key={k}>
              <div style={{ fontFamily: F.mono, fontSize: 10.5, letterSpacing: '0.07em', textTransform: 'uppercase', color: C.muted, marginBottom: 8 }}>{k}</div>
              <div>{items.length ? items.map((t) => <Chip key={t}>{t}</Chip>) : <span style={{ color: C.faint, fontSize: 13 }}>—</span>}</div>
            </div>
          ))}
        </div>
      ) : null}
    </Card>
  )
}

/* ── 6. Danger zone ── */
function DangerZone({ serviceName, agentId, onDeleted }) {
  const [confirming, setConfirming] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const del = async () => {
    setDeleting(true)
    try {
      await api.deleteAgent(serviceName, agentId)
      onDeleted()
    } catch {
      setDeleting(false)
      setConfirming(false)
    }
  }
  return (
    <div style={{
      marginTop: 28, border: `1px solid ${C.err}33`, borderRadius: 14, padding: '16px 20px',
      display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16, flexWrap: 'wrap',
    }}>
      <div>
        <div style={{ fontFamily: F.disp, fontWeight: 700, fontSize: 14.5, color: C.ink, marginBottom: 3 }}>Delete this agent</div>
        <div style={{ fontSize: 13, color: C.muted, fontFamily: F.body, maxWidth: 520 }}>
          Removes all telemetry, descriptions, owner assignment, cached insights, and any workflows this agent owns. This cannot be undone.
        </div>
      </div>
      <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
        {confirming && (
          <button onClick={() => setConfirming(false)} disabled={deleting} style={{
            padding: '10px 16px', background: 'none', color: C.muted, border: `1px solid ${C.border}`,
            borderRadius: 10, fontSize: 13.5, fontWeight: 600, fontFamily: F.disp, cursor: 'pointer',
          }}>Cancel</button>
        )}
        <button onClick={() => (confirming ? del() : setConfirming(true))} disabled={deleting} style={{
          padding: '10px 18px', background: C.err, color: '#FFF', border: 'none',
          borderRadius: 10, fontSize: 13.5, fontWeight: 600, fontFamily: F.disp, cursor: 'pointer',
        }}>{deleting ? 'Deleting…' : confirming ? 'Confirm delete' : 'Delete agent'}</button>
      </div>
    </div>
  )
}

/* ── page ── */
export default function AgentDetail({ serviceName, agentId, account, onBack }) {
  const [summary, setSummary] = useState(null)
  const [registration, setRegistration] = useState(null)
  const [weekly, setWeekly] = useState(null)
  const [costDays, setCostDays] = useState(() => Array(14).fill(0))
  const [capabilities, setCapabilities] = useState(null)
  const [error, setError] = useState(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    setError(null)
    api
      .getAgentSummary(serviceName, agentId)
      .then((s) => mounted.current && setSummary(s))
      .catch((e) => mounted.current && setError(e?.message || 'Could not load this agent'))
    // Independent, non-blocking side fetches.
    api.getAgentRegistration(serviceName, agentId).then((r) => mounted.current && setRegistration(r)).catch(() => {})
    api.getWeeklySummary(serviceName, agentId).then((w) => mounted.current && setWeekly(w)).catch(() => {})
    api.getAgentCapabilities(serviceName, agentId).then((c) => mounted.current && setCapabilities(c)).catch(() => {})
    api.getAgentCosts(serviceName, agentId, 14).then((c) => {
      if (!mounted.current) return
      const days = (c.cost_by_day || []).map((d) => Number(d.cost) || 0)
      // Right-align into a fixed 14-slot strip (pad the front with empty days).
      const padded = days.length >= 14 ? days.slice(-14) : [...Array(14 - days.length).fill(0), ...days]
      setCostDays(padded)
    }).catch(() => {})
    return () => { mounted.current = false }
  }, [serviceName, agentId])

  function Shell({ children }) {
    return (
      // flex:1 + width:100% so this fills .app-main (a flex row) edge-to-edge —
      // the linen background spans the full window and the inner column below
      // centers via margin:auto instead of hugging the left.
      <div style={{ flex: 1, width: '100%', minHeight: '100vh', background: C.linen, fontFamily: F.body, color: C.ink }}>
        <div style={{ maxWidth: 980, margin: '0 auto', padding: '28px 24px 64px' }}>{children}</div>
      </div>
    )
  }

  if (error && !summary) {
    return (
      <Shell>
        <button onClick={onBack} style={{ background: 'none', border: 'none', color: C.muted, fontSize: 13.5, fontFamily: F.body, cursor: 'pointer', padding: 0, marginBottom: 18 }}>← Back to fleet</button>
        <div style={{ color: C.muted, fontFamily: F.body }}>{error}</div>
      </Shell>
    )
  }
  if (!summary) {
    return (
      <Shell>
        <div style={{ color: C.muted, fontFamily: F.body }}>Loading agent…</div>
      </Shell>
    )
  }

  return (
    <Shell>
      <Header summary={summary} registration={registration} account={account} onBack={onBack} />
      <AskBar serviceName={summary.service_name} agentId={agentId} />
      <WeekStrip weekly={weekly} costDays={costDays} />
      <WorkFeed serviceName={summary.service_name} agentId={agentId} />
      <IdentityCard summary={summary} capabilities={capabilities} registration={registration} />
      <DangerZone serviceName={summary.service_name} agentId={agentId} onDeleted={onBack} />
    </Shell>
  )
}
