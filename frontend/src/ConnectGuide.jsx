import { useEffect, useRef, useState } from 'react'
import { api, getApiKey } from './api.js'
import { CodeBlock, computeGrokMcpUrl, computeOverseeEndpoint } from './AddAgent.jsx'
import { TrovisMark, SendIcon, CheckCircleIcon } from './Icons.jsx'
import { BrandMark, QuietBrand, WorksWithStrip } from './BrandMarks.jsx'
import { connectorForGuideOption, guideOpeningOptions, workSystemOptions } from './connectSetup.js'
import { getConnector } from './connectors.js'
import { doorFor } from './saasDoors.js'
// Placeholder → real key/endpoint substitution, and the wire-history
// flattening that keeps the placeholders. Extracted so both are covered by
// frontend/test/connectSnippets.test.mjs.
import {
  KEY_PLACEHOLDER,
  flattenAssistant,
  substitute,
} from './connectSnippets.js'

// The conversational "Set up with AI" flow. Trovis asks one question at a
// time (with quick-reply chips), emits copy-paste snippets carrying the
// user's real key + endpoint, answers free-text questions, and shows a live
// banner the moment the new agent's first telemetry lands. Backed by
// POST /connect/ask → asker.ask_connect (Opus). Stateless on the server —
// we post the full thread each turn.

// Local first turn so the guide opens instantly (no network round-trip).
// Included in the history we post, so the model continues from the answer.
// The chips are the registry's AI `guide_label`s plus the work systems
// (connectSetup.js), so a door added on the backend shows up here without a
// second list to maintain. This is the one Connect door: an AI worker, a
// work system or a custom source all start here.
const OPENING_TURN = {
  role: 'assistant',
  content:
    "Hey — I'm Trovis. What do you want Trovis to see?\nAn AI worker or agent platform, a work system like Stripe or Shopify, or something custom that emits OpenTelemetry.",
  options: [...guideOpeningOptions(), ...workSystemOptions()],
  code: [],
}

/** A registry work system (OAuth door) for a chip label or id, else null. */
function workSystemFor(labelOrId) {
  const c = connectorForGuideOption(labelOrId) || getConnector(labelOrId)
  return c && c.setup_type === 'oauth' ? c : null
}

// Turns posted to the backend (the model only needs role + content).
const MAX_HISTORY = 24

// `initialMessage` is the sentence the landing collected ("Our Grok Bot
// processes Shopify returns") — sent as the first user turn the moment the
// guide mounts. `initialConnector` is a registry id the landing or a deep
// link already chose; a work system renders its Connect card at once, with
// no model round-trip, because there is nothing to ask.
// `initialLocalTurn` ({content, options}) answers `initialMessage` locally —
// the landing's "which one?" intents — so the guide opens on a chip list the
// registry already holds instead of waiting on the model for it.
export default function ConnectGuide({
  active, onBack, onClose, onSkipToManual, onUpgrade,
  initialMessage = null, initialConnector = null, initialLocalTurn = null,
}) {
  const [messages, setMessages] = useState([OPENING_TURN])
  const seeded = useRef(false)
  const [input, setInput] = useState('')
  const [pending, setPending] = useState(false)
  // undefined = still loading; null = none in this session; string = the key.
  const [orgKey, setOrgKey] = useState(undefined)
  const endpoint = useRef(computeOverseeEndpoint()).current
  // The Grok Bot door hands out an MCP URL, not the ingest endpoint.
  const mcpUrl = useRef(computeGrokMcpUrl()).current
  const threadRef = useRef(null)
  const inputRef = useRef(null)

  // Resolve the org's API key once so snippets carry the real value. Session
  // users get it from /org/api-keys; api-key sessions fall back to the
  // in-memory key; otherwise we render the ov_sk_… placeholder + a note.
  useEffect(() => {
    let alive = true
    api
      .getApiKeys()
      .then((res) => {
        if (!alive) return
        setOrgKey(res?.keys?.[0]?.key || getApiKey() || null)
      })
      .catch(() => {
        if (alive) setOrgKey(getApiKey() || null)
      })
    return () => {
      alive = false
    }
  }, [])

  // Live connection detection: snapshot the current agents, then poll; when a
  // brand-new service_name appears, drop a local "connected" banner into the
  // thread. Runs while mounted (even hidden during a manual detour). The
  // banner is local-only — the model sees the new agent via the per-request
  // fleet context on its next turn.
  useEffect(() => {
    let alive = true
    let baseline = null
    const announced = new Set()
    async function tick() {
      try {
        const list = await api.listAgents()
        if (!alive || !Array.isArray(list)) return
        if (baseline === null) {
          baseline = new Set(list.map((a) => a.service_name).filter(Boolean))
          return
        }
        for (const a of list) {
          const name = a.service_name
          if (!name || baseline.has(name) || announced.has(name)) continue
          announced.add(name)
          const label = a.display_name || name
          // If this new agent pushed the account past its plan cap, it lands
          // view-locked — celebrate the connection but nudge to upgrade.
          let overLimit = false
          try {
            const u = await api.getAccountUsage()
            overLimit =
              !!u && u.agent_limit != null &&
              (u.locked_count > 0 || u.agent_count > u.agent_limit)
          } catch {
            /* best-effort — fall back to the plain "connected" banner */
          }
          if (!alive) return
          setMessages((prev) => [...prev, { kind: 'connected', name: label, overLimit }])
        }
      } catch {
        /* ignore — polling is best-effort */
      }
    }
    tick()
    const t = setInterval(tick, 5000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])

  // Keep the newest message visible; focus the input when the guide is shown.
  useEffect(() => {
    if (threadRef.current) threadRef.current.scrollTop = threadRef.current.scrollHeight
  }, [messages, pending])
  useEffect(() => {
    if (active && inputRef.current) inputRef.current.focus()
  }, [active])

  // Seed the thread from what the landing already learned (once).
  useEffect(() => {
    if (seeded.current) return
    seeded.current = true
    const ws = initialConnector ? workSystemFor(initialConnector) : null
    if (ws) {
      setMessages((prev) => [...prev, { kind: 'work_system', connectorId: ws.id }])
      return
    }
    if (initialMessage && initialLocalTurn && Array.isArray(initialLocalTurn.options)) {
      setMessages((prev) => [
        ...prev,
        { role: 'user', content: String(initialMessage) },
        { role: 'assistant', content: initialLocalTurn.content || '', options: initialLocalTurn.options, code: [] },
      ])
      return
    }
    if (initialMessage && String(initialMessage).trim()) send(String(initialMessage))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function send(text) {
    const q = (text ?? input).trim()
    if (!q || pending) return
    setInput('')
    // A work-system chip needs no model turn: the door is a button, and the
    // card says what the connection adds. The pick still lands in the thread
    // as the user's words, so the conversation reads straight.
    const ws = workSystemFor(q)
    if (ws) {
      setMessages((prev) => [
        ...prev,
        { role: 'user', content: q },
        { kind: 'work_system', connectorId: ws.id },
      ])
      return
    }
    setPending(true)
    // Build the wire history from real chat turns (skip local banners),
    // flattening assistant turns, then append the user's new message.
    const wire = messages
      .filter((m) => m.role === 'user' || m.role === 'assistant')
      .map((m) =>
        m.role === 'assistant'
          ? { role: 'assistant', content: flattenAssistant(m) }
          : { role: 'user', content: m.content },
      )
    wire.push({ role: 'user', content: q })
    // Functional update so a banner injected mid-flight isn't clobbered.
    setMessages((prev) => [...prev, { role: 'user', content: q }])
    try {
      const r = await api.askConnect(wire.slice(-MAX_HISTORY))
      // A work system the model named this turn gets its Connect card right
      // under the reply — the model says what it adds, the card does the
      // connecting. AI connectors in `connectors` need nothing extra here.
      const cards = (r.connectors || [])
        .map((id) => workSystemFor(id))
        .filter(Boolean)
        .map((c) => ({ kind: 'work_system', connectorId: c.id }))
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: r.answer,
          options: r.options || [],
          code: r.code || [],
        },
        ...cards,
      ])
    } catch (e) {
      const is503 = e?.status === 503 || String(e?.message || '').includes('503')
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: is503
            ? "The AI guide is unavailable right now — the backend needs an ANTHROPIC_API_KEY. You can still add your agent manually below."
            : 'Something went wrong answering that. Please try again.',
          options: [],
          code: [],
          error: !is503,
        },
      ])
    } finally {
      setPending(false)
    }
  }

  // Chips are only interactive on the latest assistant turn (and never while a
  // reply is pending) — older chips stay visible but disabled.
  let lastAssistantIdx = -1
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === 'assistant') {
      lastAssistantIdx = i
      break
    }
  }

  return (
    <div className="connect-guide">
      <div className="connect-head">
        <button type="button" className="back-btn" onClick={onBack}>
          ← Back
        </button>
        <span className="connect-head-title">
          <span className="dash-sq sm">
            <TrovisMark size={10} />
          </span>
          Set up with AI
        </span>
        {onClose && (
          <button
            type="button"
            className="close-btn"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        )}
      </div>

      {/* The guided door is where someone arrives with no idea what Trovis
          connects to. Show the same honest split the landing and onboarding
          show — doors that work today vs logos we merely recognise — so the
          assistant is never the only thing setting expectations. */}
      <WorksWithStrip className="connect-works-with" />

      <div className="connect-thread" ref={threadRef}>
        {messages.map((m, i) =>
          m.kind === 'connected' ? (
            <ConnectedBanner key={i} name={m.name} overLimit={m.overLimit} onUpgrade={onUpgrade} />
          ) : m.kind === 'work_system' ? (
            <WorkSystemConnectCard key={i} connectorId={m.connectorId} />
          ) : (
            <GuideBubble
              key={i}
              m={m}
              orgKey={orgKey}
              endpoint={endpoint}
              mcpUrl={mcpUrl}
              chipsEnabled={i === lastAssistantIdx && !pending}
              onPick={send}
            />
          ),
        )}
        {pending && (
          <div className="dash-ask-loading" aria-label="Thinking">
            <span />
            <span />
            <span />
          </div>
        )}
      </div>

      <form
        className="connect-input-row"
        onSubmit={(e) => {
          e.preventDefault()
          send()
        }}
      >
        <div className="dash-ask-input-wrap">
          <input
            ref={inputRef}
            className="dash-ask-input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Reply, or ask me anything…"
          />
          <button
            type="submit"
            className="dash-ask-send"
            disabled={!input.trim() || pending}
            aria-label="Send"
          >
            <SendIcon size={14} />
          </button>
        </div>
      </form>

      <button type="button" className="connect-skip" onClick={onSkipToManual}>
        Skip — set up an agent manually
      </button>
    </div>
  )
}

function GuideBubble({ m, orgKey, endpoint, mcpUrl, chipsEnabled, onPick }) {
  if (m.role === 'user') {
    return (
      <div className="dash-msg user">
        <div className="dash-bubble">{m.content}</div>
      </div>
    )
  }
  const code = m.code || []
  const options = m.options || []
  return (
    <div className="dash-msg ai">
      <div className="dash-bubble">
        <div className="dash-bubble-head">
          <span className="dash-sq sm">
            <TrovisMark size={9} />
          </span>
          TROVIS
        </div>
        <div className="dash-bubble-text">{m.content}</div>
        {code.map((c, ci) => (
          <div className="connect-code" key={ci}>
            {c.title && <div className="connect-code-title">{c.title}</div>}
            <CodeBlock code={substitute(c.content, orgKey, endpoint, mcpUrl)} />
            {orgKey === null && c.content.includes(KEY_PLACEHOLDER) && (
              <div className="connect-code-note">
                No key in this session — replace ov_sk_… with your key from Settings.
              </div>
            )}
          </div>
        ))}
        {options.length > 0 && (
          <div className="connect-chips">
            {options.map((o, oi) => (
              <button
                key={oi}
                type="button"
                className="connect-chip"
                disabled={!chipsEnabled}
                onClick={() => chipsEnabled && onPick(o)}
              >
                <QuietBrand texts={[o]} size={14} />
                {o}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// The work-system turn: the same OAuth door the Connections page uses
// (saasDoors.js), offered inside the conversation. It reads /saas/connections
// once so an already-connected system says so instead of offering a second
// authorization, asks for the shop domain where the door needs one, and
// hands off to the provider with a hard navigation — the OAuth return lands
// on Connections, where the row now reads Authorized / Waiting for data.
function WorkSystemConnectCard({ connectorId }) {
  const connector = getConnector(connectorId)
  const door = doorFor(connectorId)
  const [saas, setSaas] = useState(null)
  const [shop, setShop] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    api
      .getSaasConnections()
      .then((d) => alive && setSaas(d || { connections: [] }))
      .catch(() => alive && setSaas({ error: true, connections: [] }))
    return () => {
      alive = false
    }
  }, [])

  if (!connector || !door) return null
  const row = (saas?.connections || []).find((c) => c.provider === connector.id)
  const connected = !!row && row.status === 'connected'
  const canOauth = saas !== null && !saas.error && door.configured(saas)
  const needsShop = !!door.needsShop
  const shopReady = !needsShop || !!shop.trim()

  async function connect() {
    setError(null)
    if (needsShop && !shop.trim()) {
      setError('Enter your Shopify store domain to connect.')
      return
    }
    setBusy(true)
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
      setBusy(false)
    }
  }

  return (
    <div className="connect-card" data-connector={connector.id}>
      <div className="connect-card-head">
        {connector.brandId && <BrandMark id={connector.brandId} size={18} />}
        <strong>{connector.name}</strong>
        {connected && <span className="cx-status is-on">Connected</span>}
      </div>
      <p className="connect-card-desc">{connector.description}</p>
      {connected ? (
        <p className="connect-card-note">
          Already authorized{row.provider_account_id ? ` as ${row.provider_account_id}` : ''}.
          Trovis attaches {connector.name}&apos;s events to a run when the agent puts the
          run&apos;s id on the object it touches.
        </p>
      ) : (
        <>
          <p className="connect-card-note">
            Trovis asks you to authorize in {connector.name}; no code to paste. The
            connection adds {connector.name}&apos;s own view of outcomes to the runs it
            can link to.
          </p>
          {needsShop && (
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
          <button
            type="button"
            className="btn btn-primary btn-sm connect-card-cta"
            onClick={connect}
            disabled={busy || saas === null || !canOauth || !shopReady}
            title={saas !== null && !canOauth ? door.notConfigured : undefined}
            aria-label={`Connect ${connector.name}`}
          >
            {busy ? 'Working…' : saas === null ? 'Checking…' : `Connect ${connector.name}`}
          </button>
          {saas !== null && !saas.error && !canOauth && (
            <span className="connect-card-note">{door.notConfigured}</span>
          )}
        </>
      )}
      {error && <span className="cx-error" role="alert">{error}</span>}
    </div>
  )
}

function ConnectedBanner({ name, overLimit, onUpgrade }) {
  if (overLimit) {
    // The new agent pushed the account past its plan cap — it's recording, but
    // view-locked until they upgrade. Celebrate the connection, nudge to upgrade.
    return (
      <div className="connect-banner is-upgrade">
        <CheckCircleIcon size={15} />
        <span>
          <strong>{name}</strong> connected — it’s recording, but locked on your plan.{' '}
          {onUpgrade && (
            <button type="button" className="connect-banner-upgrade" onClick={onUpgrade}>
              Upgrade to view
            </button>
          )}
        </span>
      </div>
    )
  }
  return (
    <div className="connect-banner">
      <CheckCircleIcon size={15} />
      <span>
        <strong>{name}</strong> connected — telemetry flowing.
      </span>
    </div>
  )
}
