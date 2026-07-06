/* ─────────────────────────────────────────────
   TROVIS — Terms of Service + Privacy Policy (in-app)
   Served at /terms and /privacy. The app has no router, so App.jsx detects the
   path and renders <TrovisLegal page="terms|privacy" /> as a public page (no
   auth). Self-contained inline styles — brand palette, isolated from theme.

   Starter documents tailored to Trovis. NOT a substitute for legal advice —
   have counsel review before relying on them. Update "Last updated", the
   entity, and jurisdiction as needed.
   ───────────────────────────────────────────── */

const C = {
  linen: "#F5F1EB", cream: "#FBF8F3", border: "#DDD7CE",
  ink: "#2C2418", body: "#4A4137", muted: "#8C8378", teal: "#5A7B7B",
};
const F = {
  disp: "'Space Grotesk', sans-serif",
  body: "'DM Sans', sans-serif",
};

const LAST_UPDATED = "June 22, 2026";
const CONTACT = "hello@trovisai.com";

function TMark({ size = 22, color = C.teal }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <line x1="3" y1="5" x2="21" y2="5" stroke={color} strokeWidth="2.4" strokeLinecap="round" />
      <line x1="12" y1="5" x2="12" y2="21" stroke={color} strokeWidth="2.4" strokeLinecap="round" />
      <line x1="6.5" y1="12" x2="9.5" y2="12" stroke={color} strokeWidth="2.4" strokeLinecap="round" />
      <line x1="14.5" y1="12" x2="17.5" y2="12" stroke={color} strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  );
}

function Layout({ title, children }) {
  return (
    <div style={{ minHeight: "100vh", background: C.linen, color: C.ink, fontFamily: F.body }}>
      <link
        href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=DM+Sans:wght@300;400;500;600&display=swap"
        rel="stylesheet"
      />
      <div style={{ maxWidth: 760, margin: "0 auto", padding: "0 24px" }}>
        <nav style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "24px 0" }}>
          <a href="/" style={{ display: "flex", alignItems: "center", gap: 10, textDecoration: "none" }}>
            <TMark size={22} />
            <span style={{ fontFamily: F.disp, fontWeight: 700, fontSize: 19, letterSpacing: "-0.02em", color: C.teal }}>trovis</span>
          </a>
          <div style={{ display: "flex", gap: 22 }}>
            <a href="/terms" style={{ fontSize: 14, color: C.body, textDecoration: "none" }}>Terms</a>
            <a href="/privacy" style={{ fontSize: 14, color: C.body, textDecoration: "none" }}>Privacy</a>
          </div>
        </nav>

        <main style={{ padding: "24px 0 80px" }}>
          <h1 style={{ fontFamily: F.disp, fontWeight: 700, fontSize: "clamp(30px, 5vw, 42px)", letterSpacing: "-0.02em", margin: "0 0 8px" }}>
            {title}
          </h1>
          <p style={{ fontSize: 13.5, color: C.muted, margin: "0 0 36px" }}>Last updated: {LAST_UPDATED}</p>
          <div style={{ fontSize: 15.5, lineHeight: 1.7, color: C.body }}>{children}</div>
        </main>

        <footer style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 12, padding: "24px 0 48px", borderTop: `1px solid ${C.border}` }}>
          <span style={{ fontSize: 13, color: C.muted }}>© 2026 Trovis · Omaha, NE</span>
          <span style={{ fontSize: 13 }}>
            <a href="/terms" style={{ color: C.muted, textDecoration: "none" }}>Terms</a>
            <span style={{ color: C.border, margin: "0 8px" }}>·</span>
            <a href="/privacy" style={{ color: C.muted, textDecoration: "none" }}>Privacy</a>
            <span style={{ color: C.border, margin: "0 8px" }}>·</span>
            <a href={`mailto:${CONTACT}`} style={{ color: C.muted, textDecoration: "none" }}>Contact</a>
          </span>
        </footer>
      </div>
    </div>
  );
}

function H({ children }) {
  return (
    <h2 style={{ fontFamily: F.disp, fontWeight: 700, fontSize: 20, color: C.ink, letterSpacing: "-0.01em", margin: "32px 0 10px" }}>
      {children}
    </h2>
  );
}

function TermsBody() {
  return (
    <Layout title="Terms of Service">
      <p>
        These Terms of Service ("Terms") govern your access to and use of Trovis (the
        "Service"), operated by Trovis ("Trovis," "we," "us"). By creating an account or
        using the Service, you agree to these Terms. If you are using the Service on behalf
        of an organization, you represent that you have authority to bind that organization.
      </p>

      <H>1. The Service</H>
      <p>
        Trovis ingests OpenTelemetry data emitted by your AI agents, stores it, and presents
        analysis — including plain-English summaries, cost tracking, and behavioral
        ("drift") insights generated with the help of third-party AI models. The Service is
        provided on an ongoing, "as available" basis and may change over time.
      </p>

      <H>2. Accounts</H>
      <p>
        You are responsible for safeguarding your account credentials and API keys, and for
        all activity under your account. Notify us promptly of any unauthorized use. You must
        be at least 18 and provide accurate registration information.
      </p>

      <H>3. Acceptable use</H>
      <p>
        You agree not to misuse the Service: no unlawful activity, no attempts to disrupt or
        overload our infrastructure (including the telemetry-ingestion endpoints beyond
        documented rate limits), no reverse engineering except as permitted by law, and no
        uploading of data you don't have the right to send us.
      </p>

      <H>4. Plans, billing & cancellation</H>
      <p>
        Paid plans are billed in advance on a recurring basis (monthly or annually) through
        our payment processor, Stripe. Free plans require no payment. You can upgrade,
        downgrade, or cancel at any time from your account; cancellation takes effect at the
        end of the current billing period, and access to paid features ends then. Except where
        required by law, fees are non-refundable. We may change pricing on prospective notice.
        Your plan determines how many agents are <em>viewable</em>; telemetry you send is always
        recorded regardless of plan.
      </p>

      <H>5. Your data</H>
      <p>
        As between you and Trovis, you own the telemetry and content you send to the Service
        ("Customer Data"). You grant us a limited license to host, process, and analyze
        Customer Data to provide and improve the Service, including sending relevant content
        to our AI subprocessor to generate summaries and insights. Our handling of personal
        data is described in our <a href="/privacy" style={{ color: C.teal }}>Privacy Policy</a>.
      </p>

      <H>6. Intellectual property</H>
      <p>
        The Service, including its software, design, and content (excluding Customer Data),
        is owned by Trovis and protected by intellectual-property laws. These Terms grant you
        a limited, non-exclusive, non-transferable right to use the Service.
      </p>

      <H>7. Disclaimers</H>
      <p>
        The Service is provided "as is" and "as available" without warranties of any kind,
        express or implied. AI-generated descriptions and insights may be inaccurate or
        incomplete; you should not rely on them as the sole basis for decisions. We do not
        warrant that the Service will be uninterrupted or error-free.
      </p>

      <H>8. Limitation of liability</H>
      <p>
        To the maximum extent permitted by law, Trovis will not be liable for any indirect,
        incidental, special, consequential, or punitive damages, or for lost profits or data.
        Our total liability for any claim relating to the Service will not exceed the amounts
        you paid us in the twelve months before the event giving rise to the claim.
      </p>

      <H>9. Termination</H>
      <p>
        You may stop using the Service at any time. We may suspend or terminate access if you
        breach these Terms or use the Service in a way that risks harm to us or others. On
        termination, your right to use the Service ends; we may delete Customer Data after a
        reasonable retention period.
      </p>

      <H>10. Changes & governing law</H>
      <p>
        We may update these Terms; material changes will be communicated through the Service
        or by email, and continued use constitutes acceptance. These Terms are governed by the
        laws of the State of Nebraska, USA, without regard to conflict-of-laws rules.
      </p>

      <H>11. Contact</H>
      <p>
        Questions about these Terms? Email <a href={`mailto:${CONTACT}`} style={{ color: C.teal }}>{CONTACT}</a>.
      </p>
    </Layout>
  );
}

function PrivacyBody() {
  return (
    <Layout title="Privacy Policy">
      <p>
        This Privacy Policy explains how Trovis ("we," "us") collects, uses, and shares
        information when you use Trovis (the "Service"). By using the Service you agree to
        this policy.
      </p>

      <H>Information we collect</H>
      <p>
        <strong>Account information</strong> — your name, email, organization, and hashed
        password (and, for team members, invite details). <strong>Agent telemetry</strong> —
        the OpenTelemetry spans your agents send us, which may include operation names, tool
        calls, token counts, timing, and — when your agents are configured to capture it —
        message and response content. <strong>Usage &amp; billing</strong> — how you use the
        Service and, for paid plans, billing details handled by our payment processor.
        <strong> Diagnostics</strong> — limited error and performance data to keep the
        Service reliable.
      </p>

      <H>How we use information</H>
      <p>
        To provide and operate the Service (store telemetry, render your dashboard, generate
        plain-English summaries, cost figures, and drift insights), to process payments, to
        secure and improve the Service, to communicate with you (e.g., password resets,
        invites, service notices), and to comply with legal obligations.
      </p>

      <H>Subprocessors we share with</H>
      <p>
        We share data only with service providers who help us run Trovis, under contractual
        confidentiality obligations:
      </p>
      <ul style={{ margin: "0 0 8px", paddingLeft: 22 }}>
        <li><strong>Anthropic</strong> — generates plain-English summaries, drift analysis, and answers from your telemetry (Claude API).</li>
        <li><strong>Stripe</strong> — payment processing for paid plans (we don't store full card numbers).</li>
        <li><strong>Resend</strong> — sending transactional email (password resets, invites).</li>
        <li><strong>Sentry</strong> — error monitoring.</li>
        <li><strong>Railway &amp; Vercel</strong> — hosting of our backend, database, and frontend.</li>
      </ul>
      <p>We do not sell your personal information.</p>

      <H>Data retention</H>
      <p>
        We retain Customer Data for as long as your account is active and for a reasonable
        period afterward, unless you request earlier deletion. Backups may persist for a
        limited time before being overwritten.
      </p>

      <H>Security</H>
      <p>
        We use industry-standard measures to protect data in transit and at rest, including
        hashed credentials and tokens and access controls. No system is perfectly secure, so
        we cannot guarantee absolute security.
      </p>

      <H>Your rights</H>
      <p>
        Depending on your location, you may have rights to access, correct, export, or delete
        your personal data. To make a request — including deletion of your organization and
        its data — email <a href={`mailto:${CONTACT}`} style={{ color: C.teal }}>{CONTACT}</a> and
        we'll respond within a reasonable time.
      </p>

      <H>Cookies &amp; local storage</H>
      <p>
        The Service uses browser local storage to keep you signed in and remember preferences
        (such as your theme). We do not use third-party advertising cookies.
      </p>

      <H>Children</H>
      <p>The Service is not directed to children under 13 (or the minimum age in your jurisdiction), and we do not knowingly collect their data.</p>

      <H>Changes & contact</H>
      <p>
        We may update this policy; material changes will be communicated through the Service
        or by email. Questions? Email <a href={`mailto:${CONTACT}`} style={{ color: C.teal }}>{CONTACT}</a>.
      </p>
    </Layout>
  );
}

// page: 'terms' | 'privacy'
export default function TrovisLegal({ page }) {
  return page === "privacy" ? <PrivacyBody /> : <TermsBody />;
}
