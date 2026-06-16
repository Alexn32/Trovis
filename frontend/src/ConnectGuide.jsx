import { useEffect, useRef, useState } from 'react'
import { api, getApiKey } from './api.js'
import { CodeBlock, computeOverseeEndpoint } from './AddAgent.jsx'
import { TrovisMark, SendIcon, CheckCircleIcon } from './Icons.jsx'

// The conversational "Set up with AI" flow. Trovis asks one question at a
// time (with quick-reply chips), emits copy-paste snippets carrying the
// user's real key + endpoint, answers free-text questions, and shows a live
// banner the moment the new agent's first telemetry lands. Backed by
// POST /connect/ask → asker.ask_connect (Opus). Stateless on the server —
// we post the full thread each turn.

// Hardcoded first turn so the guide opens instantly (no network round-trip).
// Included in the history we post, so the model continues from the answer.
const OPENING_TURN = {
  role: 'assistant',
  content:
    "Hey — I'm Trovis. I'll get your agent connected in a couple of minutes.\nWhat's your agent built with?",
  options: [
    'OpenAI Agents SDK',
    'Claude Agent SDK / Claude Code',
    'OpenClaw',
    'Hermes',
    'Custom Python / other',
  ],
  code: [],
}

// Turns posted to the backend (the model only needs role + content).
const MAX_HISTORY = 24

// Fill the user's real key/endpoint into a snippet at render time. The
// negative lookahead skips placeholders used as an env-var NAME
// (`export TROVIS_API_KEY=...`) so only value positions are substituted —
// the copy button then copies exactly what's shown.
function substitute(text, key, endpoint) {
  return (text || '')
    .replace(/TROVIS_ENDPOINT(?!\s*=)/g, endpoint)
    .replace(/TROVIS_API_KEY(?!\s*=)/g, key || 'ov_sk_…')
}

// Flatten an assistant turn (answer + its code snippets, placeholders intact)
// so the model recalls exactly what it already handed the user.
function flattenAssistant(m) {
  const codeText = (m.code || []).map((c) => c.content).join('\n\n')
  return codeText ? `${m.content}\n\n${codeText}` : m.content
}

export default function ConnectGuide({ active, onBack, onClose, onSkipToManual, onUpgrade }) {
  const [messages, setMessages] = useState([OPENING_TURN])
  const [input, setInput] = useState('')
  const [pending, setPending] = useState(false)
  // undefined = still loading; null = none in this session; string = the key.
  const [orgKey, setOrgKey] = useState(undefined)
  const endpoint = useRef(computeOverseeEndpoint()).current
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

  async function send(text) {
    const q = (text ?? input).trim()
    if (!q || pending) return
    setInput('')
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
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: r.answer,
          options: r.options || [],
          code: r.code || [],
        },
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

      <div className="connect-thread" ref={threadRef}>
        {messages.map((m, i) =>
          m.kind === 'connected' ? (
            <ConnectedBanner key={i} name={m.name} overLimit={m.overLimit} onUpgrade={onUpgrade} />
          ) : (
            <GuideBubble
              key={i}
              m={m}
              orgKey={orgKey}
              endpoint={endpoint}
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
        Skip — add manually
      </button>
    </div>
  )
}

function GuideBubble({ m, orgKey, endpoint, chipsEnabled, onPick }) {
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
            <CodeBlock code={substitute(c.content, orgKey, endpoint)} />
            {orgKey === null && c.content.includes('TROVIS_API_KEY') && (
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
                {o}
              </button>
            ))}
          </div>
        )}
      </div>
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
