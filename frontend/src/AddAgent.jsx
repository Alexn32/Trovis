import { useState } from 'react'
import { getApiKey } from './api.js'
import { OpenAIIcon, AnthropicIcon, RobotIcon, ActivityIcon } from './Icons.jsx'

// Per-platform logo + brand color for the picker tiles. Real brands (OpenAI,
// Claude/Anthropic) use their logomarks; the in-house platforms (OpenClaw,
// Hermes) get thematic glyphs since they have no public logo.
const PLATFORM_LOGOS = {
  openclaw:         { Icon: RobotIcon,     color: 'var(--text-secondary)' },
  'openai-agents':  { Icon: OpenAIIcon,    color: '#10a37f' },
  'claude-agent-sdk': { Icon: AnthropicIcon, color: '#d97757' },
  'claude-agents':  { Icon: AnthropicIcon, color: '#d97757' },
  hermes:           { Icon: ActivityIcon,  color: 'var(--text-secondary)' },
}

// ============================================================================
// AddAgent — the three-step onboarding wizard.
// ----------------------------------------------------------------------------
// Step 1: choose a platform (always shown).
// Step 2: choose an LLM provider (skipped for platforms that don't need it).
// Step 3: platform/provider-specific setup instructions, with an editable
//         agent name and a copyable Oversee endpoint at the top.
//
// All copy buttons render *already-substituted* code so what you see is
// exactly what gets copied to the clipboard.
// ============================================================================

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

// Only the three platforms with first-party Oversee integrations
// are surfaced for now. Generic Python / Node / framework /
// no-code-product instruction pages still exist in this file — they
// just aren't reachable from the picker. Re-adding any tile to this
// array is enough to bring its page back.
const PLATFORMS = [
  { id: 'openclaw',       label: 'OpenClaw',                  subtitle: 'AI agent platform — agents connect themselves',  needsProvider: false },
  { id: 'openai-agents',  label: 'OpenAI Agents SDK',         subtitle: 'OpenAI native agent framework',                  needsProvider: false },
  { id: 'claude-agent-sdk', label: 'Claude Agent SDK',        subtitle: 'query() + ClaudeSDKClient (Claude Code engine)',  needsProvider: false },
  { id: 'claude-agents',  label: 'Claude Managed Agents',     subtitle: 'client.beta.agents + beta.sessions API',         needsProvider: false },
  { id: 'hermes',         label: 'Hermes Agent',              subtitle: 'Python agent platform — pip plugin',             needsProvider: false },
  // ChatGPT is intentionally not in the picker: OpenAI's MCP app registration
  // is pending. The MCP server + OAuth/Actions backend remain live and tested.
]

const PROVIDERS = [
  { id: 'anthropic', label: 'Anthropic (Claude)' },
  { id: 'openai',    label: 'OpenAI (GPT)' },
  { id: 'xai',       label: 'xAI (Grok)' },
  { id: 'google',    label: 'Google (Gemini)' },
  { id: 'bedrock',   label: 'AWS Bedrock' },
  { id: 'mistral',   label: 'Mistral' },
  { id: 'cohere',    label: 'Cohere' },
  { id: 'groq',      label: 'Groq' },
  { id: 'ollama',    label: 'Ollama (Local)' },
  { id: 'together',  label: 'Together AI' },
  { id: 'deepseek',  label: 'DeepSeek' },
  { id: 'multiple',  label: 'Multiple providers' },
  { id: 'other-llm', label: 'Other / Not listed' },
]

// Per-provider Python OTEL instrumentation package + import lines.
// Verified against PyPI as of May 2026 (per the build spec).
const PYTHON_PROVIDERS = {
  anthropic: {
    label: 'Anthropic (Claude)',
    pkg: 'opentelemetry-instrumentation-anthropic',
    importLines:
`from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
AnthropicInstrumentor().instrument()`,
  },
  openai: {
    label: 'OpenAI (GPT)',
    pkg: 'opentelemetry-instrumentation-openai',
    importLines:
`from opentelemetry.instrumentation.openai import OpenAIInstrumentor
OpenAIInstrumentor().instrument()`,
  },
  google: {
    label: 'Google (Gemini)',
    pkg: 'opentelemetry-instrumentation-google-generativeai',
    importLines:
`from opentelemetry.instrumentation.google_generativeai import GoogleGenerativeAiInstrumentor
GoogleGenerativeAiInstrumentor().instrument()`,
  },
  bedrock: {
    label: 'AWS Bedrock',
    pkg: 'opentelemetry-instrumentation-bedrock',
    importLines:
`from opentelemetry.instrumentation.bedrock import BedrockInstrumentor
BedrockInstrumentor().instrument()`,
  },
  mistral: {
    label: 'Mistral',
    pkg: 'opentelemetry-instrumentation-mistral',
    importLines:
`from opentelemetry.instrumentation.mistral import MistralInstrumentor
MistralInstrumentor().instrument()`,
  },
  cohere: {
    label: 'Cohere',
    pkg: 'opentelemetry-instrumentation-cohere',
    importLines:
`from opentelemetry.instrumentation.cohere import CohereInstrumentor
CohereInstrumentor().instrument()`,
  },
  groq: {
    label: 'Groq',
    pkg: 'opentelemetry-instrumentation-groq',
    importLines:
`from opentelemetry.instrumentation.groq import GroqInstrumentor
GroqInstrumentor().instrument()`,
  },
  ollama: {
    label: 'Ollama (Local)',
    pkg: 'opentelemetry-instrumentation-ollama',
    importLines:
`from opentelemetry.instrumentation.ollama import OllamaInstrumentor
OllamaInstrumentor().instrument()`,
  },
  together: {
    label: 'Together AI',
    pkg: 'opentelemetry-instrumentation-together-ai',
    importLines:
`from opentelemetry.instrumentation.together_ai import TogetherAiInstrumentor
TogetherAiInstrumentor().instrument()`,
  },
}

// Framework-level instrumentors (CrewAI / LangChain). OpenAI Agents SDK
// gets its own dedicated onboarding page (OpenAIAgentsInstructions),
// since the oversee-agents package handles setup in two lines and
// captures agent identity automatically.
const FRAMEWORK_INSTRUMENTORS = {
  crewai: {
    label: 'CrewAI',
    title: 'Connect CrewAI agents',
    pkg: 'openinference-instrumentation-crewai',
    importLines:
`from openinference.instrumentation.crewai import CrewAIInstrumentor
CrewAIInstrumentor().instrument()`,
    runFile: 'your_crew.py',
    note: 'If your CrewAI agents use LangChain internally, also install openinference-instrumentation-langchain and add LangChainInstrumentor().instrument().',
  },
  langchain: {
    label: 'LangChain / LangGraph',
    title: 'Connect LangChain / LangGraph agents',
    pkg: 'openinference-instrumentation-langchain',
    importLines:
`from openinference.instrumentation.langchain import LangChainInstrumentor
LangChainInstrumentor().instrument()`,
    runFile: 'your_agent.py',
  },
}

// ---------------------------------------------------------------------------
// Substitution
// ---------------------------------------------------------------------------

function effectiveAgentName(name) {
  return (name || '').trim() || 'my-agent-name'
}

function fill(text, agentName, endpoint) {
  return text
    .replaceAll('AGENT_NAME', effectiveAgentName(agentName))
    .replaceAll('OVERSEE_ENDPOINT', endpoint)
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

function AgentMessageBlock({ code }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {}
  }
  return (
    <div className="agent-message-block">
      <button type="button" className="copy-btn copy-btn-light" onClick={copy}>
        {copied ? '✓ Copied' : 'Copy message'}
      </button>
      <pre className="agent-message-pre">{code}</pre>
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
      Once connected, your agent will appear on the Oversee dashboard within seconds.
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
      <button type="button" className="close-btn" onClick={onClose} aria-label="Close">
        ×
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Step 1 + 2 — selection grids
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

function ProviderStep({ onSelect }) {
  return (
    <div>
      <h2 className="wizard-title">What LLM does your agent call?</h2>
      <p className="wizard-subtitle">
        We'll show you the exact tracing package for your provider.
      </p>
      <div className="platform-grid">
        {PROVIDERS.map((p) => (
          <button
            key={p.id}
            type="button"
            className="platform-card"
            onClick={() => onSelect(p)}
          >
            <span className="platform-card-label">{p.label}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Step 3 — header (agent name + endpoint)
// ---------------------------------------------------------------------------

function Step3Header({ agentName, setAgentName, endpoint }) {
  const [copied, setCopied] = useState(false)
  async function copyEndpoint() {
    try {
      await navigator.clipboard.writeText(endpoint)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {}
  }
  return (
    <div className="step3-header">
      <div className="field">
        <label className="field-label" htmlFor="agent-name-input">Agent name</label>
        <input
          id="agent-name-input"
          type="text"
          className="text-input"
          placeholder="my-agent-name"
          value={agentName}
          onChange={(e) => setAgentName(e.target.value)}
          autoComplete="off"
          spellCheck="false"
        />
        <p className="helper-text">This identifies your agent in Oversee.</p>
      </div>
      <div className="field">
        <label className="field-label">Oversee endpoint</label>
        <div className="endpoint-display">
          <code className="endpoint-url">{endpoint}</code>
          <button
            type="button"
            className="copy-btn-inline"
            onClick={copyEndpoint}
          >
            {copied ? '✓ Copied' : 'Copy'}
          </button>
        </div>
        <p className="helper-text">
          Where agents send telemetry. The Oversee SDKs target this
          automatically — you only set it manually for the OpenClaw plugin or
          raw OpenTelemetry setups.
        </p>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Reusable instruction patterns
// ---------------------------------------------------------------------------

const QUICK_ENV_TEMPLATE =
`OTEL_SERVICE_NAME=AGENT_NAME \\
OTEL_EXPORTER_OTLP_ENDPOINT=OVERSEE_ENDPOINT \\
OTEL_EXPORTER_OTLP_PROTOCOL=http/json \\
OTEL_TRACES_EXPORTER=otlp \\
python {RUN_FILE}`

const EXPLICIT_SETUP_TEMPLATE =
`from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry import trace

resource = Resource.create({"service.name": "AGENT_NAME"})
provider = TracerProvider(resource=resource)
provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint="OVERSEE_ENDPOINT"))
)
trace.set_tracer_provider(provider)

{IMPORT_LINES}`

function pythonQuickEnvCmd(agentName, endpoint, runFile) {
  return fill(QUICK_ENV_TEMPLATE.replace('{RUN_FILE}', runFile), agentName, endpoint)
}

function pythonExplicitSetup(importLines, agentName, endpoint) {
  return fill(
    EXPLICIT_SETUP_TEMPLATE.replace('{IMPORT_LINES}', importLines),
    agentName,
    endpoint,
  )
}

// Quick + Explicit tabs for any Python instrumentor-style integration.
function PythonInstrumentorTabs({
  pkg,
  importLines,
  agentName,
  endpoint,
  runFile = 'your_agent.py',
  preNote,
}) {
  const quickInstall = `pip install opentelemetry-distro opentelemetry-exporter-otlp ${pkg}`
  const explicitInstall = `pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http ${pkg}`
  const envCmd = pythonQuickEnvCmd(agentName, endpoint, runFile)
  const explicitSetup = pythonExplicitSetup(importLines, agentName, endpoint)

  return (
    <Tabs
      tabs={[
        {
          label: 'Quick setup',
          content: (
            <>
              {preNote && <Callout variant="info">{preNote}</Callout>}
              <NumberedStep n={1} title="Install packages">
                <CodeBlock code={quickInstall} />
              </NumberedStep>
              <NumberedStep n={2} title="Add these two lines to the top of your agent's entry file, before any imports">
                <CodeBlock code={importLines} />
              </NumberedStep>
              <NumberedStep n={3} title="Run your agent with these environment variables">
                <CodeBlock code={envCmd} />
              </NumberedStep>
              <NumberedStep n={4} title="Your agent will appear in Oversee within seconds." />
              <SuccessCallout />
            </>
          ),
        },
        {
          label: 'Explicit setup',
          content: (
            <>
              {preNote && <Callout variant="info">{preNote}</Callout>}
              <NumberedStep n={1} title="Install packages">
                <CodeBlock code={explicitInstall} />
              </NumberedStep>
              <NumberedStep n={2} title="Add this setup block to the top of your agent's entry file">
                <CodeBlock code={explicitSetup} />
              </NumberedStep>
              <NumberedStep n={3} title="Run your agent normally">
                <CodeBlock code={`python ${runFile}`} />
              </NumberedStep>
              <NumberedStep n={4} title="Your agent will appear in Oversee within seconds." />
              <SuccessCallout />
            </>
          ),
        },
      ]}
    />
  )
}

// ---------------------------------------------------------------------------
// Instruction pages — Custom Python
// ---------------------------------------------------------------------------

function CustomPythonInstructions({ provider, agentName, endpoint }) {
  // Special cases first.
  if (provider === 'xai') return <PythonXaiInstructions agentName={agentName} endpoint={endpoint} />
  if (provider === 'deepseek') return <PythonDeepSeekInstructions agentName={agentName} endpoint={endpoint} />
  if (provider === 'multiple') return <PythonMultipleInstructions agentName={agentName} endpoint={endpoint} />
  if (provider === 'other-llm') return <PythonGenericInstructions agentName={agentName} endpoint={endpoint} />

  const cfg = PYTHON_PROVIDERS[provider]
  return (
    <>
      <h2 className="instructions-title">Connect a Python agent using {cfg.label}</h2>
      <PythonInstrumentorTabs
        pkg={cfg.pkg}
        importLines={cfg.importLines}
        agentName={agentName}
        endpoint={endpoint}
      />
    </>
  )
}

function PythonXaiInstructions({ agentName, endpoint }) {
  const nativeInstall = 'pip install xai-sdk[telemetry-http]'
  const nativeSetup = fill(
`from xai_sdk.telemetry import Telemetry

telemetry = Telemetry()
telemetry.setup_otlp_exporter(
    endpoint="OVERSEE_ENDPOINT"
)`,
    agentName, endpoint,
  )

  return (
    <>
      <h2 className="instructions-title">Connect a Python agent using xAI (Grok)</h2>
      <Tabs tabs={[
        {
          label: 'Using xAI SDK (native)',
          content: (
            <>
              <NumberedStep n={1} title="Install packages">
                <CodeBlock code={nativeInstall} />
              </NumberedStep>
              <NumberedStep n={2} title="Add this to your agent before making any Grok calls">
                <CodeBlock code={nativeSetup} />
              </NumberedStep>
              <NumberedStep n={3} title="Run your agent normally. Traces flow automatically." />
              <SuccessCallout />
            </>
          ),
        },
        {
          label: 'Using OpenAI-compatible SDK',
          content: (
            <PythonInstrumentorTabs
              pkg={PYTHON_PROVIDERS.openai.pkg}
              importLines={PYTHON_PROVIDERS.openai.importLines}
              agentName={agentName}
              endpoint={endpoint}
              preNote={`If you call Grok via the OpenAI SDK with base_url="https://api.x.ai/v1", use these instructions.`}
            />
          ),
        },
      ]} />
    </>
  )
}

function PythonDeepSeekInstructions({ agentName, endpoint }) {
  return (
    <>
      <h2 className="instructions-title">Connect a Python agent using DeepSeek</h2>
      <Callout variant="info">
        DeepSeek uses an OpenAI-compatible API. Use the OpenAI instrumentation package.
      </Callout>
      <PythonInstrumentorTabs
        pkg={PYTHON_PROVIDERS.openai.pkg}
        importLines={PYTHON_PROVIDERS.openai.importLines}
        agentName={agentName}
        endpoint={endpoint}
      />
    </>
  )
}

function PythonMultipleInstructions({ agentName, endpoint }) {
  const install = 'pip install opentelemetry-distro opentelemetry-exporter-otlp opentelemetry-instrumentation-anthropic opentelemetry-instrumentation-openai opentelemetry-instrumentation-google-generativeai opentelemetry-instrumentation-bedrock'
  const setupLines =
`from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
from opentelemetry.instrumentation.openai import OpenAIInstrumentor

AnthropicInstrumentor().instrument()
OpenAIInstrumentor().instrument()`
  const envCmd = pythonQuickEnvCmd(agentName, endpoint, 'your_agent.py')

  return (
    <>
      <h2 className="instructions-title">Connect a Python agent calling multiple providers</h2>
      <NumberedStep n={1} title="Install all common instrumentation packages">
        <CodeBlock code={install} />
      </NumberedStep>
      <NumberedStep n={2} title="Add all instrumentor lines to the top of your agent's entry file">
        <CodeBlock code={setupLines} />
      </NumberedStep>
      <NumberedStep n={3} title="Run with OTEL environment variables (same as Quick setup)">
        <CodeBlock code={envCmd} />
      </NumberedStep>
      <Callout variant="info">
        Only the instrumentors for libraries you have installed will activate. The others are safely ignored.
      </Callout>
      <SuccessCallout />
    </>
  )
}

function PythonGenericInstructions({ agentName, endpoint }) {
  const setup = fill(
`from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry import trace

resource = Resource.create({"service.name": "AGENT_NAME"})
provider = TracerProvider(resource=resource)
provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint="OVERSEE_ENDPOINT"))
)
trace.set_tracer_provider(provider)

# Create spans for your agent's operations
tracer = trace.get_tracer("AGENT_NAME")
with tracer.start_as_current_span("my-operation") as span:
    span.set_attribute("custom.key", "value")
    # your agent logic here`,
    agentName, endpoint,
  )

  return (
    <>
      <h2 className="instructions-title">Generic OpenTelemetry setup</h2>
      <NumberedStep n={1} title="Install packages">
        <CodeBlock code="pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http" />
      </NumberedStep>
      <NumberedStep n={2} title="Add this setup block and instrument your operations">
        <CodeBlock code={setup} />
      </NumberedStep>
      <SuccessCallout />
    </>
  )
}

// ---------------------------------------------------------------------------
// Instruction pages — OpenClaw
// ---------------------------------------------------------------------------

// OpenClaw is special among the platforms: the user is already logged in
// to Oversee, so we can pre-fill the endpoint and their API key directly.
// The wizard's Step3Header agentName/endpoint fields are ignored here —
// the values that matter are the live session ones.
// The OTEL ingest endpoint agents send telemetry to. This is the Oversee
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
        reporting telemetry to Oversee.
      </p>

      <Callout variant="blue">
        <strong>OpenClaw + Oversee:</strong> Install the plugin, connect
        through chat, and every agent is monitored automatically.
      </Callout>

      <PrefillBlock label="Your Oversee endpoint" value={endpoint} />
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
// Instructions page — OpenAI Agents SDK (oversee-agents pip package)
// ---------------------------------------------------------------------------
//
// Two-line install via the dedicated SDK we ship at oversee-agents/. The
// page pre-fills the operator's endpoint + API key the same way the
// OpenClaw page does so they can paste the full setup snippet without
// chasing values from elsewhere.

function OpenAIAgentsInstructions({ agentName, endpoint }) {
  const resolvedEndpoint = endpoint || computeOverseeEndpoint()
  const apiKey = getApiKey() || ''
  const installCmd = 'pip install oversee-agents[openai]'
  // The setup snippet uses our `fill()` substitution for AGENT_NAME +
  // OVERSEE_ENDPOINT. The API key is substituted separately because
  // it's not in the standard placeholder set — we fall back to a
  // visible `ov_sk_…` placeholder when the user is logged out so the
  // snippet still reads cleanly.
  const setupCode = fill(
`from agents import Agent, Runner
from oversee import init

init(api_key="OVERSEE_API_KEY", agent_name="AGENT_NAME")

# Your existing code — no changes needed
agent = Agent(name="Support", instructions="You handle customer tickets…")
result = await Runner.run(agent, "Help me with my order")`,
    agentName,
    resolvedEndpoint,
  ).replace('OVERSEE_API_KEY', apiKey || 'ov_sk_…')

  return (
    <>
      <h2 className="instructions-title">Connect OpenAI Agents SDK</h2>
      <p className="instructions-subtitle">
        Two-line setup with the <code>oversee-agents</code> package.
        Your existing <code>Agent</code> and <code>Runner</code> code
        stays unchanged.
      </p>

      <PrefillBlock label="Your Oversee endpoint" value={resolvedEndpoint} />
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
          Oversee on first creation. The agent's <code>name</code> and{' '}
          <code>instructions</code> become its identity — Oversee uses
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
          <code>OVERSEE_API_KEY</code> — your Oversee API key
        </li>
        <li>
          <code>OVERSEE_ENDPOINT</code> — custom endpoint (defaults to
          the Oversee cloud)
        </li>
        <li>
          <code>OVERSEE_AGENT_NAME</code> — default <code>service.name</code>
        </li>
        <li>
          <code>OVERSEE_CAPTURE_OUTPUTS</code> — set to{' '}
          <code>true</code> for content capture
        </li>
      </ul>

      <SuccessCallout />
    </>
  )
}

// ---------------------------------------------------------------------------
// Instructions page — Anthropic Claude Managed Agents (oversee-agents pip package)
// ---------------------------------------------------------------------------
//
// Mirrors the OpenAI Agents SDK page. The oversee-agents package
// ships a `platform="anthropic"` mode that monkey-patches the
// anthropic SDK's beta.agents + beta.sessions resources to emit the
// same Oversee-named OTEL spans as every other agent platform.

function AnthropicAgentsInstructions({ agentName, endpoint }) {
  const resolvedEndpoint = endpoint || computeOverseeEndpoint()
  const apiKey = getApiKey() || ''
  const installCmd = 'pip install oversee-agents[anthropic]'
  const setupCode = fill(
`import anthropic
from oversee import init

init(api_key="OVERSEE_API_KEY", agent_name="AGENT_NAME", platform="anthropic")

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
    ...  # your event handling — spans flow into Oversee automatically`,
    agentName,
    resolvedEndpoint,
  ).replace('OVERSEE_API_KEY', apiKey || 'ov_sk_…')

  return (
    <>
      <h2 className="instructions-title">Connect Claude Managed Agents</h2>
      <p className="instructions-subtitle">
        Two-line setup with the <code>oversee-agents</code> package.
        Your <code>client.beta.agents.create</code> and{' '}
        <code>client.beta.sessions.stream</code> calls stay unchanged —
        Oversee patches the SDK transparently.
      </p>

      <PrefillBlock label="Your Oversee endpoint" value={resolvedEndpoint} />
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
        code={`from oversee import init, monitor

init(api_key="${apiKey || 'ov_sk_…'}", platform="anthropic")
client = monitor(anthropic.Anthropic())
# Only this client emits Oversee spans.`}
      />

      <h3 className="section-title section-title-spaced">Environment variables</h3>
      <p>
        All <code>init()</code> args fall back to env vars — handy for
        containers and CI:
      </p>
      <ul>
        <li>
          <code>OVERSEE_API_KEY</code> — your Oversee API key
        </li>
        <li>
          <code>OVERSEE_ENDPOINT</code> — custom endpoint (defaults to
          the Oversee cloud)
        </li>
        <li>
          <code>OVERSEE_AGENT_NAME</code> — default <code>service.name</code>
        </li>
        <li>
          <code>OVERSEE_CAPTURE_OUTPUTS</code> — set to{' '}
          <code>true</code> for content capture
        </li>
      </ul>

      <SuccessCallout />
    </>
  )
}

// ---------------------------------------------------------------------------
// Instructions page — Claude Agent SDK (oversee-agents, query() patch)
// ---------------------------------------------------------------------------
//
// Distinct from "Claude Managed Agents" above: this is the
// claude-agent-sdk package (query() + ClaudeSDKClient, the Claude Code
// engine), NOT the anthropic.beta.agents API. The adapter wraps
// query() so each run's message stream becomes the usual Oversee spans.

function ClaudeAgentSdkInstructions({ agentName, endpoint }) {
  const resolvedEndpoint = endpoint || computeOverseeEndpoint()
  const apiKey = getApiKey() || ''
  const installCmd = 'pip install oversee-agents[claude-agent-sdk]'
  const setupCode = fill(
`from claude_agent_sdk import query, ClaudeAgentOptions
from oversee import init

# Call init() BEFORE importing/using query so the patch is in place.
init(api_key="OVERSEE_API_KEY", agent_name="AGENT_NAME", platform="claude-agent-sdk")

# Your existing code — no changes needed
async for message in query(
    prompt="Refactor the auth module",
    options=ClaudeAgentOptions(system_prompt="You are a senior engineer."),
):
    ...  # handle messages as you already do`,
    agentName,
    resolvedEndpoint,
  ).replace('OVERSEE_API_KEY', apiKey || 'ov_sk_…')

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

      <PrefillBlock label="Your Oversee endpoint" value={resolvedEndpoint} />
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
          <code>OVERSEE_API_KEY</code> — your Oversee API key
        </li>
        <li>
          <code>OVERSEE_ENDPOINT</code> — custom endpoint (defaults to
          the Oversee cloud)
        </li>
        <li>
          <code>OVERSEE_AGENT_NAME</code> — default{' '}
          <code>service.name</code>
        </li>
        <li>
          <code>OVERSEE_CAPTURE_OUTPUTS</code> — set to{' '}
          <code>true</code> for content capture
        </li>
      </ul>

      <SuccessCallout />
    </>
  )
}

// ---------------------------------------------------------------------------
// Instructions page — Hermes Agent (oversee-agents pip package, entry point)
// ---------------------------------------------------------------------------
//
// Hermes discovers plugins via Python entry points, so the install
// is `pip install oversee-agents[hermes]` plus `hermes plugins enable
// oversee` — no scaffold to copy around. Same span vocabulary as the
// OpenClaw plugin (and same `/oversee` chat command) so muscle memory
// transfers cleanly between platforms.

function HermesAgentsInstructions({ agentName, endpoint }) {
  const resolvedEndpoint = endpoint || computeOverseeEndpoint()
  const apiKey = getApiKey() || ''
  const installCmd = 'pip install oversee-agents[hermes]'
  const enableCmd = 'hermes plugins enable oversee'
  // Hermes prompts for these env vars at `plugins enable` time per the
  // plugin.yaml `requires_env` declaration; we also surface them here
  // so operators who'd rather set the shell env directly have a clear
  // recipe with their actual key/endpoint in place.
  const envExport = fill(
`export OVERSEE_API_KEY="OVERSEE_API_KEY"
export OVERSEE_ENDPOINT="OVERSEE_ENDPOINT"
export OVERSEE_AGENT_NAME="AGENT_NAME"`,
    agentName,
    resolvedEndpoint,
  ).replace('OVERSEE_API_KEY', apiKey || 'ov_sk_…')
  const chatCmds =
`/oversee connect ${resolvedEndpoint}
/oversee apikey ${apiKey || 'ov_sk_…'}
/oversee capture on
/oversee status`

  return (
    <>
      <h2 className="instructions-title">Connect Hermes Agent</h2>
      <p className="instructions-subtitle">
        One <code>pip install</code> + one <code>hermes plugins enable</code>.
        The plugin is bundled inside <code>oversee-agents</code> and exposed
        via a Python entry point, so Hermes finds it automatically.
      </p>

      <PrefillBlock label="Your Oversee endpoint" value={resolvedEndpoint} />
      <PrefillBlock
        label="Your API key"
        value={apiKey}
        placeholder="(no key in session — log in and try again)"
      />

      <NumberedStep n={1} title="Install the SDK">
        <CodeBlock code={installCmd} />
      </NumberedStep>

      <NumberedStep n={2} title="Enable the Oversee plugin in Hermes">
        <CodeBlock code={enableCmd} />
        <p style={{ marginTop: 8 }}>
          Hermes will prompt you for <code>OVERSEE_API_KEY</code> the
          first time. You can also set these in your shell ahead of
          time:
        </p>
        <CodeBlock code={envExport} />
      </NumberedStep>

      <NumberedStep n={3} title="(Optional) Configure from chat">
        <p>
          Once Hermes is running, the plugin registers an{' '}
          <code>/oversee</code> slash command so you can adjust the
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
        captured unless you flip <code>OVERSEE_CAPTURE_OUTPUTS=true</code>{' '}
        or run <code>/oversee capture on</code>.
      </Callout>

      <h3 className="section-title section-title-spaced">Manual install (alternative)</h3>
      <p>
        If your Hermes setup doesn't pick up entry-point plugins, drop
        the plugin directory in by hand:
      </p>
      <CodeBlock
        code={`cp -r $(python -c "import oversee.hermes_plugin, os; print(os.path.dirname(oversee.hermes_plugin.__file__))") ~/.hermes/plugins/oversee`}
      />

      <h3 className="section-title section-title-spaced">Environment variables</h3>
      <ul>
        <li>
          <code>OVERSEE_API_KEY</code> — your Oversee API key
        </li>
        <li>
          <code>OVERSEE_ENDPOINT</code> — custom endpoint (defaults to
          the Oversee cloud)
        </li>
        <li>
          <code>OVERSEE_AGENT_NAME</code> — default{' '}
          <code>service.name</code> (defaults to <code>hermes-agent</code>)
        </li>
        <li>
          <code>OVERSEE_CAPTURE_OUTPUTS</code> — set to{' '}
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
  const connectCmd = `/oversee connect ${endpoint}`
  const apikeyCmd = `/oversee apikey ${apiKey || 'YOUR_KEY'}`
  return (
    <>
      <p className="tab-subtitle">
        Paste these into any of your agent's chats. Each command runs in the
        OpenClaw gateway and applies to every agent on the instance.
      </p>

      <NumberedStep n={1} title="Install the Oversee plugin">
        <CodeBlock code={installCmd} />
        <p className="helper-text">
          Paste this to your agent or run in terminal — either works.
        </p>
      </NumberedStep>

      <NumberedStep n={2} title="Connect to your Oversee instance">
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
        <CodeBlock code="/oversee capture on" />
        <p className="helper-text">
          Recommended — lets you see what your agents actually produce
          (messages, responses, tool results) in the dashboard.
        </p>
      </NumberedStep>

      <NumberedStep n={5} title="Verify">
        <CodeBlock code="/oversee status" />
      </NumberedStep>

      <SuccessCallout />
    </>
  )
}

function OpenClawTerminalSetup({ endpoint, apiKey, installCmd }) {
  const configCommands =
    `openclaw config set plugins.entries.oversee.config.endpoint "${endpoint}"\n` +
    `openclaw config set plugins.entries.oversee.config.apiKey "${apiKey || 'YOUR_KEY'}"`
  const captureCmd =
    'openclaw config set plugins.entries.oversee.config.captureOutputs true'
  return (
    <>
      <p className="tab-subtitle">
        Run these in the terminal where <code>openclaw</code> is installed.
      </p>

      <NumberedStep n={1} title="Install the Oversee plugin">
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
// Instruction pages — CrewAI / LangChain / OpenAI Agents (same shape)
// ---------------------------------------------------------------------------

function FrameworkInstructions({ frameworkId, agentName, endpoint }) {
  const cfg = FRAMEWORK_INSTRUMENTORS[frameworkId]
  return (
    <>
      <h2 className="instructions-title">{cfg.title}</h2>
      <PythonInstrumentorTabs
        pkg={cfg.pkg}
        importLines={cfg.importLines}
        agentName={agentName}
        endpoint={endpoint}
        runFile={cfg.runFile}
      />
      {cfg.note && <Callout variant="info">{cfg.note}</Callout>}
    </>
  )
}

// ---------------------------------------------------------------------------
// Instruction pages — Claude Cowork & Claude Code (native OTEL)
// ---------------------------------------------------------------------------

function ClaudeCoworkInstructions({ endpoint }) {
  return (
    <>
      <h2 className="instructions-title">Native OTEL export — no code changes needed</h2>
      <p className="instructions-subtitle">
        Claude Cowork has built-in OpenTelemetry support. Just enter your Oversee endpoint.
      </p>
      <NumberedStep n={1} title="Open Claude Desktop and go to Organization settings → Cowork (or Settings → Monitoring)." />
      <NumberedStep n={2} title="Enter your Oversee OTLP endpoint">
        <CodeBlock code={endpoint} />
      </NumberedStep>
      <NumberedStep n={3} title="Select the OTLP protocol: HTTP/JSON." />
      <NumberedStep n={4} title="Add authentication headers if required by your Oversee deployment." />
      <NumberedStep n={5} title="Click Save. Events begin flowing immediately." />
      <Callout variant="info">
        Requires Claude Team or Enterprise plan. Admin access required.
      </Callout>
      <h3 className="section-title section-title-spaced">What you'll see</h3>
      <p>
        User prompts, every tool and MCP invocation (server name, tool name,
        parameters, success/failure, execution time), file access patterns,
        and cost data.
      </p>
      <SuccessCallout />
    </>
  )
}

function ClaudeCodeInstructions({ endpoint }) {
  const settingsJson = fill(
`{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "OVERSEE_ENDPOINT"
  }
}`,
    '', endpoint,
  )
  return (
    <>
      <h2 className="instructions-title">Native OTEL export — no code changes needed</h2>
      <p className="instructions-subtitle">
        Claude Code has built-in OpenTelemetry support. Add settings and restart.
      </p>
      <NumberedStep n={1} title="Open your Claude Code settings file">
        <CodeBlock code="~/.claude/settings.json" />
      </NumberedStep>
      <NumberedStep n={2} title={`Add or merge this into the "env" section`}>
        <CodeBlock code={settingsJson} />
      </NumberedStep>
      <NumberedStep n={3} title="Restart Claude Code. Telemetry flows immediately." />
      <h3 className="section-title section-title-spaced">What you'll see</h3>
      <p>
        API requests, tool calls, token usage, cost data, and session activity.
      </p>
      <SuccessCallout />
    </>
  )
}

// ---------------------------------------------------------------------------
// Instruction pages — Node.js / TypeScript (any provider)
// ---------------------------------------------------------------------------

function NodeInstructions({ agentName, endpoint }) {
  const install = 'npm install @opentelemetry/api @opentelemetry/sdk-node @opentelemetry/exporter-trace-otlp-http @opentelemetry/auto-instrumentations-node'
  const tracing = fill(
`const { NodeSDK } = require('@opentelemetry/sdk-node');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { getNodeAutoInstrumentations } = require('@opentelemetry/auto-instrumentations-node');

const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter({
    url: 'OVERSEE_ENDPOINT',
  }),
  serviceName: 'AGENT_NAME',
  instrumentations: [getNodeAutoInstrumentations()],
});

sdk.start();`,
    agentName, endpoint,
  )

  return (
    <>
      <h2 className="instructions-title">Connect a Node.js / TypeScript agent</h2>
      <NumberedStep n={1} title="Install packages">
        <CodeBlock code={install} />
      </NumberedStep>
      <NumberedStep n={2} title="Create tracing.js in your project root">
        <CodeBlock code={tracing} />
      </NumberedStep>
      <NumberedStep n={3} title="Start your agent with this preload">
        <CodeBlock code="node --require ./tracing.js your-agent.js" />
      </NumberedStep>
      <NumberedStep n={4} title="Your agent will appear in Oversee within seconds." />
      <Callout variant="info">
        The auto-instrumentations-node package automatically traces HTTP calls,
        including calls to OpenAI, Anthropic, xAI, and other LLM APIs.
      </Callout>
      <SuccessCallout />
    </>
  )
}

// ---------------------------------------------------------------------------
// Instruction pages — Other (generic OTEL)
// ---------------------------------------------------------------------------

function OtherInstructions({ agentName, endpoint }) {
  const envBlock = fill(
`OTEL_SERVICE_NAME=AGENT_NAME
OTEL_EXPORTER_OTLP_ENDPOINT=OVERSEE_ENDPOINT
OTEL_EXPORTER_OTLP_PROTOCOL=http/json
OTEL_TRACES_EXPORTER=otlp`,
    agentName, endpoint,
  )

  return (
    <>
      <h2 className="instructions-title">Generic OpenTelemetry setup</h2>
      <p>
        Any application that exports OpenTelemetry traces via HTTP/JSON can connect to Oversee.
      </p>
      <NumberedStep n={1} title="Configure your application with these environment variables">
        <CodeBlock code={envBlock} />
      </NumberedStep>
      <p>
        Configure your application's OTEL exporter to send traces to the endpoint above.
        See the OpenTelemetry documentation for your language and framework.
      </p>
      <p>
        <a
          className="external-link"
          href="https://opentelemetry.io/docs/"
          target="_blank"
          rel="noreferrer"
        >
          OpenTelemetry Docs →
        </a>
      </p>
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
  // Unreachable from the picker — the platform list above only
  // contains the live integrations. Returning null is safer than
  // rendering a stale OtherInstructions page.
  return null
}

// ---------------------------------------------------------------------------
// Top-level wizard component
// ---------------------------------------------------------------------------

export default function AddAgent({ onClose }) {
  const [platform, setPlatform] = useState(null)   // platform id, e.g. 'custom-python'
  const [provider, setProvider] = useState(null)   // provider id, only when platform.needsProvider
  const [agentName, setAgentName] = useState('')
  // Defaults to the real Oversee API ingest endpoint (VITE_API_URL → prod).
  const [endpoint, setEndpoint] = useState(computeOverseeEndpoint())

  const selectedPlatform = PLATFORMS.find((p) => p.id === platform)
  const needsProvider = selectedPlatform?.needsProvider ?? false
  const totalSteps = selectedPlatform && !needsProvider ? 2 : 3

  // Derived step:
  //  - no platform yet            → 1
  //  - platform needs provider, none picked → 2
  //  - everything else            → final (2 or 3)
  let step
  if (!platform) step = 1
  else if (needsProvider && !provider) step = 2
  else step = totalSteps

  function handleBack() {
    // "Most recent selection wins" — going back unsets the latest pick.
    if (provider) setProvider(null)
    else if (platform) setPlatform(null)
  }

  function handlePlatformPick(p) {
    setPlatform(p.id)
    // If the user backs out and re-enters with a different platform that
    // doesn't need a provider, leave provider as-is — it's just ignored.
  }

  function handleProviderPick(p) {
    setProvider(p.id)
  }

  const onBack = step > 1 ? handleBack : null
  // The final step is always the instructions view — at step 3 when a provider
  // was chosen, or at step 2 when the platform skipped provider selection.
  const showInstructions = step === totalSteps && step > 1 && !!platform

  return (
    <div className="add-agent">
      <WizardHeader
        step={step}
        total={totalSteps}
        onBack={onBack}
        onClose={onClose}
      />

      {step === 1 && <PlatformStep onSelect={handlePlatformPick} />}
      {step === 2 && needsProvider && <ProviderStep onSelect={handleProviderPick} />}
      {showInstructions && (
        <>
          {platform !== 'openclaw' && (
            // OpenClaw auto-fills endpoint + key from the live session
            // (the user is already logged in to Oversee), so the generic
            // wizard inputs would just duplicate what OpenClawInstructions
            // displays at the top of its own section.
            <Step3Header
              agentName={agentName}
              setAgentName={setAgentName}
              endpoint={endpoint}
            />
          )}
          <InstructionsView
            platform={platform}
            agentName={agentName}
            endpoint={endpoint}
          />
        </>
      )}
    </div>
  )
}
