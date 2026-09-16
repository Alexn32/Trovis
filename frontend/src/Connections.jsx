import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { BrandMark } from './BrandMarks.jsx'
import { CATEGORY_LABELS } from './connectors.js'
import { relativeTime } from './utils.js'
import {
  aiConnectors,
  aiRowState,
  healthFor,
  isConnectable,
  moreConnectors,
  saasStatus,
  workRowState,
  workSystemConnectors,
} from './connectionsPage.js'

// Connections — the visibility infrastructure page.
//
// "What can Trovis see, and how do I connect another source of work?"
// Connections feed Work; Work stays its own surface. This page is neither a
// marketplace nor a telemetry console: recognisable marks, one line each,
// a quiet state, one action.
//
// State comes from GET /connect/health, a read model over what the server
// actually recorded (connect_health.py). Two kinds of truth, kept apart:
//   - AI workers & platforms are DOORS. Agents are derived from telemetry,
//     so there is no installation record; a row reads Connected with a
//     "last observed" time only when attributable telemetry arrived, and
//     otherwise it is just a door. Identity comes from the stamp each
//     Trovis-owned door writes on the wire, never from an agent's name.
//   - Work systems (Stripe / HubSpot / Shopify) have a durable OAuth row
//     (configured) and a durable log of verified webhooks (observed), so
//     they read Connected, Waiting for data, or Not connected.
// Nothing here is coverage: "Connected" means the data path worked, not
// that Trovis sees all the work. There is no degraded state because the
// server records no concrete failure yet, and silence is not one.
//
// The connect / disconnect mechanics moved here from Settings → Integrations
// unchanged; Settings keeps a doorway to this page, not a second manager.

// One row per work system: which API calls its door uses. Thunks, not bound
// references, so a test can stub `api.*` after this module loads.
const SAAS_DOORS = {
  stripe: {
    start: () => api.startStripeConnect(),
    disconnect: () => api.disconnectStripe(),
    configured: (d) => !!d?.stripe_oauth_configured,
    notConfigured: 'Stripe Connect isn’t configured on this deploy yet.',
    startError: 'Could not start Stripe Connect.',
    disconnectError: 'Could not disconnect Stripe.',
    fineprint:
      'Stripe helps Trovis understand payment and refund outcomes related to work. Trovis only '
      + 'associates Stripe activity with work it can reliably link. This is not Trovis billing.',
  },
  hubspot: {
    start: () => api.startHubSpotConnect(),
    disconnect: () => api.disconnectHubSpot(),
    configured: (d) => !!d?.hubspot_oauth_configured,
    notConfigured: 'HubSpot Connect isn’t configured on this deploy yet.',
    startError: 'Could not start HubSpot Connect.',
    disconnectError: 'Could not disconnect HubSpot.',
    fineprint:
      'HubSpot helps Trovis understand deal and ticket changes related to work. Trovis only '
      + 'associates HubSpot activity with work it can reliably link. This is not CRM or contact sync.',
  },
  shopify: {
    start: (shop) => api.startShopifyConnect(shop),
    disconnect: () => api.disconnectShopify(),
    configured: (d) => !!d?.shopify_oauth_configured,
    notConfigured: 'Shopify Connect isn’t configured on this deploy yet.',
    startError: 'Could not start Shopify Connect.',
    disconnectError: 'Could not disconnect Shopify.',
    needsShop: true,
    shopPlaceholder: 'your-store.myshopify.com',
    fineprint:
      'Shopify helps Trovis understand order, payment, refund, and fulfillment outcomes related '
      + 'to work. Trovis only associates Shopify activity with work it can reliably link. This is '
      + 'not catalog, product, or customer sync.',
  },
}

export default function Connections({ active = true, onConnect }) {
  // SaaS connection rows. null = not asked yet; {error} = the check failed,
  // which must not render as "Not connected" for every system.
  const [saas, setSaas] = useState(null)
  // Normalized health. null = not asked yet; {error} = the check failed,
  // which must render as "couldn't check", never as Not connected.
  const [health, setHealth] = useState(null)
  const [busy, setBusy] = useState(null)
  const alive = useRef(true)

  function loadSaas() {
    return api
      .getSaasConnections()
      .then((d) => alive.current && setSaas(d || { connections: [] }))
      .catch(() => alive.current && setSaas({ error: true, connections: [] }))
  }
  function loadHealth() {
    return api
      .getConnectHealth()
      .then((d) => alive.current && setHealth(d && Array.isArray(d.connectors) ? d : { error: true }))
      .catch(() => alive.current && setHealth({ error: true }))
  }
  function reload() {
    return Promise.all([loadSaas(), loadHealth()])
  }

  // Fetch on mount and again each time the pane comes back on screen (a
  // person may have connected or disconnected something elsewhere). Two
  // GETs, no polling.
  useEffect(() => {
    alive.current = true
    if (active) reload()
    return () => { alive.current = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])

  const ai = aiConnectors()
  const work = workSystemConnectors()
  const more = moreConnectors()

  return (
    <div className="view connections-view">
      <header className="cx-header">
        <h2 className="section-label">Connections</h2>
        <p className="cx-lede">
          Connect the AI workers and systems where work happens. Trovis uses
          these connections to build a clearer record of the work.
        </p>
      </header>

      <section className="cx-section" aria-labelledby="cx-ai">
        <h3 id="cx-ai" className="cx-section-title">AI workers &amp; platforms</h3>
        <p className="cx-section-note">
          Agents show up in Trovis as soon as they report in. Connect opens
          the setup for that platform.
        </p>
        {health?.error && (
          <p className="cx-error" role="status">
            Couldn’t check which connections have reported in.{' '}
            <button type="button" className="btn-link-inline" onClick={loadHealth}>Retry</button>
          </p>
        )}
        <ul className="cx-list">
          {ai.map((c) => (
            <AiRow key={c.id} connector={c} health={healthFor(health, c.id)} onConnect={onConnect} />
          ))}
        </ul>
      </section>

      <section className="cx-section" aria-labelledby="cx-work">
        <h3 id="cx-work" className="cx-section-title">{CATEGORY_LABELS.work_system}</h3>
        <p className="cx-section-note">
          Systems where work waits on a payment, a deal, or an order.
        </p>
        {saas?.error && (
          <p className="cx-error" role="status">
            Couldn’t check which work systems are connected.{' '}
            <button type="button" className="btn-link-inline" onClick={loadSaas}>Retry</button>
          </p>
        )}
        <ul className="cx-list">
          {work.map((c) => (
            <WorkSystemRow
              key={c.id}
              connector={c}
              door={SAAS_DOORS[c.id]}
              saas={saas}
              health={healthFor(health, c.id)}
              healthPending={health === null}
              busy={busy}
              setBusy={setBusy}
              onChanged={reload}
            />
          ))}
        </ul>
      </section>

      {more.length > 0 && (
        <section className="cx-section cx-more" aria-labelledby="cx-more">
          <h3 id="cx-more" className="cx-section-title">More connections</h3>
          <p className="cx-section-note">
            Recognised when they show up in work today. A direct connection is coming.
          </p>
          <ul className="cx-more-list">
            {more.map((c) => (
              <li key={c.id} className="cx-more-item">
                <BrandMark id={c.brandId} size={14} />
                <span>{c.name}</span>
                <span className="cx-soon">Coming soon</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

function Mark({ connector }) {
  if (connector.brandId) return <BrandMark id={connector.brandId} size={20} />
  // No brand (the custom OpenTelemetry path): a neutral glyph, never a
  // borrowed logo.
  return (
    <span className="brand-mark cx-mark-generic" aria-hidden="true">
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6">
        <circle cx="10" cy="10" r="3" />
        <path d="M10 2v3M10 15v3M2 10h3M15 10h3" strokeLinecap="round" />
      </svg>
    </span>
  )
}

// A door, with state only when telemetry proved the path: an observed
// connector reads Connected and when it was last observed. No "installed"
// claim — nothing records that setup happened, so nothing is said.
function AiRow({ connector, health, onConnect }) {
  const connectable = isConnectable(connector)
  const st = aiRowState(health, relativeTime)
  return (
    <li className={`cx-row${connectable ? '' : ' is-soon'}${st.status ? ' is-connected' : ''}`} data-connector={connector.id}>
      <span className="cx-mark"><Mark connector={connector} /></span>
      <span className="cx-body">
        <span className="cx-name">
          {connector.name}
          {st.status && <span className="cx-status is-on">{st.status}</span>}
        </span>
        <span className="cx-desc">{connector.description}</span>
        {st.detail && (
          <span className="cx-observed" title={health?.last_observed_at || undefined}>{st.detail}</span>
        )}
      </span>
      <span className="cx-action">
        {connectable ? (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => onConnect?.(connector.id)}
            aria-label={`${st.action} ${connector.name}`}
          >
            {st.action}
          </button>
        ) : (
          <span className="cx-soon">Coming soon</span>
        )}
      </span>
    </li>
  )
}

// A work system with a durable OAuth row. Connected / Not connected is read
// from the server; everything else on the row is a door or a disconnect.
function WorkSystemRow({ connector, door, saas, health, healthPending, busy, setBusy, onChanged }) {
  const [error, setError] = useState(null)
  const [shop, setShop] = useState('')
  const row = (saas?.connections || []).find((c) => c.provider === connector.id)
  // The OAuth row decides the ACTIONS (connect vs disconnect); the health
  // row decides the WORDS. Authorization without observed activity is
  // "Waiting for data", not "Connected".
  const status = saasStatus(row)
  const words = workRowState(health, row, relativeTime, connector.name)
  const checking = saas === null || healthPending
  const canOauth = door.configured(saas)
  const mine = busy === connector.id
  const needsShop = !!door.needsShop
  const shopReady = !needsShop || !!shop.trim()

  async function connect() {
    setError(null)
    if (needsShop && !shop.trim()) {
      setError('Enter your Shopify store domain to connect.')
      return
    }
    setBusy(connector.id)
    try {
      const res = await door.start(needsShop ? shop.trim() : undefined)
      if (res?.authorize_url) {
        window.location.href = res.authorize_url
        return
      }
      setError(door.startError)
    } catch (e) {
      if (e?.status === 503) setError(door.notConfigured)
      else if (e?.status === 400 && needsShop) setError('Enter a valid Shopify store (your-store.myshopify.com).')
      else setError(`${door.startError} Please try again.`)
    } finally {
      setBusy(null)
    }
  }

  async function disconnect() {
    setError(null)
    setBusy(connector.id)
    try {
      await door.disconnect()
      await onChanged()
    } catch {
      setError(door.disconnectError)
    } finally {
      setBusy(null)
    }
  }

  return (
    <li className={`cx-row cx-row-work${status.connected ? ' is-connected' : ''}`} data-connector={connector.id}>
      <span className="cx-mark"><Mark connector={connector} /></span>
      <span className="cx-body">
        <span className="cx-name">
          {connector.name}
          {!checking && !saas?.error && (
            <span className={`cx-status${words.connected ? ' is-on' : ''}`}>{words.status}</span>
          )}
        </span>
        <span className="cx-desc">{connector.description}</span>
        {!checking && !saas?.error && words.detail && (
          <span className="cx-observed" title={health?.last_observed_at || undefined}>{words.detail}</span>
        )}
        {!status.connected && needsShop && !checking && (
          <input
            className="text-input cx-shop"
            type="text"
            value={shop}
            onChange={(e) => setShop(e.target.value)}
            placeholder={door.shopPlaceholder}
            aria-label="Shopify store domain"
            autoComplete="off"
            spellCheck={false}
          />
        )}
        {error && <span className="cx-error" role="alert">{error}</span>}
        <details className="cx-fineprint">
          <summary>What this connection does</summary>
          <p>{door.fineprint}</p>
        </details>
      </span>
      <span className="cx-action">
        {checking ? (
          <span className="cx-soon">Checking…</span>
        ) : status.connected ? (
          // Available, but quiet: a text link, not a red button.
          <button type="button" className="btn-link-inline cx-disconnect" onClick={disconnect} disabled={!!busy}>
            {mine ? 'Working…' : 'Disconnect'}
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={connect}
            disabled={!!busy || !canOauth || !shopReady}
            title={!canOauth ? door.notConfigured : undefined}
            aria-label={`Connect ${connector.name}`}
          >
            {mine ? 'Working…' : 'Connect'}
          </button>
        )}
      </span>
    </li>
  )
}
