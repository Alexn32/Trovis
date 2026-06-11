import { useState } from 'react'
import { getApiKey } from './api.js'
import { OpenAIIcon, AnthropicIcon, ActivityIcon, OpenClawIcon } from './Icons.jsx'

// Per-platform logo + brand color for the picker tiles. OpenClaw uses its own
// full-color lobster mark; OpenAI / Claude use their logomarks tinted to brand;
// Hermes (no public logo) gets a thematic glyph.
const PLATFORM_LOGOS = {
  openclaw:        { Icon: OpenClawIcon }, // self-colored
  'openai-agents': { Icon: OpenAIIcon,    color: '#10a37f' },
  claude:          { Icon: AnthropicIcon, color: '#d97757' },
  hermes:          { Icon: ActivityIcon,  color: 'var(--text-secondary)' },
}

// The two Claude variants shown on the sub-step after picking "Claude Agents".
// Each maps to the existing instructions platform id.
const CLAUDE_VARIANTS = [
  {
    id: 'claude-agent-sdk',
    label: 'Claude Agent SDK',
    subtitle: 'query() + ClaudeSDKClient — the Claude Code engine',
  },
  {
    id: 'claude-agents',
    label: 'Claude Managed Agents',
    subtitle: 'client.beta.agents + beta.sessions API',
  },
]

// ============================================================================
// AddAgent — the onboarding wizard.
// ----------------------------------------------------------------------------
// Step 1: choose a platform (always shown).
// Step 2: Claude only — choose the SDK flavor (other platforms skip this).
// Final step: platform-specific setup instructions, with a copyable Trovis
//             endpoint and API key at the top.
//
// All copy buttons render *already-substituted* code so what you see is
// exactly what gets copied to the clipboard.
// ============================================================================

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

// Only the platforms with first-party Trovis integrations are surfaced.
// Every tile maps to an instructions page in InstructionsView below —
// adding a tile here means adding (or restoring) its page there.
const PLATFORMS = [
  { id: 'openclaw',       label: 'OpenClaw',          subtitle: 'AI agent platform — agents connect themselves' },
  { id: 'openai-agents',  label: 'OpenAI Agents SDK', subtitle: 'OpenAI native agent framework' },
  // One Claude tile; a sub-step then splits SDK vs Managed Agents.
  { id: 'claude',         label: 'Claude Agents',     subtitle: 'Claude Agent SDK or Managed Agents' },
  { id: 'hermes',         label: 'Hermes Agent',      subtitle: 'Python agent platform — pip plugin' },
  // ChatGPT is intentionally not in the picker: OpenAI's MCP app registration
  // is pending. The MCP server + OAuth/Actions backend remain live and tested.
]

// ---------------------------------------------------------------------------
// Substitution
// ---------------------------------------------------------------------------

function effectiveAgentName(name) {
  return (name || '').trim() || 'my-agent-name'
}

function fill(text, agentName, endpoint) {
  return text
    .replaceAll('AGENT_NAME', effectiveAgentName(agentName))
    .replaceAll('TROVIS_ENDPOINT', endpoint)
}

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------

function CodeBlock({ code }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard API can fail on non-secure contexts; silently no-op so
      // the rest of the wizard keeps working.
    }
  }
  return (
    <div className="code-block">
      <button type="button" className="copy-btn" onClick={copy}>
        {copied ? '✓ Copied' : 'Copy'}
      </button>
      <pre className="code-pre">{code}</pre>
    </div>
  )
}

function Tabs({ tabs }) {
  const [active, setActive] = useState(0)
  return (
    <div>
      <div className="pill-tabs">
        {tabs.map((t, i) => (
          <button
            key={t.label}
            type="button"
            className={`pill-tab ${i === active ? 'pill-tab-active' : ''}`}
            onClick={() => setActive(i)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="tab-panel">{tabs[active].content}</div>
    </div>
  )
}

function NumberedStep({ n, title, children }) {
  return (
    <div className="numbered-step">
      <h4 className="numbered-step-title">
        <span className="numbered-step-n">{n}</span>
        <span>{title}</span>
      </h4>
      {children && <div className="numbered-step-body">{children}</div>}
    </div>
  )
}

function Callout({ variant = 'info', children }) {
  return <div className={`callout callout-${variant}`}>{children}</div>
}

function SuccessCallout() {
  return (
    <Callout variant="success">
      Once connected, your agent will appear on the Trovis dashboard within seconds.
    </Callout>
  )
}

// ---------------------------------------------------------------------------
// Wizard chrome
// ---------------------------------------------------------------------------

function StepIndicator({ step, total }) {
  return <div className="step-indicator">Step {step} of {total}</div>
}

function WizardHeader({ step, total, onBack, onClose }) {
  return (
    <div className="wizard-header">
      <div className="wizard-header-left">
        {onBack && (
          <button type="button" className="back-btn" onClick={onBack}>
            ← Back
          </button>
        )}
        <StepIndicator step={step} total={total} />
      </div>
      {/* No close button when embedded (e.g. inside onboarding) — the host owns chrome. */}
      {onClose && (
        <button type="button" className="close-btn" onClick={onClose} aria-label="Close">
          ×
        </button>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Step 1 — platform picker grid
// ---------------------------------------------------------------------------

function PlatformStep({ onSelect }) {
  return (
    <div>
      <h2 className="wizard-title">Choose your platform</h2>
      <p className="wizard-subtitle">
        Pick the closest match — we'll show you exactly what to do next.
      </p>
      <div className="platform-grid">
        {PLATFORMS.map((p) => {
          const logo = PLATFORM_LOGOS[p.id]
          const Logo = logo?.Icon
          return (
            <button
              key={p.id}
              type="button"
              className="platform-card"
              onClick={() => onSelect(p)}
            >
              {Logo && (
                <span className="platform-card-logo" style={{ color: logo.color }}>
                  <Logo size={20} />
                </span>
              )}
              <span className="platform-card-text">
                <span className="platform-card-label">{p.label}</span>
                <span className="platform-card-subtitle">{p.subtitle}</span>
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Instruction pages — OpenClaw
// ---------------------------------------------------------------------------

// The OTEL ingest endpoint agents send telemetry to. This is the Trovis
// API (Railway), NOT the dashboard origin (Vercel) — so we use VITE_API_URL
// (set at build time) and fall back to the production API, never the page
// origin, which would be wrong for the hosted dashboard.
const PRODUCTION_API = 'https://web-production-e6bc4.up.railway.app'

function computeOverseeEndpoint() {
  const base = import.meta.env.VITE_API_URL || PRODUCTION_API
  return base.replace(/\/+$/, '') + '/v1/traces'
}

function OpenClawInstructions() {
  const endpoint = computeOverseeEndpoint()
  const apiKey = getApiKey() || ''
  const installCmd = 'openclaw plugins install clawhub:@alexn32/openclaw-plugin'

  return (
    <>
      <h2 className="instructions-title">Connect OpenClaw agents</h2>
      <p className="instructions-subtitle">
        Install the plugin and every agent on this OpenClaw instance starts
        reporting telemetry to Trovis.
      </p>

      <Callout variant="blue">
        <strong>OpenClaw + Trovis:</strong> Install the plugin, connect
        through chat, and every agent is monitored automatically.
      </Callout>

      <PrefillBlock label="Your Trovis endpoint" value={endpoint} />
      <PrefillBlock
        label="Your API key"
        value={apiKey}
        placeholder="(no key in session — log in and try again)"
      />

      <Tabs
        tabs={[
          {
            label: 'Chat setup (recommended)',
            content: (
              <OpenClawChatSetup
                endpoint={endpoint}
                apiKey={apiKey}
                installCmd={installCmd}
              />
            ),
          },
          {
            label: 'Terminal setup',
            content: (
              <OpenClawTerminalSetup
                endpoint={endpoint}
                apiKey={apiKey}
                installCmd={installCmd}
              />
            ),
          },
        ]}
      />
    </>
  )
}

// ---------------------------------------------------------------------------
// Instructions page — OpenAI Agents SDK (trovis-agents pip package)
// ---------------------------------------------------------------------------
//
// Two-line install via the dedicated SDK we ship at trovis-agents/. The
// page pre-fills the operator's endpoint + API key the same way the
// OpenClaw page does so they can paste the full setup snippet without
// chasing values from elsewhere.

function OpenAIAgentsInstructions({ agentName, endpoint }) {
  const resolvedEndpoint = endpoint || computeOverseeEndpoint()
  const apiKey = getApiKey() || ''
  const installCmd = 'pip install trovis-agents[openai]'
  // The setup snippet uses our `fill()` substitution for AGENT_NAME +
  // TROVIS_ENDPOINT. The API key is substituted separately because
  // it's not in the standard placeholder set — we fall back to a
  // visible `ov_sk_…` placeholder when the user is logged out so the
  // snippet still reads cleanly.
  const setupCode = fill(
`from agents import Agent, Runner
from trovis import init

init(api_key="TROVIS_API_KEY", agent_name="AGENT_NAME")

# Your existing code — no changes needed
agent = Agent(name="Support", instructions="You handle customer tickets…")
result = await Runner.run(agent, "Help me with my order")`,
    agentName,
    resolvedEndpoint,
  ).replace('TROVIS_API_KEY', apiKey || 'ov_sk_…')

  return (
    <>
      <h2 className="instructions-title">Connect OpenAI Agents SDK</h2>
      <p className="instructions-subtitle">
        Two-line setup with the <code>trovis-agents</code> package.
        Your existing <code>Agent</code> and <code>Runner</code> code
        stays unchanged.
      </p>

      <PrefillBlock label="Your Trovis endpoint" value={resolvedEndpoint} />
      <PrefillBlock
        label="Your API key"
        value={apiKey}
        placeholder="(no key in session — log in and try again)"
      />

      <NumberedStep n={1} title="Install the SDK">
        <CodeBlock code={installCmd} />
      </NumberedStep>

      <NumberedStep n={2} title="Initialize at startup">
        <CodeBlock code={setupCode} />
      </NumberedStep>

      <NumberedStep n={3} title="Run your agent as you normally would">
        <p>
          Every <code>Agent()</code> you construct registers itself with
          Trovis on first creation. The agent's <code>name</code> and{' '}
          <code>instructions</code> become its identity — Trovis uses
          them to auto-generate a plain-English description on the
          dashboard.
        </p>
      </NumberedStep>

      <Callout variant="info">
        <strong>What gets captured by default:</strong> agent identity
        (name + system prompt), every LLM call (model, duration, tokens),
        every tool call, handoffs, guardrails, run completion. Message
        content is <em>not</em> captured unless you pass{' '}
        <code>capture_outputs=True</code> to <code>init()</code>.
      </Callout>

      <h3 className="section-title section-title-spaced">Environment variables</h3>
      <p>
        All <code>init()</code> args fall back to env vars — handy for
        containers and CI:
      </p>
      <ul>
        <li>
          <code>TROVIS_API_KEY</code> — your Trovis API key
        </li>
        <li>
          <code>TROVIS_ENDPOINT</code> — custom endpoint (defaults to
          the Trovis cloud)
        </li>
        <li>
          <code>TROVIS_AGENT_NAME</code> — default <code>service.name</code>
        </li>
        <li>
          <code>TROVIS_CAPTURE_OUTPUTS</code> — set to{' '}
          <code>true</code> for content capture
        </li>
      </ul>

      <SuccessCallout />
    </>
  )
}

// ---------------------------------------------------------------------------
// Instructions page — Anthropic Claude Managed Agents (trovis-agents pip package)
// ---------------------------------------------------------------------------
//
// Mirrors the OpenAI Agents SDK page. The trovis-agents package
// ships a `platform="anthropic"` mode that monkey-patches the
// anthropic SDK's beta.agents + beta.sessions resources to emit the
// same Trovis-named OTEL spans as every other agent platform.

function AnthropicAgentsInstructions({ agentName, endpoint }) {
  const resolvedEndpoint = endpoint || computeOverseeEndpoint()
  const apiKey = getApiKey() || ''
  const installCmd = 'pip install trovis-agents[anthropic]'
  const setupCode = fill(
`import anthropic
from trovis import init

init(api_key="TROVIS_API_KEY", agent_name="AGENT_NAME", platform="anthropic")

# Your existing code — no changes needed
client = anthropic.Anthropic()
agent = client.beta.agents.create(
    name="Coding Assistant",
    model={"id": "claude-opus-4-7"},
    system="You are a helpful coding assistant.",
    tools=[{"type": "agent_toolset_20260401"}],
)
session = client.beta.sessions.create(agent=agent.id, environment_id=env_id)

for event in client.beta.sessions.stream(session.id):
    ...  # your event handling — spans flow into Trovis automatically`,
    agentName,
    resolvedEndpoint,
  ).replace('TROVIS_API_KEY', apiKey || 'ov_sk_…')

  return (
    <>
      <h2 className="instructions-title">Connect Claude Managed Agents</h2>
      <p className="instructions-subtitle">
        Two-line setup with the <code>trovis-agents</code> package.
        Your <code>client.beta.agents.create</code> and{' '}
        <code>client.beta.sessions.stream</code> calls stay unchanged —
        Trovis patches the SDK transparently.
      </p>

      <PrefillBlock label="Your Trovis endpoint" value={resolvedEndpoint} />
      <PrefillBlock
        label="Your API key"
        value={apiKey}
        placeholder="(no key in session — log in and try again)"
      />

      <NumberedStep n={1} title="Install the SDK">
        <CodeBlock code={installCmd} />
      </NumberedStep>

      <NumberedStep n={2} title="Initialize at startup">
        <CodeBlock code={setupCode} />
      </NumberedStep>

      <NumberedStep n={3} title="Run your agent as you normally would">
        <p>
          Every <code>client.beta.agents.create(...)</code> call emits an
          agent_registration span with the agent's <code>name</code>,{' '}
          <code>system</code> prompt, model, and declared tools. Every
          event from <code>client.beta.sessions.stream(...)</code> —{' '}
          <code>user.message</code>, <code>agent.message</code>,{' '}
          <code>agent.tool_use</code>, <code>session.status_idle</code> —
          becomes its own span in the dashboard.
        </p>
      </NumberedStep>

      <Callout variant="info">
        <strong>What gets captured by default:</strong> agent identity
        (name + system prompt + model + tool list), every user message
        and agent response, every tool use, and run completion. Message
        content is <em>not</em> captured unless you pass{' '}
        <code>capture_outputs=True</code> to <code>init()</code>.
      </Callout>

      <h3 className="section-title section-title-spaced">Advanced: per-client instrumentation</h3>
      <p>
        If you don't want class-level monkey-patching (e.g. multi-tenant
        hosts), wrap a single client instead:
      </p>
      <CodeBlock
        code={`from trovis import init, monitor

init(api_key="${apiKey || 'ov_sk_…'}", platform="anthropic")
client = monitor(anthropic.Anthropic())
# Only this client emits Trovis spans.`}
      />

      <h3 className="section-title section-title-spaced">Environment variables</h3>
      <p>
        All <code>init()</code> args fall back to env vars — handy for
        containers and CI:
      </p>
      <ul>
        <li>
          <code>TROVIS_API_KEY</code> — your Trovis API key
        </li>
        <li>
          <code>TROVIS_ENDPOINT</code> — custom endpoint (defaults to
          the Trovis cloud)
        </li>
        <li>
          <code>TROVIS_AGENT_NAME</code> — default <code>service.name</code>
        </li>
        <li>
          <code>TROVIS_CAPTURE_OUTPUTS</code> — set to{' '}
          <code>true</code> for content capture
        </li>
      </ul>

      <SuccessCallout />
    </>
  )
}

// ---------------------------------------------------------------------------
// Instructions page — Claude Agent SDK (trovis-agents, query() patch)
// ---------------------------------------------------------------------------
//
// Distinct from "Claude Managed Agents" above: this is the
// claude-agent-sdk package (query() + ClaudeSDKClient, the Claude Code
// engine), NOT the anthropic.beta.agents API. The adapter wraps
// query() so each run's message stream becomes the usual Trovis spans.

function ClaudeAgentSdkInstructions({ agentName, endpoint }) {
  const resolvedEndpoint = endpoint || computeOverseeEndpoint()
  const apiKey = getApiKey() || ''
  const installCmd = 'pip install trovis-agents[claude-agent-sdk]'
  const setupCode = fill(
`from claude_agent_sdk import query, ClaudeAgentOptions
from trovis import init

# Call init() BEFORE importing/using query so the patch is in place.
init(api_key="TROVIS_API_KEY", agent_name="AGENT_NAME", platform="claude-agent-sdk")

# Your existing code — no changes needed
async for message in query(
    prompt="Refactor the auth module",
    options=ClaudeAgentOptions(system_prompt="You are a senior engineer."),
):
    ...  # handle messages as you already do`,
    agentName,
    resolvedEndpoint,
  ).replace('TROVIS_API_KEY', apiKey || 'ov_sk_…')

  return (
    <>
      <h2 className="instructions-title">Connect Claude Agent SDK</h2>
      <p className="instructions-subtitle">
        For the <code>claude-agent-sdk</code> package —{' '}
        <code>query()</code> and <code>ClaudeSDKClient</code>, the Claude
        Code engine. (Not the <code>beta.agents</code> Managed Agents
        API — that's the "Claude Managed Agents" tile.)
      </p>

      <Callout variant="blue">
        <strong>Which Claude tile?</strong> Use this one if your code
        calls <code>query(...)</code> or builds a{' '}
        <code>ClaudeSDKClient</code>. Use "Claude Managed Agents" if it
        calls <code>client.beta.agents.create(...)</code>.
      </Callout>

      <PrefillBlock label="Your Trovis endpoint" value={resolvedEndpoint} />
      <PrefillBlock
        label="Your API key"
        value={apiKey}
        placeholder="(no key in session — log in and try again)"
      />

      <NumberedStep n={1} title="Install the SDK">
        <CodeBlock code={installCmd} />
      </NumberedStep>

      <NumberedStep n={2} title="Initialize before your first query()">
        <CodeBlock code={setupCode} />
        <p style={{ marginTop: 8 }}>
          Order matters: call <code>init()</code> before{' '}
          <code>from claude_agent_sdk import query</code> elsewhere, so
          the instrumentation is in place when <code>query</code> is
          bound.
        </p>
      </NumberedStep>

      <NumberedStep n={3} title="Run your agent as you normally would">
        <p>
          Each run emits an <code>agent_registration</code> (from your{' '}
          <code>system_prompt</code>), a span per message
          (<code>message_received</code>, <code>llm_output</code>,{' '}
          <code>tool_call</code>), and an{' '}
          <code>agent_run_complete</code> carrying the run's token usage
          and cost.
        </p>
      </NumberedStep>

      <Callout variant="info">
        <strong>What gets captured by default:</strong> agent identity,
        message + tool-call metadata, token usage, and estimated cost.
        Message and response <em>content</em> are captured only when{' '}
        <code>capture_outputs=True</code> is passed to{' '}
        <code>init()</code>.
      </Callout>

      <h3 className="section-title section-title-spaced">Environment variables</h3>
      <ul>
        <li>
          <code>TROVIS_API_KEY</code> — your Trovis API key
        </li>
        <li>
          <code>TROVIS_ENDPOINT</code> — custom endpoint (defaults to
          the Trovis cloud)
        </li>
        <li>
          <code>TROVIS_AGENT_NAME</code> — default{' '}
          <code>service.name</code>
        </li>
        <li>
          <code>TROVIS_CAPTURE_OUTPUTS</code> — set to{' '}
          <code>true</code> for content capture
        </li>
      </ul>

      <SuccessCallout />
    </>
  )
}

// ---------------------------------------------------------------------------
// Instructions page — Hermes Agent (trovis-agents pip package, entry point)
// ---------------------------------------------------------------------------
//
// Hermes discovers plugins via Python entry points, so the install
// is `pip install trovis-agents[hermes]` plus `hermes plugins enable
// trovis` — no scaffold to copy around. Same span vocabulary as the
// OpenClaw plugin (and same `/trovis` chat command) so muscle memory
// transfers cleanly between platforms.

function HermesAgentsInstructions({ agentName, endpoint }) {
  const resolvedEndpoint = endpoint || computeOverseeEndpoint()
  const apiKey = getApiKey() || ''
  const installCmd = 'pip install trovis-agents[hermes]'
  const enableCmd = 'hermes plugins enable trovis'
  // Hermes prompts for these env vars at `plugins enable` time per the
  // plugin.yaml `requires_env` declaration; we also surface them here
  // so operators who'd rather set the shell env directly have a clear
  // recipe with their actual key/endpoint in place. Built with template
  // literals, not fill(): the placeholder names double as the shell
  // variable names here, so fill() would rewrite the left side of `=`.
  const envExport =
`export TROVIS_API_KEY="${apiKey || 'ov_sk_…'}"
export TROVIS_ENDPOINT="${resolvedEndpoint}"
export TROVIS_AGENT_NAME="${effectiveAgentName(agentName)}"`
  const chatCmds =
`/trovis connect ${resolvedEndpoint}
/trovis apikey ${apiKey || 'ov_sk_…'}
/trovis capture on
/trovis status`

  return (
    <>
      <h2 className="instructions-title">Connect Hermes Agent</h2>
      <p className="instructions-subtitle">
        One <code>pip install</code> + one <code>hermes plugins enable</code>.
        The plugin is bundled inside <code>trovis-agents</code> and exposed
        via a Python entry point, so Hermes finds it automatically.
      </p>

      <PrefillBlock label="Your Trovis endpoint" value={resolvedEndpoint} />
      <PrefillBlock
        label="Your API key"
        value={apiKey}
        placeholder="(no key in session — log in and try again)"
      />

      <NumberedStep n={1} title="Install the SDK">
        <CodeBlock code={installCmd} />
      </NumberedStep>

      <NumberedStep n={2} title="Enable the Trovis plugin in Hermes">
        <CodeBlock code={enableCmd} />
        <p style={{ marginTop: 8 }}>
          Hermes will prompt you for <code>TROVIS_API_KEY</code> the
          first time. You can also set these in your shell ahead of
          time:
        </p>
        <CodeBlock code={envExport} />
      </NumberedStep>

      <NumberedStep n={3} title="(Optional) Configure from chat">
        <p>
          Once Hermes is running, the plugin registers an{' '}
          <code>/trovis</code> slash command so you can adjust the
          connection without restarting:
        </p>
        <CodeBlock code={chatCmds} />
      </NumberedStep>

      <Callout variant="info">
        <strong>What gets captured by default:</strong> agent identity
        from <code>~/.hermes/SOUL.md</code> (sent once on gateway
        start), every <code>post_tool_call</code> as a{' '}
        <code>tool_call</code> span with the tool name and parameter
        keys. Tool results and <code>memory.md</code> are <em>not</em>{' '}
        captured unless you flip <code>TROVIS_CAPTURE_OUTPUTS=true</code>{' '}
        or run <code>/trovis capture on</code>.
      </Callout>

      <h3 className="section-title section-title-spaced">Manual install (alternative)</h3>
      <p>
        If your Hermes setup doesn't pick up entry-point plugins, drop
        the plugin directory in by hand:
      </p>
      <CodeBlock
        code={`cp -r $(python -c "import trovis.hermes_plugin, os; print(os.path.dirname(trovis.hermes_plugin.__file__))") ~/.hermes/plugins/trovis`}
      />

      <h3 className="section-title section-title-spaced">Environment variables</h3>
      <ul>
        <li>
          <code>TROVIS_API_KEY</code> — your Trovis API key
        </li>
        <li>
          <code>TROVIS_ENDPOINT</code> — custom endpoint (defaults to
          the Trovis cloud)
        </li>
        <li>
          <code>TROVIS_AGENT_NAME</code> — default{' '}
          <code>service.name</code> (defaults to <code>hermes-agent</code>)
        </li>
        <li>
          <code>TROVIS_CAPTURE_OUTPUTS</code> — set to{' '}
          <code>true</code> to include tool results + memory.md
        </li>
      </ul>

      <SuccessCallout />
    </>
  )
}

function PrefillBlock({ label, value, placeholder }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    if (!value) return
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // navigator.clipboard requires a secure context; degrade silently
    }
  }
  return (
    <div className="field" style={{ marginBottom: 14 }}>
      <label className="field-label">{label}</label>
      <div className="endpoint-display">
        <code className="endpoint-url">
          {value || placeholder || '(not set)'}
        </code>
        {value && (
          <button
            type="button"
            className="copy-btn-inline"
            onClick={copy}
          >
            {copied ? '✓ Copied' : 'Copy'}
          </button>
        )}
      </div>
    </div>
  )
}

function OpenClawChatSetup({ endpoint, apiKey, installCmd }) {
  const connectCmd = `/trovis connect ${endpoint}`
  const apikeyCmd = `/trovis apikey ${apiKey || 'YOUR_KEY'}`
  return (
    <>
      <p className="tab-subtitle">
        Paste these into any of your agent's chats. Each command runs in the
        OpenClaw gateway and applies to every agent on the instance.
      </p>

      <NumberedStep n={1} title="Install the Trovis plugin">
        <CodeBlock code={installCmd} />
        <p className="helper-text">
          Paste this to your agent or run in terminal — either works.
        </p>
      </NumberedStep>

      <NumberedStep n={2} title="Connect to your Trovis instance">
        <CodeBlock code={connectCmd} />
      </NumberedStep>

      <NumberedStep n={3} title="Set your API key">
        <CodeBlock code={apikeyCmd} />
        {!apiKey && (
          <p className="helper-text">
            We couldn't read your API key from this session — replace
            <code> YOUR_KEY</code> with the value from your dashboard.
          </p>
        )}
      </NumberedStep>

      <NumberedStep n={4} title="(Optional) Enable output capture">
        <CodeBlock code="/trovis capture on" />
        <p className="helper-text">
          Recommended — lets you see what your agents actually produce
          (messages, responses, tool results) in the dashboard.
        </p>
      </NumberedStep>

      <NumberedStep n={5} title="Verify">
        <CodeBlock code="/trovis status" />
      </NumberedStep>

      <SuccessCallout />
    </>
  )
}

function OpenClawTerminalSetup({ endpoint, apiKey, installCmd }) {
  const configCommands =
    `openclaw config set plugins.entries.trovis.config.endpoint "${endpoint}"\n` +
    `openclaw config set plugins.entries.trovis.config.apiKey "${apiKey || 'YOUR_KEY'}"`
  const captureCmd =
    'openclaw config set plugins.entries.trovis.config.captureOutputs true'
  return (
    <>
      <p className="tab-subtitle">
        Run these in the terminal where <code>openclaw</code> is installed.
      </p>

      <NumberedStep n={1} title="Install the Trovis plugin">
        <CodeBlock code={installCmd} />
      </NumberedStep>

      <NumberedStep n={2} title="Set the endpoint and API key">
        <CodeBlock code={configCommands} />
        {!apiKey && (
          <p className="helper-text">
            We couldn't read your API key from this session — replace
            <code> YOUR_KEY</code> with the value from your dashboard.
          </p>
        )}
      </NumberedStep>

      <NumberedStep n={3} title="(Optional) Enable output capture">
        <CodeBlock code={captureCmd} />
        <p className="helper-text">
          Recommended — lets you see what your agents actually produce.
        </p>
      </NumberedStep>

      <NumberedStep n={4} title="Restart the gateway">
        <CodeBlock code="openclaw gateway restart" />
      </NumberedStep>

      <SuccessCallout />
    </>
  )
}

// ---------------------------------------------------------------------------
// Dispatcher — picks the right instructions component
// ---------------------------------------------------------------------------

function InstructionsView({ platform, agentName, endpoint }) {
  if (platform === 'openclaw') {
    return <OpenClawInstructions agentName={agentName} endpoint={endpoint} />
  }
  if (platform === 'openai-agents') {
    return <OpenAIAgentsInstructions agentName={agentName} endpoint={endpoint} />
  }
  if (platform === 'claude-agents') {
    return <AnthropicAgentsInstructions agentName={agentName} endpoint={endpoint} />
  }
  if (platform === 'claude-agent-sdk') {
    return <ClaudeAgentSdkInstructions agentName={agentName} endpoint={endpoint} />
  }
  if (platform === 'hermes') {
    return <HermesAgentsInstructions agentName={agentName} endpoint={endpoint} />
  }
  // Unreachable from the picker — PLATFORMS only contains the live
  // integrations. Returning null is safer than rendering a wrong page
  // for an id we don't recognize.
  return null
}

// ---------------------------------------------------------------------------
// Top-level wizard component
// ---------------------------------------------------------------------------

export default function AddAgent({ onClose, embedded = false }) {
  const [platform, setPlatform] = useState(null)   // platform id, e.g. 'openclaw'
  const [claudeVariant, setClaudeVariant] = useState(null) // 'claude-agent-sdk' | 'claude-agents'
  // The ingest endpoint (VITE_API_URL → prod). Agents name themselves on
  // connect and are renamable on the dashboard, so there's no name input.
  const [endpoint] = useState(computeOverseeEndpoint())

  // Claude is the only platform with a sub-step (pick the SDK flavor)
  // at step 2 before the instructions.
  const needsClaudeVariant = platform === 'claude'
  const totalSteps = platform && !needsClaudeVariant ? 2 : 3

  // Derived step:
  //  - no platform yet                        → 1
  //  - platform needs a sub-step, none picked → 2
  //  - everything else                        → final (2 or 3)
  let step
  if (!platform) step = 1
  else if (needsClaudeVariant && !claudeVariant) step = 2
  else step = totalSteps

  function handleBack() {
    // "Most recent selection wins" — going back unsets the latest pick.
    if (claudeVariant) setClaudeVariant(null)
    else if (platform) setPlatform(null)
  }

  function handlePlatformPick(p) {
    setPlatform(p.id)
  }

  const onBack = step > 1 ? handleBack : null
  const showInstructions = step === totalSteps && step > 1 && !!platform

  // Claude resolves to one of the two real instruction platforms once a
  // variant is chosen; every other platform passes through unchanged.
  const effectivePlatform = needsClaudeVariant ? claudeVariant : platform

  return (
    <div className="add-agent">
      <WizardHeader
        step={step}
        total={totalSteps}
        onBack={onBack}
        onClose={embedded ? null : onClose}
      />

      {step === 1 && <PlatformStep onSelect={handlePlatformPick} />}
      {step === 2 && needsClaudeVariant && (
        <ClaudeVariantStep onSelect={(v) => setClaudeVariant(v.id)} />
      )}
      {showInstructions && (
        <InstructionsView
          platform={effectivePlatform}
          agentName=""
          endpoint={endpoint}
        />
      )}
    </div>
  )
}

// Sub-step after picking "Claude Agents": choose the SDK flavor.
function ClaudeVariantStep({ onSelect }) {
  return (
    <div>
      <h2 className="wizard-title">Which Claude setup?</h2>
      <p className="wizard-subtitle">
        Both report to Trovis the same way — pick the one your code uses.
      </p>
      <div className="platform-grid">
        {CLAUDE_VARIANTS.map((v) => (
          <button
            key={v.id}
            type="button"
            className="platform-card"
            onClick={() => onSelect(v)}
          >
            <span className="platform-card-logo" style={{ color: '#d97757' }}>
              <AnthropicIcon size={20} />
            </span>
            <span className="platform-card-text">
              <span className="platform-card-label">{v.label}</span>
              <span className="platform-card-subtitle">{v.subtitle}</span>
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}
