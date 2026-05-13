import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { SendIcon, SparkleIcon } from './Icons.jsx'

// Conversational interface, wired to POST /ask. Stateless on the backend:
// the full thread ships with every request, which is how we get multi-turn
// follow-ups ("how many spans does it have?") to work without server-side
// session state.

const SUGGESTIONS = [
  'Which agents had the most errors today?',
  'What does my fleet look like overall?',
  'Are any agents misconfigured?',
  'Which agent is the slowest?',
  'How many tool calls did we make this week?',
  'Show me agents that have stopped reporting.',
]

export default function Ask() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [pending, setPending] = useState(false)
  const threadRef = useRef(null)

  useEffect(() => {
    if (threadRef.current) {
      threadRef.current.scrollTop = threadRef.current.scrollHeight
    }
  }, [messages, pending])

  async function submit(text) {
    const q = (text ?? input).trim()
    if (!q) return
    // The thread we DISPLAY: just the user's plain question. The thread
    // we SEND to the API: the latest user turn is prefixed with a
    // context block containing captured outputs from across the fleet,
    // so Claude can answer "what did my agent write today?"-style
    // questions. UI-vs-API divergence is intentional: we don't want the
    // user seeing a long preamble before their own question.
    const displayThread = [...messages, { role: 'user', content: q }]
    setMessages(displayThread)
    setInput('')
    setPending(true)

    try {
      const contextBlock = await buildOutputContext()
      const enrichedQ = contextBlock ? `${contextBlock}\n\n---\n\n${q}` : q
      // Replace just the latest user turn with the enriched version;
      // prior turns are sent as-is.
      const apiThread = displayThread.map((m, i, arr) =>
        i === arr.length - 1
          ? { role: 'user', content: enrichedQ }
          : { role: m.role, content: m.content },
      )
      const res = await api.ask(apiThread)
      setMessages((m) => [...m, { role: 'assistant', content: res.answer }])
    } catch (e) {
      setMessages((m) => [
        ...m,
        {
          role: 'assistant',
          content: `Couldn't answer: ${e.message}`,
          error: true,
        },
      ])
    } finally {
      setPending(false)
    }
  }

  // Fetch the fleet, then captured outputs per agent in parallel. If any
  // outputs exist anywhere, build a context block listing them. If none
  // exist, return a short instruction telling Claude to stick to
  // metadata so it doesn't hallucinate content. Failures degrade
  // silently — a network blip shouldn't break the question.
  async function buildOutputContext() {
    let agents
    try {
      agents = await api.listAgents()
    } catch {
      return ''
    }
    if (!agents || agents.length === 0) return ''

    const outputsByAgent = await Promise.all(
      agents.map((a) =>
        api.getAgentOutputs(a.service_name, 5).catch(() => []),
      ),
    )

    let anyOutputs = false
    const lines = ['Recent captured outputs across the user\'s agents:']
    for (let i = 0; i < agents.length; i++) {
      const outs = outputsByAgent[i] || []
      if (outs.length === 0) continue
      anyOutputs = true
      for (const o of outs) {
        const snippet = (o.content || '').replace(/\s+/g, ' ').slice(0, 400)
        lines.push(
          `- ${agents[i].service_name} [${o.content_type}] ${o.operation}: ${snippet}`,
        )
      }
    }

    if (!anyOutputs) {
      return (
        'Output capture is not enabled for any of the user\'s agents. ' +
        'You can describe agent metadata (operation names, span counts, ' +
        'error rates, durations) but you cannot quote the actual content ' +
        'of messages, responses, or tool results — that data has not been ' +
        'captured. If the user asks about content, suggest they enable ' +
        'capture by running `/oversee capture on` in their agent\'s chat.'
      )
    }
    return lines.join('\n')
  }

  function onSubmit(e) {
    e.preventDefault()
    submit()
  }

  if (messages.length === 0) {
    return (
      <div className="ask-shell">
        <div className="ask-empty">
          <span className="ask-empty-icon">
            <SparkleIcon size={24} />
          </span>
          <h2>Ask anything about your agents</h2>
          <p className="ask-empty-subtitle">
            Full context across every agent — what they do, how they're
            performing, where the errors are.
          </p>
          <div className="ask-suggestions">
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                type="button"
                className="ask-suggestion"
                onClick={() => submit(s)}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
        <AskInputRow
          value={input}
          onChange={setInput}
          onSubmit={onSubmit}
          pending={pending}
        />
      </div>
    )
  }

  return (
    <div className="ask-shell">
      <div className="ask-thread" ref={threadRef}>
        {messages.map((m, i) => (
          <div key={i} className={`ask-message ${m.role}`}>
            <div className="ask-bubble">{m.content}</div>
          </div>
        ))}
        {pending && (
          <div className="ask-message assistant">
            <div className="ask-bubble">
              <span className="spinner" /> Thinking…
            </div>
          </div>
        )}
      </div>
      <AskInputRow
        value={input}
        onChange={setInput}
        onSubmit={onSubmit}
        pending={pending}
      />
    </div>
  )
}

function AskInputRow({ value, onChange, onSubmit, pending }) {
  return (
    <form className="ask-input-row" onSubmit={onSubmit}>
      <input
        className="ask-input"
        type="text"
        placeholder="Ask anything about your agents…"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoFocus
      />
      <button
        type="submit"
        className="ask-send"
        disabled={pending || !value.trim()}
        aria-label="Send"
      >
        <SendIcon size={16} />
      </button>
    </form>
  )
}
