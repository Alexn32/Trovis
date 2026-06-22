import { useState, useEffect, useRef } from "react";

/* ─────────────────────────────────────────────
   TROVIS — Landing Page (logged-out front door)
   Hero signature: a self-playing Work Feed window —
   records tick in, one drift event gets caught live.
   Ladder: hero sells dev insights; system-of-record
   vision lives in the dark band mid-page.

   In-app variant: CTAs don't navigate to external URLs —
   they call onGetStarted / onSignIn so the existing Login
   flow takes over in place. Fully self-contained styling
   (inline + own fonts), so it's isolated from app theme.
   ───────────────────────────────────────────── */

const C = {
  linen: "#F5F1EB", cream: "#FBF8F3", subtle: "#ECE8E1", border: "#DDD7CE",
  ink: "#2C2418", body: "#4A4137", muted: "#8C8378", faint: "#B8B0A4",
  teal: "#5A7B7B", tealDark: "#7A9E9E", dark: "#1A1714",
  ok: "#2A9D6E", warn: "#D4792A", err: "#C43528",
};
const F = {
  disp: "'Space Grotesk', sans-serif",
  body: "'DM Sans', sans-serif",
  mono: "'JetBrains Mono', monospace",
};

const GRAIN =
  "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='240' height='240'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.35'/%3E%3C/svg%3E\")";

function TMark({ size = 26, color = C.teal }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <line x1="3" y1="5" x2="21" y2="5" stroke={color} strokeWidth="2.4" strokeLinecap="round" />
      <line x1="12" y1="5" x2="12" y2="21" stroke={color} strokeWidth="2.4" strokeLinecap="round" />
      <line x1="6.5" y1="12" x2="9.5" y2="12" stroke={color} strokeWidth="2.4" strokeLinecap="round" />
      <line x1="14.5" y1="12" x2="17.5" y2="12" stroke={color} strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  );
}

/* ── hero feed data: a loop with one drift catch ── */
const HERO_FEED = [
  { agent: "support-agent", text: "Resolved 14 tickets, escalated 1 to Maria", cost: "$0.84", status: "ok" },
  { agent: "billing-agent", text: "Reconciled 232 invoices against Stripe", cost: "$1.12", status: "ok" },
  { agent: "intake-agent", text: "Handed 3 leads to the sales team", cost: "$0.22", status: "ok" },
  { agent: "research-agent", text: "Drifted from scope — queried CRM outside its task", cost: "$0.31", status: "drift" },
  { agent: "qa-agent", text: "Flagged 2 responses for human approval", cost: "$0.09", status: "ok" },
  { agent: "ops-agent", text: "Completed nightly report, 0 anomalies", cost: "$0.47", status: "ok" },
];

// Renders a <button> when an onClick is given (in-app CTA), else an <a href>
// (anchors / external links). Styling is identical either way.
function Btn({ children, href, onClick, kind = "primary", style }) {
  const base = {
    display: "inline-block", padding: "13px 24px", borderRadius: 10,
    fontFamily: F.disp, fontWeight: 600, fontSize: 15, letterSpacing: "-0.01em",
    textDecoration: "none", cursor: "pointer", border: "none",
  };
  const kinds = {
    primary: { background: C.teal, color: C.cream },
    ghost: { background: "transparent", color: C.body, border: `1.5px solid ${C.border}` },
    dark: { background: C.cream, color: C.ink },
  };
  const merged = { ...base, ...kinds[kind], ...style };
  if (onClick) return <button type="button" onClick={onClick} style={merged}>{children}</button>;
  return <a href={href} style={{ ...merged, display: "inline-block" }}>{children}</a>;
}

// A text link rendered as a button so it can drive in-app navigation.
function LinkButton({ children, onClick, style }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        background: "none", border: "none", padding: 0, cursor: "pointer",
        fontFamily: F.body, fontSize: 14.5, color: C.body, ...style,
      }}
    >
      {children}
    </button>
  );
}

function SectionLabel({ children, light }) {
  return (
    <div style={{
      fontFamily: F.mono, fontSize: 12, fontWeight: 500, letterSpacing: "0.08em",
      textTransform: "uppercase", color: light ? C.tealDark : C.muted, marginBottom: 16,
    }}>{children}</div>
  );
}

/* ── Nav ── */
function Nav({ onGetStarted, onSignIn }) {
  return (
    <nav style={{
      display: "flex", alignItems: "center", justifyContent: "space-between",
      padding: "24px 0", flexWrap: "wrap", gap: 14,
    }}>
      <a href="#top" style={{ display: "flex", alignItems: "center", gap: 10, textDecoration: "none" }}>
        <TMark size={24} />
        <span style={{ fontFamily: F.disp, fontWeight: 700, fontSize: 21, letterSpacing: "-0.02em", color: C.teal }}>trovis</span>
      </a>
      <div style={{ display: "flex", alignItems: "center", gap: 26, flexWrap: "wrap" }}>
        {[["How it works", "#how"], ["Runtimes", "#runtimes"], ["Pricing", "#pricing"]].map(([t, h]) => (
          <a key={t} href={h} style={{ fontFamily: F.body, fontSize: 14.5, color: C.body, textDecoration: "none" }}>{t}</a>
        ))}
        <LinkButton onClick={onSignIn}>Sign in</LinkButton>
        <Btn onClick={onGetStarted} style={{ padding: "10px 18px", fontSize: 14 }}>Create your account</Btn>
      </div>
    </nav>
  );
}

/* ── Hero with live feed window ── */
function HeroFeedWindow() {
  const [count, setCount] = useState(2);
  const reduced = useRef(false);
  useEffect(() => {
    reduced.current = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced.current) { setCount(HERO_FEED.length); return; }
    const id = setInterval(() => setCount((c) => (c >= HERO_FEED.length ? 2 : c + 1)), 2400);
    return () => clearInterval(id);
  }, []);
  const rows = HERO_FEED.slice(0, count);
  return (
    <div style={{
      background: C.cream, border: `1px solid ${C.border}`, borderRadius: 16,
      boxShadow: "0 24px 60px -30px rgba(44,36,24,0.25)", overflow: "hidden", width: "100%",
    }}>
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "12px 16px", borderBottom: `1px solid ${C.subtle}`,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <TMark size={13} />
          <span style={{ fontFamily: F.mono, fontSize: 11, letterSpacing: "0.08em", textTransform: "uppercase", color: C.muted }}>Work feed</span>
        </div>
        <span style={{ fontFamily: F.mono, fontSize: 11, color: C.muted }}>6 agents · live</span>
      </div>
      <div style={{ minHeight: 318 }}>
        {rows.map((r, i) => (
          <div key={`${r.agent}-${i}`} style={{
            display: "flex", alignItems: "center", gap: 12, padding: "12px 16px",
            borderTop: i === 0 ? "none" : `1px solid ${C.subtle}`,
            background: r.status === "drift" ? "#D4792A0D" : "transparent",
            animation: reduced.current ? "none" : "tvIn 0.45s ease both",
          }}>
            <span style={{
              width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
              background: r.status === "drift" ? C.warn : C.ok,
            }} />
            <span style={{ fontFamily: F.mono, fontSize: 11.5, color: C.muted, width: 104, flexShrink: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.agent}</span>
            <span style={{ fontFamily: F.body, fontSize: 13.5, color: C.ink, flex: 1, minWidth: 0 }}>
              {r.text}
              {r.status === "drift" && (
                <span style={{
                  marginLeft: 8, fontFamily: F.mono, fontSize: 10, letterSpacing: "0.06em",
                  textTransform: "uppercase", color: C.warn, border: `1px solid ${C.warn}55`,
                  borderRadius: 5, padding: "2px 6px", whiteSpace: "nowrap",
                }}>drift detected</span>
              )}
            </span>
            <span style={{ fontFamily: F.mono, fontSize: 11.5, color: C.muted, flexShrink: 0 }}>{r.cost}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Hero({ onGetStarted }) {
  return (
    <header style={{
      display: "flex", gap: 48, alignItems: "center", flexWrap: "wrap",
      padding: "64px 0 88px",
    }}>
      <div style={{ flex: "1 1 420px", minWidth: 0 }}>
        <div style={{
          fontFamily: F.mono, fontSize: 12, fontWeight: 500, letterSpacing: "0.08em",
          textTransform: "uppercase", color: C.muted, marginBottom: 18,
        }}>Works with OpenClaw today</div>
        <h1 style={{
          fontFamily: F.disp, fontWeight: 700, fontSize: "clamp(36px, 5.2vw, 56px)",
          lineHeight: 1.06, letterSpacing: "-0.02em", color: C.ink, margin: "0 0 20px",
        }}>
          Your agents are working.<br />
          <span style={{ color: C.teal }}>See what they're actually doing.</span>
        </h1>
        <p style={{ fontFamily: F.body, fontSize: 17.5, lineHeight: 1.6, color: C.body, maxWidth: 480, margin: "0 0 32px" }}>
          Trovis records everything your AI agents do and tells you in plain English. Every action, every cost, every time one drifts from its job — without reading a single trace.
        </p>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center" }}>
          <Btn onClick={onGetStarted}>Create your account</Btn>
          <Btn href="#how" kind="ghost">See how it works</Btn>
        </div>
        <p style={{ fontFamily: F.body, fontSize: 13, color: C.muted, margin: "14px 2px 0" }}>
          Free for 5 agents. No card required. First insight in under 10 minutes.
        </p>
      </div>
      <div style={{ flex: "1 1 420px", minWidth: 0 }}>
        <HeroFeedWindow />
      </div>
    </header>
  );
}

/* ── How it works (a real sequence — numbering carries meaning) ── */
function HowItWorks() {
  const steps = [
    {
      n: "1", title: "Connect an agent",
      body: "Install the SDK, add two lines to your agent. Telemetry starts flowing immediately — identity, actions, tokens, costs.",
      code: "pip install trovis-agents",
    },
    {
      n: "2", title: "Trovis records everything",
      body: "Every action becomes a permanent, attributed record tied to the agent's declared identity. Nothing is sampled, nothing is mutated.",
      code: null,
    },
    {
      n: "3", title: "Read it in plain English",
      body: "The Work Feed tells you what each agent actually did. Ask questions in plain language. Get flagged the moment one drifts.",
      code: null,
    },
  ];
  return (
    <section id="how" style={{ padding: "72px 0" }}>
      <SectionLabel>How it works</SectionLabel>
      <h2 style={{ fontFamily: F.disp, fontWeight: 700, fontSize: "clamp(26px, 3.4vw, 36px)", letterSpacing: "-0.02em", color: C.ink, margin: "0 0 36px", maxWidth: 560 }}>
        From blind to briefed in three steps.
      </h2>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 18 }}>
        {steps.map((s) => (
          <div key={s.n} style={{ background: C.cream, border: `1px solid ${C.border}`, borderRadius: 14, padding: "24px 24px 26px" }}>
            <div style={{ fontFamily: F.mono, fontSize: 13, color: C.teal, marginBottom: 14 }}>{s.n}</div>
            <h3 style={{ fontFamily: F.disp, fontWeight: 700, fontSize: 18.5, color: C.ink, margin: "0 0 10px", letterSpacing: "-0.01em" }}>{s.title}</h3>
            <p style={{ fontFamily: F.body, fontSize: 14.5, lineHeight: 1.6, color: C.body, margin: 0 }}>{s.body}</p>
            {s.code && (
              <div style={{
                marginTop: 16, background: C.linen, border: `1px solid ${C.subtle}`, borderRadius: 8,
                padding: "10px 14px", fontFamily: F.mono, fontSize: 13, color: C.ink,
              }}>$ {s.code}</div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

/* ── Features ── */
function Features() {
  const feats = [
    {
      title: "Work Feed",
      body: "Plain-English summaries of what every agent actually did. \"Resolved 14 tickets, escalated 1 to Maria\" — not a wall of spans. Your manager can read it. So can you, at a glance.",
    },
    {
      title: "Drift detection",
      body: "Trovis reads each agent's declared identity and compares it against observed behavior. When an agent steps outside its job, you find out from Trovis — not from the damage.",
    },
    {
      title: "Cost tracking",
      body: "Token and dollar costs per agent, per task, per day. Attributed, not aggregated. Know which agent spent what before the invoice tells you.",
    },
    {
      title: "Fleet",
      body: "Every agent in one sortable view — status, activity, cost, owner. Scales from your first agent to your fiftieth without changing how you work.",
    },
    {
      title: "Ask",
      body: "\"What did my agents do today?\" \"Why is this one flagged?\" Ask in plain language, get answers built from the record — charts, comparisons, timelines.",
    },
    {
      title: "Workflow & handoffs",
      body: "See how work flows between your agents — Trovis detects when one agent hands off to another, and workflow maps place the humans in the loop. Follow the real path, not the diagram.",
    },
  ];
  return (
    <section style={{ padding: "24px 0 72px" }}>
      <SectionLabel>What you get</SectionLabel>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 18 }}>
        {feats.map((f) => (
          <div key={f.title} style={{ background: C.cream, border: `1px solid ${C.border}`, borderRadius: 14, padding: "22px 24px" }}>
            <h3 style={{ fontFamily: F.disp, fontWeight: 700, fontSize: 17, color: C.ink, margin: "0 0 8px", letterSpacing: "-0.01em" }}>{f.title}</h3>
            <p style={{ fontFamily: F.body, fontSize: 14.5, lineHeight: 1.6, color: C.body, margin: 0 }}>{f.body}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

/* ── Runtimes ── */
function Runtimes({ onGetStarted }) {
  const rts = [
    { name: "OpenClaw", status: "live" },
    { name: "OpenAI Agents SDK", status: "live" },
    { name: "Claude Agent SDK", status: "live" },
    { name: "Your runtime", status: "request" },
  ];
  return (
    <section id="runtimes" style={{ padding: "0 0 72px" }}>
      <SectionLabel>Runtimes</SectionLabel>
      <h2 style={{ fontFamily: F.disp, fontWeight: 700, fontSize: "clamp(24px, 3vw, 32px)", letterSpacing: "-0.02em", color: C.ink, margin: "0 0 10px" }}>
        One record across every runtime.
      </h2>
      <p style={{ fontFamily: F.body, fontSize: 15.5, lineHeight: 1.6, color: C.body, maxWidth: 560, margin: "0 0 28px" }}>
        Trovis is the neutral layer — it isn't tied to any one agent framework. OpenClaw is fully supported today; the next runtimes ship in order of demand.
      </p>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 14 }}>
        {rts.map((r) => (
          <div key={r.name} style={{
            display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10,
            background: C.cream, border: `1px solid ${C.border}`, borderRadius: 12, padding: "16px 18px",
            opacity: r.status === "live" ? 1 : 0.85,
          }}>
            <span style={{ fontFamily: F.disp, fontWeight: 500, fontSize: 15, color: C.ink }}>{r.name}</span>
            {r.status === "live" && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontFamily: F.mono, fontSize: 10.5, letterSpacing: "0.06em", textTransform: "uppercase", color: C.ok }}>
                <span style={{ width: 7, height: 7, borderRadius: "50%", background: C.ok }} />Live
              </span>
            )}
            {r.status === "soon" && (
              <LinkButton onClick={onGetStarted} style={{ fontSize: 12.5, color: C.teal }}>Coming soon — get notified</LinkButton>
            )}
            {r.status === "request" && (
              <LinkButton onClick={onGetStarted} style={{ fontSize: 12.5, color: C.teal }}>Request it</LinkButton>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

/* ── Vision band (the one tonal shift on the page) ── */
function VisionBand({ onGetStarted }) {
  return (
    <section style={{
      background: C.dark, borderRadius: 20, padding: "clamp(36px, 6vw, 64px)",
      margin: "0 0 80px", position: "relative", overflow: "hidden",
    }}>
      <div style={{ position: "absolute", inset: 0, backgroundImage: GRAIN, opacity: 0.05, pointerEvents: "none" }} />
      <div style={{ position: "relative", maxWidth: 640 }}>
        <SectionLabel light>Where this goes</SectionLabel>
        <h2 style={{
          fontFamily: F.disp, fontWeight: 700, fontSize: "clamp(26px, 3.6vw, 40px)",
          letterSpacing: "-0.02em", lineHeight: 1.12, color: "#F5F1EB", margin: "0 0 18px",
        }}>
          For 100 years, the org chart only showed humans.
        </h2>
        <p style={{ fontFamily: F.body, fontSize: 16, lineHeight: 1.65, color: "#B8B0A4", margin: "0 0 26px" }}>
          Every record Trovis keeps today — every action, decision, handoff, and cost — builds toward the same thing: the system of record for the hybrid workforce. The insights you get on day one and the audit log your organization needs in year three are the same data. You're not adopting a dashboard. You're starting the record.
        </p>
        <Btn onClick={onGetStarted} kind="dark">Start the record</Btn>
      </div>
    </section>
  );
}

/* ── Pricing ── */
function Pricing({ onGetStarted }) {
  const [cycle, setCycle] = useState("monthly");
  const tiers = [
    { name: "Free", monthly: 0, agents: "Up to 5 agents", cta: "Create your account", highlight: false },
    { name: "Starter", monthly: 49, agents: "Up to 15 agents", cta: "Start with Starter", highlight: false },
    { name: "Pro", monthly: 199, agents: "Up to 50 agents", cta: "Start with Pro", highlight: true },
    { name: "Enterprise", monthly: null, agents: "Unlimited agents", cta: "Talk to us", highlight: false },
  ];
  // Numbers are ceilings; annual = 20% off the monthly rate, shown per-month.
  const priceFor = (t) => {
    if (t.monthly === null) return { big: "Custom", small: "" };
    if (t.monthly === 0) return { big: "$0", small: "forever" };
    const m = cycle === "annual" ? Math.round(t.monthly * 0.8) : t.monthly;
    return { big: `$${m}`, small: cycle === "annual" ? "/mo · billed annually" : "/month" };
  };
  const ctaStyle = (t) => ({
    marginTop: "auto", textAlign: "center", padding: "11px 0", borderRadius: 10,
    fontFamily: F.disp, fontWeight: 600, fontSize: 14, textDecoration: "none", cursor: "pointer",
    background: t.highlight ? C.teal : "transparent",
    color: t.highlight ? C.cream : C.body,
    border: t.highlight ? "none" : `1.5px solid ${C.border}`,
  });
  const tab = (c, label) => (
    <button
      type="button"
      onClick={() => setCycle(c)}
      style={{
        padding: "8px 18px", border: "none", cursor: "pointer", fontFamily: F.body,
        fontSize: 13.5, fontWeight: 500,
        background: cycle === c ? C.teal : "transparent",
        color: cycle === c ? C.cream : C.body,
      }}
    >{label}</button>
  );
  return (
    <section id="pricing" style={{ padding: "0 0 80px" }}>
      <SectionLabel>Pricing</SectionLabel>
      <h2 style={{ fontFamily: F.disp, fontWeight: 700, fontSize: "clamp(24px, 3vw, 32px)", letterSpacing: "-0.02em", color: C.ink, margin: "0 0 8px" }}>
        Every feature at every tier. Pay for agents, nothing else.
      </h2>
      <p style={{ fontFamily: F.body, fontSize: 14.5, color: C.muted, margin: "0 0 20px" }}>Save 20% with annual billing.</p>
      <div style={{
        display: "inline-flex", border: `1px solid ${C.border}`, borderRadius: 10,
        overflow: "hidden", marginBottom: 26,
      }}>
        {tab("monthly", "Monthly")}
        {tab("annual", "Annual − 20%")}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 16 }}>
        {tiers.map((t) => {
          const p = priceFor(t);
          return (
            <div key={t.name} style={{
              background: C.cream, borderRadius: 14, padding: "26px 24px",
              border: t.highlight ? `2px solid ${C.teal}` : `1px solid ${C.border}`,
              display: "flex", flexDirection: "column", gap: 0,
            }}>
              <div style={{ fontFamily: F.disp, fontWeight: 700, fontSize: 16, color: C.ink, marginBottom: 14 }}>{t.name}</div>
              <div style={{ marginBottom: 6 }}>
                <span style={{ fontFamily: F.disp, fontWeight: 700, fontSize: 32, color: C.ink, letterSpacing: "-0.02em" }}>{p.big}</span>
                <span style={{ fontFamily: F.body, fontSize: 14, color: C.muted }}> {p.small}</span>
              </div>
              <div style={{ fontFamily: F.body, fontSize: 14.5, color: C.body, marginBottom: 22 }}>{t.agents} · all features</div>
              {t.name === "Enterprise" ? (
                <a href="mailto:hello@trovisai.com" style={ctaStyle(t)}>{t.cta}</a>
              ) : (
                <button type="button" onClick={onGetStarted} style={ctaStyle(t)}>{t.cta}</button>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

/* ── Final CTA + Footer ── */
function FinalCta({ onGetStarted }) {
  return (
    <section style={{ textAlign: "center", padding: "0 0 88px" }}>
      <h2 style={{ fontFamily: F.disp, fontWeight: 700, fontSize: "clamp(26px, 3.6vw, 38px)", letterSpacing: "-0.02em", color: C.ink, margin: "0 0 14px" }}>
        Find what matters.
      </h2>
      <p style={{ fontFamily: F.body, fontSize: 16, color: C.body, margin: "0 0 26px" }}>
        Connect your first agent free. Your first plain-English insight is minutes away.
      </p>
      <Btn onClick={onGetStarted}>Create your account</Btn>
    </section>
  );
}

function Footer() {
  return (
    <footer style={{
      display: "flex", alignItems: "center", justifyContent: "space-between",
      gap: 16, flexWrap: "wrap", padding: "26px 0 44px", borderTop: `1px solid ${C.border}`,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <TMark size={15} color={C.muted} />
        <span style={{ fontFamily: F.body, fontSize: 13, color: C.muted }}>The system of record for the hybrid workforce.</span>
      </div>
      <div style={{ display: "flex", gap: 20, alignItems: "center", flexWrap: "wrap" }}>
        <a href="#pricing" style={{ fontFamily: F.body, fontSize: 13, color: C.muted, textDecoration: "none" }}>Pricing</a>
        <a href="https://trovisai.com/terms" style={{ fontFamily: F.body, fontSize: 13, color: C.muted, textDecoration: "none" }}>Terms</a>
        <a href="https://trovisai.com/privacy" style={{ fontFamily: F.body, fontSize: 13, color: C.muted, textDecoration: "none" }}>Privacy</a>
        <a href="mailto:hello@trovisai.com" style={{ fontFamily: F.body, fontSize: 13, color: C.muted, textDecoration: "none" }}>Contact</a>
        <a href="https://x.com/trovisai" style={{ fontFamily: F.body, fontSize: 13, color: C.muted, textDecoration: "none" }}>X</a>
        <span style={{ fontFamily: F.body, fontSize: 13, color: C.faint }}>© 2026 Trovis · Omaha, NE</span>
      </div>
    </footer>
  );
}

// onGetStarted / onSignIn drive the in-app Login flow (no external navigation).
export default function TrovisLanding({ onGetStarted = () => {}, onSignIn = () => {} }) {
  return (
    <div id="top" style={{ minHeight: "100vh", background: C.linen, color: C.ink, fontFamily: F.body, position: "relative", overflowX: "hidden" }}>
      <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=DM+Sans:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
      <div style={{ position: "fixed", inset: 0, backgroundImage: GRAIN, opacity: 0.04, pointerEvents: "none", zIndex: 1 }} />
      <div style={{ position: "relative", zIndex: 2, maxWidth: 1080, margin: "0 auto", padding: "0 24px" }}>
        <Nav onGetStarted={onGetStarted} onSignIn={onSignIn} />
        <Hero onGetStarted={onGetStarted} />
        <HowItWorks />
        <Features />
        <Runtimes onGetStarted={onGetStarted} />
        <VisionBand onGetStarted={onGetStarted} />
        <Pricing onGetStarted={onGetStarted} />
        <FinalCta onGetStarted={onGetStarted} />
        <Footer />
      </div>
      <style>{`
        @keyframes tvIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
        @media (prefers-reduced-motion: reduce) { * { animation: none !important; } }
        a:focus-visible, button:focus-visible { outline: 2px solid ${C.teal}; outline-offset: 2px; }
        html { scroll-behavior: smooth; }
      `}</style>
    </div>
  );
}
