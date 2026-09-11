// Home's sections, as presentation only.
//
// Every one takes already-read data (see homeView.js) and renders it. None of
// them fetch, none of them decide what a number means, and none of them invent
// a value the contract did not supply. That separation is deliberate: the
// rules about what may be SAID are unit-tested in homeView.js, and this file
// is what those rules look like.
//
// Theming is CSS variables only — no hardcoded colors — and every status
// distinction carries text or a shape, never color alone.

import {
  bucketLabel, findingQualifier, formatCount, formatMoney, relTime,
} from './homeView.js'

/* ── small shared bits ─────────────────────────────────────────────── */

export function Section({ title, sub, aside, children, id }) {
  return (
    <section className="hv-section" aria-labelledby={id}>
      <div className="hv-section-head">
        <div>
          <h2 className="hv-section-title" id={id}>{title}</h2>
          {sub ? <p className="hv-section-sub">{sub}</p> : null}
        </div>
        {aside ? <div className="hv-section-aside">{aside}</div> : null}
      </div>
      {children}
    </section>
  )
}

/**
 * A limitation, stated compactly and expandable.
 *
 * Progressive disclosure is the point: the reader should understand that a
 * number is bounded without being made to read the contract, and should be
 * able to get the rest if they want it.
 */
export function Caveat({ children, detail }) {
  if (!children) return null
  return (
    <p className="hv-caveat">
      <span className="hv-caveat-mark" aria-hidden="true">!</span>
      <span>
        {children}
        {detail ? (
          <>
            {' '}
            <details className="hv-caveat-more">
              <summary>Why</summary>
              <span>{detail}</span>
            </details>
          </>
        ) : null}
      </span>
    </p>
  )
}

export function SectionError({ lead, onRetry }) {
  return (
    <div className="hv-error" role="alert">
      <p className="hv-error-lead">{lead}</p>
      {onRetry ? (
        <button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  )
}

export function Skeleton({ lines = 3, label = 'Loading' }) {
  return (
    <div className="hv-skel" role="status" aria-label={label}>
      {Array.from({ length: lines }, (_, i) => (
        <span key={i} className="hv-skel-row" />
      ))}
    </div>
  )
}

/* ── work getting done ─────────────────────────────────────────────── */

/**
 * The period's completions, as the page's primary visual.
 *
 * Bars, not a line: a line implies continuity between buckets, and daily
 * completion counts are discrete. Values are printed on the bars and repeated
 * in a visually-hidden table, so the chart is readable without relying on
 * size comparison or color.
 */
export function CompletionChart({ series, timezone, locale }) {
  if (!series.available) {
    return (
      <div className="hv-chart-empty">
        <p className="hv-chart-empty-lead">No completion history for this period</p>
        <p className="hv-chart-empty-sub">
          Trovis does not draw history it does not have — a flat line here would
          claim nothing happened.
        </p>
      </div>
    )
  }
  const max = Math.max(series.max, 1)
  return (
    <figure className="hv-chart">
      <div className="hv-chart-bars" role="presentation">
        {series.points.map((p, i) => {
          const pct = (p.completed / max) * 100
          return (
            <div className="hv-bar-col" key={i}>
              <span className="hv-bar-value">{p.completed}</span>
              <span
                className={`hv-bar${p.completed === 0 ? ' is-zero' : ''}`}
                style={{ height: `${Math.max(pct, p.completed > 0 ? 6 : 1.5)}%` }}
              />
              <span className="hv-bar-label">
                {bucketLabel(p.bucketStart, locale, timezone)}
              </span>
            </div>
          )
        })}
      </div>
      <figcaption className="sr-only">
        <table>
          <caption>Recorded completions per day</caption>
          <tbody>
            {series.points.map((p, i) => (
              <tr key={i}>
                <th scope="row">{bucketLabel(p.bucketStart, locale, timezone) || `Day ${i + 1}`}</th>
                <td>{p.completed}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </figcaption>
    </figure>
  )
}

/** Completions by existing job identity. Unclassified work stays visible. */
export function JobBreakdown({ jobs, onOpenJob }) {
  if (jobs.empty) return null
  const max = Math.max(jobs.max, 1)
  return (
    <ul className="hv-jobs">
      {jobs.rows.map((r, i) => {
        const openable = r.kind === 'job' && r.id != null && onOpenJob
        const Row = openable ? 'button' : 'div'
        return (
          <li key={`${r.kind}-${r.id ?? i}`} className="hv-job">
            <Row
              {...(openable
                ? { type: 'button', className: 'hv-job-row is-link',
                    onClick: () => onOpenJob(r.id) }
                : { className: 'hv-job-row' })}
            >
              <span className="hv-job-label">
                {/* The name truncates; the tag must not, or the one row that
                    most needs explaining is the one that loses its label. */}
                <span className="hv-job-name">{r.name}</span>
                {r.kind === 'unclassified' ? (
                  <span className="hv-job-tag">no job declared</span>
                ) : null}
              </span>
              <span className="hv-job-track" aria-hidden="true">
                <span
                  className={`hv-job-fill hv-job-fill-${r.kind}`}
                  style={{ width: `${Math.max((r.completed / max) * 100, 2)}%` }}
                />
              </span>
              <span className="hv-job-count">{formatCount(r.completed)}</span>
            </Row>
          </li>
        )
      })}
    </ul>
  )
}

/**
 * Current activity — deliberately subordinate.
 *
 * "Two things are moving right now" and "five finished this week" are
 * different kinds of measure, and the snapshot keeps them in separate objects
 * for exactly that reason. Merging them into one row of KPI cards is how a
 * dashboard ends up claiming a state is an accomplishment.
 */
export function CurrentActivity({ current, onGoWork }) {
  const cells = [
    { key: 'moving', label: 'moving', value: current.moving, filter: 'moving' },
    { key: 'waiting_on_person', label: 'waiting on someone', value: current.waiting, filter: 'waiting' },
    { key: 'blocked', label: 'stuck', value: current.blocked, filter: 'stuck' },
  ]
  return (
    <div className="hv-now">
      <span className="hv-now-label">Right now</span>
      <ul className="hv-now-list">
        {cells.map((c) => (
          <li key={c.key}>
            <button
              type="button"
              className="hv-now-cell"
              onClick={() => onGoWork && onGoWork(c.filter)}
            >
              <span className="hv-now-value">{c.value.text}</span>
              <span className="hv-now-cell-label">{c.label}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

/* ── findings ──────────────────────────────────────────────────────── */

const CATEGORY_WORD = {
  attention: 'Needs a look',
  opportunity: 'Opportunity',
  positive_change: 'Progress',
}

/**
 * One finding, scannable.
 *
 * No severity, no impact score, no urgency — none of those exist in the
 * record, and deriving them from `category` would be inventing them. What is
 * shown is what the finding actually carries: what was seen, why it matters
 * when the evidence establishes that, and the qualification when one applies.
 */
export function FindingCard({ finding, onOpen, onAcknowledge, busy }) {
  const qualifier = findingQualifier(finding)
  const step = finding.next_step
  const entities = (finding.entities || []).slice(0, 3)
  return (
    <li className="hv-finding">
      <article className={`hv-finding-card hv-cat-${finding.category}`}>
        <div className="hv-finding-top">
          <span className={`hv-finding-cat hv-cat-${finding.category}`}>
            {CATEGORY_WORD[finding.category] || finding.category}
          </span>
          {finding.state === 'acknowledged' ? (
            <span className="hv-finding-state">Acknowledged</span>
          ) : null}
        </div>
        <h3 className="hv-finding-title">
          <button type="button" className="hv-finding-open" onClick={() => onOpen(finding)}>
            {finding.title}
          </button>
        </h3>
        <p className="hv-finding-explain">{finding.explanation}</p>
        {finding.consequence ? (
          <p className="hv-finding-conseq">{finding.consequence}</p>
        ) : null}
        {entities.length ? (
          <ul className="hv-finding-ents">
            {entities.map((e, i) => (
              <li key={i} className="hv-ent">
                <span className="hv-ent-kind">{e.kind}</span>
                {e.label || e.id}
              </li>
            ))}
          </ul>
        ) : null}
        {qualifier ? <p className="hv-finding-qual">{qualifier}</p> : null}
        <div className="hv-finding-actions">
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => onOpen(finding)}>
            {step && step.kind !== 'no_action' && step.text ? step.text : 'View evidence'}
          </button>
          {onAcknowledge && finding.state !== 'acknowledged' ? (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={busy}
              onClick={() => onAcknowledge(finding)}
            >
              Mark seen
            </button>
          ) : null}
        </div>
      </article>
    </li>
  )
}

/** The analysis lifecycle line. Never a spinner for a settled state. */
export function AnalysisNote({ read, onRefresh }) {
  const inFlight = read.state === 'queued' || read.state === 'running'
  return (
    <div className={`hv-ai-note hv-tone-${read.tone}`} role="status">
      <span className="hv-ai-dot" aria-hidden="true" data-state={read.state} />
      <span className="hv-ai-text">
        <span className="hv-ai-label">{read.label}</span>
        {read.detail ? <span className="hv-ai-detail">{read.detail}</span> : null}
        {read.newerEvidence && read.state !== 'debounced' ? (
          <span className="hv-ai-detail">
            New records have arrived since this analysis ran.
          </span>
        ) : null}
      </span>
      {inFlight ? <span className="hv-ai-pulse" aria-hidden="true" /> : null}
      {!inFlight && onRefresh ? (
        <button type="button" className="btn btn-ghost btn-sm" onClick={onRefresh}>
          Check again
        </button>
      ) : null}
    </div>
  )
}

/* ── cost ──────────────────────────────────────────────────────────── */

export function CostCard({ fin, period, onOpenCost, locale }) {
  const pct = fin.coverageRatio == null ? null : Math.round(fin.coverageRatio * 100)
  return (
    <div className="hv-cost">
      <div className="hv-cost-main">
        <span className="hv-cost-value">{formatMoney(fin.spend, fin.currency, locale)}</span>
        <span className="hv-cost-label">
          recorded spend
          {fin.orgWide ? ' · organization-wide' : ''}
        </span>
      </div>
      <p className="hv-cost-note">
        {fin.orgWide
          ? 'Spend is recorded per account and agent, not per work scope — this figure covers the whole organization regardless of the work scope selected above.'
          : fin.scopeNote || ''}
      </p>
      <dl className="hv-cost-meta">
        <div>
          <dt>Period</dt>
          <dd>{period}</dd>
        </div>
        <div>
          <dt>Priced coverage</dt>
          <dd>
            {pct == null ? 'Not established' : `${pct}% of cost-bearing calls`}
          </dd>
        </div>
      </dl>
      {fin.unpricedNote ? <Caveat>{fin.unpricedNote}</Caveat> : null}
      {onOpenCost ? (
        <button type="button" className="btn btn-secondary btn-sm" onClick={onOpenCost}>
          Open Cost
        </button>
      ) : null}
    </div>
  )
}

/* ── freshness ─────────────────────────────────────────────────────── */

/**
 * Three different "when", kept apart.
 *
 * Retrieval is not analysis and neither is the record's own currency. A single
 * "Updated 2m ago" would let a fast page load stand in for a fresh record.
 */
export function FreshnessPanel({ fresh, analysisLabel }) {
  const rows = [
    { label: 'Numbers read', value: relTime(fresh.retrieved) },
    { label: 'Investigation', value: fresh.analysed ? relTime(fresh.analysed) : analysisLabel },
    { label: 'Newest work activity', value: relTime(fresh.sourceActivity) },
    { label: 'Newest telemetry', value: relTime(fresh.sourceTelemetry) },
  ]
  return (
    <details className="hv-fresh">
      <summary className="hv-fresh-summary">
        <span>Data freshness</span>
        <span className="hv-fresh-lead">{relTime(fresh.retrieved) || '—'}</span>
      </summary>
      <dl className="hv-fresh-list">
        {rows.map((r) => (
          <div key={r.label}>
            <dt>{r.label}</dt>
            <dd>{r.value || (fresh.absenceEstablished ? 'None recorded' : 'Not found')}</dd>
          </div>
        ))}
      </dl>
      {!fresh.absenceEstablished ? (
        <p className="hv-fresh-note">
          Trovis could not read the whole scope, so a missing timestamp here
          means “not found”, not “never happened”.
        </p>
      ) : null}
    </details>
  )
}
