import { useEffect, useState } from 'react'
import { api, getApiKey } from './api.js'
import {
  OpenAIIcon,
  AnthropicIcon,
  ActivityIcon,
  OpenClawIcon,
  SparkleIcon,
  TrovisMark,
} from './Icons.jsx'
import ConnectGuide from './ConnectGuide.jsx'

// Per-platform logo + brand color for the picker tiles. OpenClaw uses its own
// full-color lobster mark; OpenAI / Claude use their logomarks tinted to brand;
// Hermes (no public logo) gets a thematic glyph.
const PLATFORM_LOGOS = {
  openclaw:        { Icon: OpenClawIcon }, // self-colored
  'openai-agents': { Icon: OpenAIIcon,    color: '#10a37f' },
  claude:          { Icon: AnthropicIcon, color: '#d97757' },
  hermes:          { Icon: ActivityIcon,  color: 'var(--text-secondary)' },
  chatgpt:         { Icon: OpenAIIcon,    color: 'var(--text-primary)' },
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
// AddAgent — the three-step onboarding wizard.
// ----------------------------------------------------------------------------
// Step 1: choose a platform (always shown).
// Step 2: choose an LLM provider (skipped for platforms that don't need it).
// Step 3: platform/provider-specific setup instructions, with an editable
//         agent name and a copyable Trovis endpoint at the top.
//
// All copy buttons render *already-substituted* code so what you see is
// exactly what gets copied to the clipboard.
// ============================================================================

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

// Only the three platforms with first-party Trovis integrations
// are surfaced for now. Generic Python / Node / framework /
// no-code-product instruction pages still exist in this file — they
// just aren't reachable from the picker. Re-adding any tile to this
// array is enough to bring its page back.
const PLATFORMS = [
  { id: 'openclaw',       label: 'OpenClaw',                  subtitle: 'AI agent platform — agents connect themselves',  needsProvider: false },
  { id: 'openai-agents',  label: 'OpenAI Agents SDK',         subtitle: 'OpenAI native agent framework',                  needsProvider: false },
  // One Claude tile; a sub-step then splits SDK vs Managed Agents.
  { id: 'claude',         label: 'Claude Agents',             subtitle: 'Claude Agent SDK or Managed Agents',             needsProvider: false },
  { id: 'hermes',         label: 'Hermes Agent',              subtitle: 'Python agent platform — pip plugin',             needsProvider: false },
  // A custom GPT built in ChatGPT: via GPT Actions (OAuth) it both reports its
  // own activity to Trovis AND can ask about the fleet (askFleet). No code.
  { id: 'chatgpt',        label: 'ChatGPT (custom GPT)',      subtitle: 'Monitor + query a GPT via Actions — no code',    needsProvider: false },
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
// since the trovis-agents package handles setup in two lines and
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
    .replaceAll('TROVIS_ENDPOINT', endpoint)
}

// ---------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------

export function CodeBlock({ code }) {
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
// Reusable instruction patterns
// ---------------------------------------------------------------------------

const QUICK_ENV_TEMPLATE =
`OTEL_SERVICE_NAME=AGENT_NAME \\
OTEL_EXPORTER_OTLP_ENDPOINT=TROVIS_ENDPOINT \\
OTEL_EXPORTER_OTLP_PROTOCOL=http/json \\
OTEL_TRACES_EXPORTER=otlp \\
OTEL_EXPORTER_OTLP_HEADERS=X-Trovis-Api-Key=TROVIS_API_KEY \\
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
    BatchSpanProcessor(OTLPSpanExporter(
        endpoint="TROVIS_ENDPOINT",
        headers={"X-Trovis-Api-Key": "TROVIS_API_KEY"},
    ))
)
trace.set_tracer_provider(provider)

{IMPORT_LINES}`

function pythonQuickEnvCmd(agentName, endpoint, runFile, apiKey) {
  return fill(QUICK_ENV_TEMPLATE.replace('{RUN_FILE}', runFile), agentName, endpoint)
    .replace('TROVIS_API_KEY', apiKey || 'ov_sk_…')
}

function pythonExplicitSetup(importLines, agentName, endpoint, apiKey) {
  return fill(
    EXPLICIT_SETUP_TEMPLATE.replace('{IMPORT_LINES}', importLines),
    agentName,
    endpoint,
  ).replace('TROVIS_API_KEY', apiKey || 'ov_sk_…')
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
  const apiKey = getApiKey() || ''
  const quickInstall = `pip install opentelemetry-distro opentelemetry-exporter-otlp ${pkg}`
  const explicitInstall = `pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http ${pkg}`
  const envCmd = pythonQuickEnvCmd(agentName, endpoint, runFile, apiKey)
  const explicitSetup = pythonExplicitSetup(importLines, agentName, endpoint, apiKey)

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
              <NumberedStep n={4} title="Your agent will appear in Trovis within seconds." />
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
              <NumberedStep n={4} title="Your agent will appear in Trovis within seconds." />
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
    endpoint="TROVIS_ENDPOINT"
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
  const apiKey = getApiKey() || ''
  const envCmd = pythonQuickEnvCmd(agentName, endpoint, 'your_agent.py', apiKey)

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
  const apiKey = getApiKey() || ''
  const setup = fill(
`from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry import trace

resource = Resource.create({"service.name": "AGENT_NAME"})
provider = TracerProvider(resource=resource)
provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(
        endpoint="TROVIS_ENDPOINT",
        headers={"X-Trovis-Api-Key": "TROVIS_API_KEY"},
    ))
)
trace.set_tracer_provider(provider)

# Create spans for your agent's operations
tracer = trace.get_tracer("AGENT_NAME")
with tracer.start_as_current_span("my-operation") as span:
    span.set_attribute("custom.key", "value")
    # your agent logic here`,
    agentName, endpoint,
  ).replace('TROVIS_API_KEY', apiKey || 'ov_sk_…')

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

// The OTEL ingest endpoint agents send telemetry to. This is the Trovis
// API (Railway), NOT the dashboard origin (Vercel) — so we use VITE_API_URL
// (set at build time) and fall back to the production API, never the page
// origin, which would be wrong for the hosted dashboard.
const PRODUCTION_API = 'https://web-production-e6bc4.up.railway.app'

export function computeOverseeEndpoint() {
  const base = import.meta.env.VITE_API_URL || PRODUCTION_API
  return base.replace(/\/+$/, '') + '/v1/traces'
}

// Canonical custom-domain host for the OAuth / GPT-Actions flow. Deliberately
// NOT derived from VITE_API_URL: ChatGPT users must never see the raw platform
// hostname, and this must match the `servers` URL the backend advertises in
// /actions/openapi.json (set via the API_URL env var). If that domain ever
// changes, update it here and in the backend env together.
const TROVIS_ACTIONS_HOST = 'https://api.trovisai.com'

// The OAuth client_id the GPT Action authenticates with. Public (not a secret)
// and must match the backend's OAUTH_CLIENT_ID (default "oversee-chatgpt" — a
// registered value, not a brand identifier, so it stays as-is).
const _OAUTH_CLIENT_ID_PUBLIC = 'oversee-chatgpt'

function OpenClawInstructions() {
  const endpoint = computeOverseeEndpoint()
  const apiKey = getApiKey() || ''
  const installCmd = 'openclaw plugins install clawhub:@trovis/openclaw-plugin'

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
  // recipe with their actual key/endpoint in place. The negative lookahead
  // fills only the quoted VALUES — `fill()` would clobber the var names too.
  const envExport =
`export TROVIS_API_KEY="TROVIS_API_KEY"
export TROVIS_ENDPOINT="TROVIS_ENDPOINT"
export TROVIS_AGENT_NAME="AGENT_NAME"`
    .replace(/TROVIS_API_KEY(?!=)/g, apiKey || 'ov_sk_…')
    .replace(/TROVIS_ENDPOINT(?!=)/g, resolvedEndpoint)
    .replace(/AGENT_NAME(?!=)/g, effectiveAgentName(agentName))
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

// ---------------------------------------------------------------------------
// Instructions page — ChatGPT custom GPT (GPT Actions + OAuth)
// ---------------------------------------------------------------------------
//
// Unlike every other tile, there's no code and no SDK: the user adds Trovis as
// an *Action* to a GPT they build in ChatGPT. The GPT then calls our
// /actions/* endpoints (OAuth-authed) as it works — connect/log/complete to
// report its activity, and askFleet to answer the user's questions about the
// fleet. Everything shown here points at the branded Actions host, never the
// raw platform URL, matching the OAuth consent page + the OpenAPI `servers` URL.

function ChatGPTInstructions() {
  const schemaUrl = `${TROVIS_ACTIONS_HOST}/actions/openapi.json`
  const oauthConfig =
`Client ID:          ${_OAUTH_CLIENT_ID_PUBLIC}
Client secret:      <your OAUTH_CLIENT_SECRET>
Authorization URL:  ${TROVIS_ACTIONS_HOST}/oauth/authorize
Token URL:          ${TROVIS_ACTIONS_HOST}/oauth/token
Scope:              (leave blank)
Token exchange:     Default (POST request)`
  const gptInstructions =
`You are connected to Trovis, the user's agent monitoring system.
- At the START of each conversation, call connectAgent with your name, your role, and a one-line description of what you do.
- As you finish each meaningful step, call logActivity with a short step name and description.
- When the task is done, call reportComplete with a one-line summary of what you accomplished.
- Whenever the user asks ANYTHING about their agents or fleet (e.g. "what was the last agent that ran?", "what did my agents do today?", "which ones are drifting?", "what did I spend?"), call askFleet with their question and answer from the result. For a plain list of agents use listAgents; for a timeline of recent runs use recentActivity.
Do the connect/log/complete calls silently in the background — don't mention Trovis unless the user asks.`

  return (
    <>
      <h2 className="instructions-title">Connect a custom GPT (ChatGPT)</h2>
      <p className="instructions-subtitle">
        Add Trovis as an <strong>Action</strong> on a GPT you build in ChatGPT.
        No code — the GPT reports what it does and shows up in your fleet.
      </p>

      <Callout variant="blue">
        <strong>What this does:</strong> your GPT reports its own activity to
        Trovis as it works (start, each step, completion), so it lands on the
        dashboard like any other agent — <em>and</em> it can answer your
        questions about the whole fleet ("what was the last agent that ran?")
        by querying Trovis back.
      </Callout>

      <PrefillBlock label="OpenAPI schema URL (import this into your GPT)" value={schemaUrl} />

      <NumberedStep n={1} title="Open your GPT's Actions">
        <p>
          In ChatGPT, go to your GPT → <strong>Configure</strong> →{' '}
          <strong>Create new action</strong> (under Actions). You'll need
          a GPT you own — create one first if you haven't.
        </p>
      </NumberedStep>

      <NumberedStep n={2} title="Import the Trovis schema">
        <p>Use “Import from URL” and paste:</p>
        <CodeBlock code={schemaUrl} />
        <p className="helper-text">
          This registers seven operations on your GPT: connectAgent,
          logActivity, reportComplete, checkStatus, askFleet, listAgents,
          and recentActivity.
        </p>
      </NumberedStep>

      <NumberedStep n={3} title="Set Authentication to OAuth">
        <p>Choose <strong>OAuth</strong> and enter:</p>
        <CodeBlock code={oauthConfig} />
        <p className="helper-text">
          The client secret is the <code>OAUTH_CLIENT_SECRET</code> you set on
          your Trovis backend — not shown here. When the GPT first runs,
          ChatGPT sends the user to the Trovis sign-in page to authorize.
        </p>
      </NumberedStep>

      <NumberedStep n={4} title="Add a privacy policy URL (if prompted)">
        <CodeBlock code="https://trovisai.com/privacy" />
      </NumberedStep>

      <NumberedStep n={5} title="Tell the GPT to report to — and query — Trovis">
        <p>Paste this into the GPT's <strong>Instructions</strong>:</p>
        <AgentMessageBlock code={gptInstructions} />
      </NumberedStep>

      <NumberedStep n={6} title="Save. Run your GPT — it authorizes once, then appears in Trovis within seconds." />

      <h3 className="section-title section-title-spaced">Prefer a custom MCP connector?</h3>
      <p>
        If your ChatGPT plan supports custom connectors, you can point one at{' '}
        <code>{`${TROVIS_ACTIONS_HOST}/sse`}</code> (or{' '}
        <code>{`${TROVIS_ACTIONS_HOST}/mcp`}</code>) with a Bearer{' '}
        Trovis API key instead of Actions. Same monitoring, different transport.
      </p>

      <Callout variant="info">
        <strong>What gets captured:</strong> the agent's name + role, each step
        you tell it to log, and task completions. Everything runs on OpenAI's
        side, so token-level cost isn't available for GPT-Action agents — you
        see their activity, not per-call spend.
      </Callout>

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

      <NumberedStep n={4} title="Turn on output capture (recommended)">
        <CodeBlock code="/trovis capture on" />
        <p className="helper-text">
          <strong>Do this or you'll only see metadata.</strong> Without capture,
          Trovis records what ran, when, and how much it cost — but <em>not</em>{' '}
          the actual messages, responses, or tool results, so you can't read
          what your agent said or ask about its outputs. Turning it on sends
          that content to Trovis; leave it off only if that's a concern.
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

      <NumberedStep n={3} title="Turn on output capture (recommended)">
        <CodeBlock code={captureCmd} />
        <p className="helper-text">
          <strong>Do this or you'll only see metadata.</strong> Without capture,
          Trovis records what ran, when, and cost — but <em>not</em> the actual
          messages, responses, or tool results, so you can't read what your
          agent said or ask about its outputs. Turning it on sends that content
          to Trovis; leave it off only if that's a concern.
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
        Claude Cowork has built-in OpenTelemetry support. Just enter your Trovis endpoint.
      </p>
      <NumberedStep n={1} title="Open Claude Desktop and go to Organization settings → Cowork (or Settings → Monitoring)." />
      <NumberedStep n={2} title="Enter your Trovis OTLP endpoint">
        <CodeBlock code={endpoint} />
      </NumberedStep>
      <NumberedStep n={3} title="Select the OTLP protocol: HTTP/JSON." />
      <NumberedStep n={4} title="Add authentication headers if required by your Trovis deployment." />
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
  const apiKey = getApiKey() || ''
  const settingsJson = fill(
`{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "TROVIS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS": "X-Trovis-Api-Key=TROVIS_API_KEY"
  }
}`,
    '', endpoint,
  ).replace('TROVIS_API_KEY', apiKey || 'ov_sk_…')
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
        API requests, tool calls, and session activity.
      </p>
      <SuccessCallout />
    </>
  )
}

// ---------------------------------------------------------------------------
// Instruction pages — Node.js / TypeScript (any provider)
// ---------------------------------------------------------------------------

function NodeInstructions({ agentName, endpoint }) {
  const apiKey = getApiKey() || ''
  const install = 'npm install @opentelemetry/api @opentelemetry/sdk-node @opentelemetry/exporter-trace-otlp-http @opentelemetry/auto-instrumentations-node'
  const tracing = fill(
`const { NodeSDK } = require('@opentelemetry/sdk-node');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const { getNodeAutoInstrumentations } = require('@opentelemetry/auto-instrumentations-node');

const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter({
    url: 'TROVIS_ENDPOINT',
    headers: { 'X-Trovis-Api-Key': 'TROVIS_API_KEY' },
  }),
  serviceName: 'AGENT_NAME',
  instrumentations: [getNodeAutoInstrumentations()],
});

sdk.start();`,
    agentName, endpoint,
  ).replace('TROVIS_API_KEY', apiKey || 'ov_sk_…')

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
      <NumberedStep n={4} title="Your agent will appear in Trovis within seconds." />
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
  const apiKey = getApiKey() || ''
  const envBlock = fill(
`OTEL_SERVICE_NAME=AGENT_NAME
OTEL_EXPORTER_OTLP_ENDPOINT=TROVIS_ENDPOINT
OTEL_EXPORTER_OTLP_PROTOCOL=http/json
OTEL_TRACES_EXPORTER=otlp
OTEL_EXPORTER_OTLP_HEADERS=X-Trovis-Api-Key=TROVIS_API_KEY`,
    agentName, endpoint,
  ).replace('TROVIS_API_KEY', apiKey || 'ov_sk_…')

  return (
    <>
      <h2 className="instructions-title">Generic OpenTelemetry setup</h2>
      <p>
        Any application that exports OpenTelemetry traces via HTTP/JSON can connect to Trovis.
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
  if (platform === 'chatgpt') {
    return <ChatGPTInstructions />
  }
  // Unreachable from the picker — the platform list above only
  // contains the live integrations. Returning null is safer than
  // rendering a stale OtherInstructions page.
  return null
}

// ---------------------------------------------------------------------------
// Top-level wizard component
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Top-level shell — landing → AI guide | manual wizard
// ---------------------------------------------------------------------------

export default function AddAgent({ onClose, embedded = false, onUpgrade }) {
  const [view, setView] = useState('landing') // 'landing' | 'guide' | 'manual'
  // Once visited, the guide stays MOUNTED (hidden) across guide↔manual
  // switches so the chat history and connect-poll baseline survive a detour.
  const [guideVisited, setGuideVisited] = useState(false)

  // At-limit awareness: a non-blocking nudge when the account is already at its
  // plan's agent cap. Telemetry is NEVER blocked (cardinal rule) — connecting
  // still works; the new agent just lands view-locked until they upgrade.
  // Skipped when embedded (onboarding starts at 0 agents, owns its own chrome).
  const [usage, setUsage] = useState(null)
  useEffect(() => {
    if (embedded) return undefined
    let alive = true
    api.getAccountUsage().then((u) => alive && setUsage(u)).catch(() => {})
    return () => { alive = false }
  }, [embedded])
  const atLimit =
    !embedded && usage?.agent_limit != null && usage.agent_count >= usage.agent_limit

  return (
    <div className="add-agent">
      {atLimit && (
        <div className="aa-limit-banner">
          <div className="aa-limit-text">
            <strong>You’re at your plan’s limit ({usage.agent_count} of {usage.agent_limit} agents).</strong>{' '}
            You can still connect this one — it’s recorded immediately — but it stays
            locked until you upgrade.
          </div>
          {onUpgrade && (
            <button type="button" className="btn btn-primary aa-limit-cta" onClick={onUpgrade}>
              Upgrade plan
            </button>
          )}
        </div>
      )}
      {view === 'landing' && (
        <AddAgentLanding
          onStartGuide={() => {
            setGuideVisited(true)
            setView('guide')
          }}
          onManual={() => setView('manual')}
          onClose={embedded ? null : onClose}
        />
      )}
      {guideVisited && (
        <div style={{ display: view === 'guide' ? undefined : 'none' }}>
          <ConnectGuide
            active={view === 'guide'}
            onBack={() => setView('landing')}
            onClose={embedded ? null : onClose}
            onSkipToManual={() => setView('manual')}
            onUpgrade={onUpgrade}
          />
        </div>
      )}
      {view === 'manual' && (
        <ManualWizard
          onClose={onClose}
          embedded={embedded}
          onBackToLanding={() => setView('landing')}
        />
      )}
    </div>
  )
}

// The hero shown when the Add Agent overlay opens: one primary path (the AI
// guide) and one secondary (the classic platform-picker wizard).
function AddAgentLanding({ onStartGuide, onManual, onClose }) {
  return (
    <div className="aa-landing">
      {onClose && (
        <button
          type="button"
          className="close-btn aa-landing-close"
          onClick={onClose}
          aria-label="Close"
        >
          ×
        </button>
      )}
      <div className="aa-landing-mark">
        <TrovisMark size={26} />
      </div>
      <h1 className="aa-landing-title">Connect an agent</h1>
      <p className="aa-landing-sub">
        Trovis walks you through it — answer a couple of questions and get
        copy-paste setup for your exact stack.
      </p>
      <button type="button" className="btn btn-primary aa-landing-cta" onClick={onStartGuide}>
        <SparkleIcon size={15} /> Set up with AI
      </button>
      <button type="button" className="aa-landing-manual" onClick={onManual}>
        Add manually instead
      </button>
      <div className="aa-landing-logos">
        <span className="aa-landing-logos-label">Works with</span>
        {PLATFORMS.map((p) => {
          const logo = PLATFORM_LOGOS[p.id]
          const Logo = logo?.Icon
          return Logo ? (
            <span key={p.id} className="aa-landing-logo" title={p.label}>
              <Logo size={16} />
            </span>
          ) : null
        })}
        <span className="aa-landing-logos-label">+ anything OTEL</span>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Manual wizard — the classic platform-picker flow (previously the default
// export). Internals unchanged; the shell above owns the .add-agent wrapper.
// ---------------------------------------------------------------------------

function ManualWizard({ onClose, embedded = false, onBackToLanding = null }) {
  const [platform, setPlatform] = useState(null)   // platform id, e.g. 'custom-python'
  const [provider, setProvider] = useState(null)   // provider id, only when platform.needsProvider
  const [claudeVariant, setClaudeVariant] = useState(null) // 'claude-agent-sdk' | 'claude-agents'
  // The ingest endpoint (VITE_API_URL → prod). Agents name themselves on
  // connect and are renamable on the dashboard, so there's no name input.
  const [endpoint] = useState(computeOverseeEndpoint())

  const selectedPlatform = PLATFORMS.find((p) => p.id === platform)
  const needsProvider = selectedPlatform?.needsProvider ?? false
  const needsClaudeVariant = platform === 'claude'
  // Either kind of sub-selection sits at step 2 before the instructions.
  const needsSubStep = needsProvider || needsClaudeVariant
  const subChosen = needsProvider ? !!provider : needsClaudeVariant ? !!claudeVariant : true
  const totalSteps = selectedPlatform && !needsSubStep ? 2 : 3

  // Derived step:
  //  - no platform yet                    → 1
  //  - platform needs a sub-step, none picked → 2
  //  - everything else                    → final (2 or 3)
  let step
  if (!platform) step = 1
  else if (needsSubStep && !subChosen) step = 2
  else step = totalSteps

  function handleBack() {
    // "Most recent selection wins" — going back unsets the latest pick.
    if (claudeVariant) setClaudeVariant(null)
    else if (provider) setProvider(null)
    else if (platform) setPlatform(null)
  }

  function handlePlatformPick(p) {
    setPlatform(p.id)
  }

  function handleProviderPick(p) {
    setProvider(p.id)
  }

  // Step-1 Back returns to the Add Agent landing (the shell owns that view).
  const onBack = step > 1 ? handleBack : onBackToLanding
  const showInstructions = step === totalSteps && step > 1 && !!platform

  // Claude resolves to one of the two real instruction platforms once a
  // variant is chosen; every other platform passes through unchanged.
  const effectivePlatform = needsClaudeVariant ? claudeVariant : platform

  return (
    <div>
      <WizardHeader
        step={step}
        total={totalSteps}
        onBack={onBack}
        onClose={embedded ? null : onClose}
      />

      {step === 1 && <PlatformStep onSelect={handlePlatformPick} />}
      {step === 2 && needsProvider && <ProviderStep onSelect={handleProviderPick} />}
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
