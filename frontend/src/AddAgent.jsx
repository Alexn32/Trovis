import { useState } from 'react'

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

const PLATFORMS = [
  { id: 'custom-python',  label: 'Custom Python Agent',       subtitle: 'Any Python script calling an LLM API',           needsProvider: true  },
  { id: 'openclaw',       label: 'OpenClaw',                  subtitle: 'AI agent platform — agents connect themselves',  needsProvider: false },
  { id: 'crewai',         label: 'CrewAI',                    subtitle: 'Multi-agent orchestration framework',            needsProvider: false },
  { id: 'langchain',      label: 'LangChain / LangGraph',     subtitle: 'LLM application framework',                      needsProvider: false },
  { id: 'openai-agents',  label: 'OpenAI Agents SDK',         subtitle: 'OpenAI native agent framework',                  needsProvider: false },
  { id: 'claude-cowork',  label: 'Claude Cowork',             subtitle: 'Anthropic desktop agent — no code needed',       needsProvider: false },
  { id: 'claude-code',    label: 'Claude Code',               subtitle: 'Anthropic coding agent — no code needed',        needsProvider: false },
  { id: 'node',           label: 'Node.js / TypeScript Agent', subtitle: 'JavaScript or TypeScript agent',                needsProvider: true  },
  { id: 'other',          label: 'Other',                     subtitle: 'Any app that supports OpenTelemetry',            needsProvider: true  },
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

// Framework-level instrumentors (used for CrewAI / LangChain / OpenAI Agents).
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
  'openai-agents': {
    label: 'OpenAI Agents SDK',
    title: 'Connect OpenAI Agents SDK',
    pkg: 'openinference-instrumentation-openai-agents',
    importLines:
`from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
OpenAIAgentsInstrumentor().instrument()`,
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
        {PLATFORMS.map((p) => (
          <button
            key={p.id}
            type="button"
            className="platform-card"
            onClick={() => onSelect(p)}
          >
            <span className="platform-card-label">{p.label}</span>
            <span className="platform-card-subtitle">{p.subtitle}</span>
          </button>
        ))}
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
          Replace localhost with your Oversee server address in production.
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

function OpenClawInstructions({ agentName, endpoint }) {
  const configBlock = fill(
`{
  "plugins": {
    "entries": {
      "oversee": {
        "endpoint": "OVERSEE_ENDPOINT",
        "agentName": "AGENT_NAME",
        "enabled": true,
        "hooks": {
          "allowConversationAccess": true
        }
      }
    }
  }
}`,
    agentName, endpoint,
  )

  const chatMessage = fill(
`I need you to install the Oversee monitoring plugin so we can track agent telemetry. Here's what to do:

1. Install the Oversee plugin package:
npm install @oversee/openclaw-plugin

2. Open openclaw.json and add this to the plugins section (create the plugins block if it doesn't exist):
{
  "plugins": {
    "entries": {
      "oversee": {
        "endpoint": "OVERSEE_ENDPOINT",
        "agentName": "AGENT_NAME",
        "enabled": true,
        "hooks": {
          "allowConversationAccess": true
        }
      }
    }
  }
}

3. Restart the gateway so the plugin loads.

4. Confirm when complete.`,
    agentName, endpoint,
  )

  return (
    <>
      <h2 className="instructions-title">Connect OpenClaw agents</h2>
      <p className="instructions-subtitle">
        Install the Oversee plugin — every agent on this OpenClaw instance is monitored automatically.
      </p>
      <Callout variant="blue">
        <strong>OpenClaw + Oversee:</strong> One plugin install gives you automatic
        monitoring of every agent — messages, tool calls, LLM requests, and run completions.
      </Callout>
      <Tabs tabs={[
        {
          label: 'Terminal setup',
          content: (
            <>
              <p className="tab-subtitle">Run these commands in your terminal or Claude Code.</p>
              <NumberedStep n={1} title="Navigate to your OpenClaw project">
                <CodeBlock code="cd /path/to/your-openclaw-project" />
              </NumberedStep>
              <NumberedStep n={2} title="Install the Oversee plugin">
                <CodeBlock code="npm install @oversee/openclaw-plugin" />
              </NumberedStep>
              <NumberedStep
                n={3}
                title="Add the Oversee config to your openclaw.json (create the plugins block if it doesn't exist)"
              >
                <CodeBlock code={configBlock} />
              </NumberedStep>
              <NumberedStep n={4} title="Restart your OpenClaw gateway">
                <CodeBlock code="openclaw gateway restart" />
              </NumberedStep>
              <NumberedStep n={5} title="Send any message to your agent. It will appear in Oversee within seconds." />
              <Callout variant="info">
                <strong>allowConversationAccess: true</strong> enables full telemetry
                including LLM calls and run completions. Without it you still get message
                and tool call telemetry.
              </Callout>
              <Callout variant="info">
                Every agent running on this OpenClaw instance is automatically monitored.
                Individual agents are identified by their workspace name.
              </Callout>
              <Callout variant="info">
                <strong>Coming soon:</strong> once published to ClawHub, installation will
                be a single command: <code>openclaw plugins install clawhub:@oversee/openclaw-plugin</code>
              </Callout>
              <SuccessCallout />
            </>
          ),
        },
        {
          label: 'Chat setup',
          content: (
            <>
              <p className="tab-subtitle">Paste this message to your OpenClaw agent.</p>
              <p className="explanatory">
                Copy the message below and paste it into your OpenClaw agent's chat. The
                agent will install the Oversee plugin and update your configuration. Some
                agents may decline if their safety rules prevent self-modification — use
                the Terminal setup tab instead.
              </p>
              <AgentMessageBlock code={chatMessage} />
              <Callout variant="warning">
                If your agent declines to modify its own configuration, use the Terminal
                setup tab. Agents with strict safety boundaries may refuse infrastructure
                changes — this is expected and correct behavior.
              </Callout>
              <SuccessCallout />
            </>
          ),
        },
      ]} />
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

function InstructionsView({ platform, provider, agentName, endpoint }) {
  if (platform === 'custom-python') {
    return <CustomPythonInstructions provider={provider} agentName={agentName} endpoint={endpoint} />
  }
  if (platform === 'openclaw') {
    return <OpenClawInstructions agentName={agentName} endpoint={endpoint} />
  }
  if (platform === 'crewai' || platform === 'langchain' || platform === 'openai-agents') {
    return <FrameworkInstructions frameworkId={platform} agentName={agentName} endpoint={endpoint} />
  }
  if (platform === 'claude-cowork') {
    return <ClaudeCoworkInstructions endpoint={endpoint} />
  }
  if (platform === 'claude-code') {
    return <ClaudeCodeInstructions endpoint={endpoint} />
  }
  if (platform === 'node') {
    return <NodeInstructions agentName={agentName} endpoint={endpoint} />
  }
  return <OtherInstructions agentName={agentName} endpoint={endpoint} />
}

// ---------------------------------------------------------------------------
// Top-level wizard component
// ---------------------------------------------------------------------------

const DEFAULT_ENDPOINT = 'http://localhost:8080/v1/traces'

export default function AddAgent({ onClose }) {
  const [platform, setPlatform] = useState(null)   // platform id, e.g. 'custom-python'
  const [provider, setProvider] = useState(null)   // provider id, only when platform.needsProvider
  const [agentName, setAgentName] = useState('')
  const [endpoint, setEndpoint] = useState(DEFAULT_ENDPOINT)

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
          <Step3Header
            agentName={agentName}
            setAgentName={setAgentName}
            endpoint={endpoint}
          />
          <InstructionsView
            platform={platform}
            provider={provider}
            agentName={agentName}
            endpoint={endpoint}
          />
        </>
      )}
    </div>
  )
}
