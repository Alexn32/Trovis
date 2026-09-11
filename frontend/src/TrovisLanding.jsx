import { useEffect, useState } from "react";
import { api } from "./api.js";

/* ─────────────────────────────────────────────
   TROVIS — Public founding waitlist
   Logged-out front door (SPA) and /waitlist.
   Copy is the Work desk / hybrid handoffs pitch.
   Styling is self-contained (inline + own fonts)
   so it stays isolated from the app theme.
   ───────────────────────────────────────────── */

const C = {
  linen: "#F5F1EB", cream: "#FBF8F3", border: "#DDD7CE",
  ink: "#2C2418", body: "#4A4137", muted: "#8C8378", faint: "#B8B0A4",
  teal: "#5A7B7B", ok: "#2A9D6E", err: "#C43528",
};
const F = {
  disp: "'Space Grotesk', sans-serif",
  body: "'DM Sans', sans-serif",
};

const GRAIN =
  "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='240' height='240'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.35'/%3E%3C/svg%3E\")";

const TITLE = "Trovis — Founding waitlist for the Work desk";
const DESCRIPTION =
  "Founding access to Trovis — the operating layer for hybrid work. See handoffs and waiting work across people and agents.";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const BENEFITS = [
  {
    title: "Handoffs you can actually see",
    body: "When work moves between a person, an internal agent, and a third-party agent, you can tell who holds it — not reconstruct it from a trace dump.",
  },
  {
    title: "Who’s waiting, what’s stuck",
    body: "Open work sitting on someone — or something — shows up on one desk. Built for teams already running agents who can’t see across the loop.",
  },
  {
    title: "For teams already in the loop",
    body: "You already run OpenClaw, OpenAI Agents, Claude, Cursor, or ChatGPT Actions. Trovis is for the work that creates, not another dashboard to babysit.",
  },
];

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

function WaitlistForm({ onJoined }) {
  const [email, setEmail] = useState("");
  const [company, setCompany] = useState("");
  const [role, setRole] = useState("");
  const [tools, setTools] = useState("");
  const [website, setWebsite] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function onSubmit(e) {
    e.preventDefault();
    setError("");
    const cleanEmail = email.trim();
    if (!EMAIL_RE.test(cleanEmail)) {
      setError("Please enter a valid email address.");
      return;
    }
    setBusy(true);
    try {
      await api.joinWaitlist({
        email: cleanEmail,
        company: company.trim() || null,
        role: role.trim() || null,
        tools: tools.trim() || null,
        website: website.trim() || null,
        source: "founding-waitlist",
      });
      onJoined();
    } catch (err) {
      setError(err?.message || "Could not join just now. Try again.");
    } finally {
      setBusy(false);
    }
  }

  const label = {
    display: "block",
    fontSize: 11,
    fontWeight: 600,
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    color: C.body,
    margin: "0 0 6px",
  };
  const input = {
    width: "100%",
    padding: "11px 12px",
    fontFamily: F.body,
    fontSize: 15,
    color: C.ink,
    background: C.linen,
    border: `1px solid ${C.border}`,
    borderRadius: 10,
    outline: "none",
    boxSizing: "border-box",
  };
  const optional = {
    fontWeight: 500,
    letterSpacing: 0,
    textTransform: "none",
    color: C.muted,
  };

  return (
    <form onSubmit={onSubmit} noValidate>
      <div
        aria-hidden="true"
        style={{ position: "absolute", left: -10000, width: 1, height: 1, overflow: "hidden" }}
      >
        <label htmlFor="wl-website">Website</label>
        <input
          id="wl-website"
          name="website"
          type="text"
          tabIndex={-1}
          autoComplete="off"
          value={website}
          onChange={(e) => setWebsite(e.target.value)}
        />
      </div>
      <div style={{ marginBottom: 14 }}>
        <label htmlFor="wl-email" style={label}>Work email</label>
        <input
          id="wl-email"
          name="email"
          type="email"
          required
          autoComplete="email"
          placeholder="you@company.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          style={input}
        />
      </div>
      <div style={{ marginBottom: 14 }}>
        <label htmlFor="wl-company" style={label}>
          Company <span style={optional}>(optional)</span>
        </label>
        <input
          id="wl-company"
          name="company"
          type="text"
          autoComplete="organization"
          value={company}
          onChange={(e) => setCompany(e.target.value)}
          style={input}
        />
      </div>
      <div style={{ marginBottom: 14 }}>
        <label htmlFor="wl-role" style={label}>
          Role <span style={optional}>(optional)</span>
        </label>
        <input
          id="wl-role"
          name="role"
          type="text"
          autoComplete="organization-title"
          placeholder="Founder, eng lead…"
          value={role}
          onChange={(e) => setRole(e.target.value)}
          style={input}
        />
      </div>
      <div style={{ marginBottom: 14 }}>
        <label htmlFor="wl-tools" style={label}>
          What agents / tools are already in your loop? <span style={optional}>(optional)</span>
        </label>
        <textarea
          id="wl-tools"
          name="tools"
          placeholder="OpenClaw, Cursor, Claude, ChatGPT Actions…"
          value={tools}
          onChange={(e) => setTools(e.target.value)}
          style={{ ...input, minHeight: 88, resize: "vertical" }}
        />
      </div>
      {error && (
        <p role="alert" style={{ color: C.err, fontSize: 13.5, margin: "0 0 12px" }}>{error}</p>
      )}
      <button
        type="submit"
        disabled={busy}
        style={{
          width: "100%",
          marginTop: 6,
          padding: "13px 18px",
          border: "none",
          borderRadius: 10,
          background: C.teal,
          color: C.cream,
          fontFamily: F.disp,
          fontWeight: 600,
          fontSize: 15,
          letterSpacing: "-0.01em",
          cursor: busy ? "wait" : "pointer",
          opacity: busy ? 0.55 : 1,
        }}
      >
        {busy ? "Joining…" : "Join the founding list"}
      </button>
      <p style={{ fontSize: 13, color: C.muted, margin: "12px 0 0", lineHeight: 1.5 }}>
        We’ll only write about founding access. No product tour, no drip.
      </p>
    </form>
  );
}

export default function TrovisLanding({ onSignIn = () => {} }) {
  const [joined, setJoined] = useState(false);

  useEffect(() => {
    const prevTitle = document.title;
    document.title = TITLE;
    const meta = document.querySelector('meta[name="description"]');
    const prevDesc = meta?.getAttribute("content");
    if (meta) meta.setAttribute("content", DESCRIPTION);
    return () => {
      document.title = prevTitle;
      if (meta && prevDesc != null) meta.setAttribute("content", prevDesc);
    };
  }, []);

  return (
    <div id="top" style={{ minHeight: "100vh", background: C.linen, color: C.ink, fontFamily: F.body, position: "relative", overflowX: "hidden" }}>
      <div style={{ position: "fixed", inset: 0, backgroundImage: GRAIN, opacity: 0.04, pointerEvents: "none", zIndex: 1 }} />
      <div style={{ position: "relative", zIndex: 2, maxWidth: 1080, margin: "0 auto", padding: "0 24px" }}>
        <nav style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "24px 0", gap: 14 }}>
          <a href="#top" style={{ display: "flex", alignItems: "center", gap: 10, textDecoration: "none" }}>
            <TMark size={24} />
            <span style={{ fontFamily: F.disp, fontWeight: 700, fontSize: 21, letterSpacing: "-0.02em", color: C.teal }}>trovis</span>
          </a>
          <button
            type="button"
            onClick={onSignIn}
            style={{
              background: "none", border: "none", padding: 0, cursor: "pointer",
              fontFamily: F.body, fontSize: 14.5, color: C.body,
            }}
          >
            Sign in
          </button>
        </nav>

        <header style={{
          display: "flex", gap: 48, alignItems: "flex-start", flexWrap: "wrap",
          padding: "36px 0 72px",
        }}>
          <div style={{ flex: "1 1 420px", minWidth: 0 }}>
            <div style={{
              fontSize: 12, fontWeight: 600, letterSpacing: "0.08em",
              textTransform: "uppercase", color: C.muted, marginBottom: 18,
            }}>
              Founding waitlist · Work desk
            </div>
            <h1 style={{
              fontFamily: F.disp, fontWeight: 700, fontSize: "clamp(34px, 5vw, 52px)",
              lineHeight: 1.06, letterSpacing: "-0.02em", color: C.ink, margin: "0 0 20px",
            }}>
              See the handoff.<br />
              <span style={{ color: C.teal }}>See who’s waiting.</span>
            </h1>
            <p style={{ fontFamily: F.body, fontSize: 17.5, lineHeight: 1.6, color: C.body, maxWidth: 520, margin: "0 0 28px" }}>
              Trovis is the operating layer for hybrid work — humans, internal agents,
              third-party agents, and the SaaS around them. Work is the noun. Judgment
              is the product. Founding access is the Work desk: visibility into hybrid
              handoffs and waiting work — not a place to watch agents record themselves.
            </p>
            <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 16 }}>
              {BENEFITS.map((b) => (
                <li key={b.title} style={{ background: C.cream, border: `1px solid ${C.border}`, borderRadius: 14, padding: "16px 18px" }}>
                  <strong style={{ display: "block", fontFamily: F.disp, fontSize: 16, letterSpacing: "-0.01em", marginBottom: 4 }}>{b.title}</strong>
                  <p style={{ margin: 0, fontSize: 14.5, lineHeight: 1.55, color: C.body }}>{b.body}</p>
                </li>
              ))}
            </ul>
          </div>

          <div style={{ flex: "1 1 340px", minWidth: 0 }}>
            <div style={{
              background: C.cream, border: `1px solid ${C.border}`, borderRadius: 16,
              padding: "28px 26px 26px", boxShadow: "0 24px 60px -30px rgba(44,36,24,0.22)",
              position: "relative",
            }}>
              {joined ? (
                <div role="status">
                  <div style={{
                    display: "inline-flex", alignItems: "center", justifyContent: "center",
                    width: 36, height: 36, borderRadius: "50%",
                    background: "rgba(42,157,110,0.12)", color: C.ok,
                    marginBottom: 14, fontSize: 18,
                  }} aria-hidden="true">✓</div>
                  <h2 style={{ fontFamily: F.disp, fontSize: 20, letterSpacing: "-0.02em", margin: "0 0 10px" }}>
                    You’re on the founding list.
                  </h2>
                  <p style={{ fontSize: 15.5, lineHeight: 1.6, color: C.body, margin: 0 }}>
                    We’ll write when the Work desk is ready for you. Same address —
                    nothing else in the meantime.
                  </p>
                </div>
              ) : (
                <>
                  <h2 style={{ fontFamily: F.disp, fontSize: 20, letterSpacing: "-0.02em", margin: "0 0 6px" }}>
                    Request founding access
                  </h2>
                  <p style={{ fontSize: 14, color: C.muted, margin: "0 0 20px", lineHeight: 1.5 }}>
                    A short note so we know how work already runs on your team.
                    The Home desk is live today for teams already in Trovis.
                  </p>
                  <WaitlistForm onJoined={() => setJoined(true)} />
                </>
              )}
            </div>
          </div>
        </header>

        <footer style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          gap: 16, flexWrap: "wrap", padding: "26px 0 44px", borderTop: `1px solid ${C.border}`,
        }}>
          <p style={{ fontSize: 13, color: C.muted, margin: 0 }}>
            The operating layer for hybrid work. Judgment, not chrome.
          </p>
          <div style={{ display: "flex", gap: 20, alignItems: "center", flexWrap: "wrap" }}>
            <a href="/terms" style={{ fontFamily: F.body, fontSize: 13, color: C.muted, textDecoration: "none" }}>Terms</a>
            <a href="/privacy" style={{ fontFamily: F.body, fontSize: 13, color: C.muted, textDecoration: "none" }}>Privacy</a>
            <a href="mailto:hello@trovisai.com" style={{ fontFamily: F.body, fontSize: 13, color: C.muted, textDecoration: "none" }}>Contact</a>
            <a href="https://x.com/tryTrovis" style={{ fontFamily: F.body, fontSize: 13, color: C.muted, textDecoration: "none" }}>X</a>
            <span style={{ fontFamily: F.body, fontSize: 13, color: C.faint }}>© 2026 Trovis</span>
          </div>
        </footer>
      </div>
      <style>{`
        a:focus-visible, button:focus-visible, input:focus-visible, textarea:focus-visible {
          outline: 2px solid ${C.teal}; outline-offset: 2px;
        }
      `}</style>
    </div>
  );
}
