import { useEffect, useRef } from "react";
import { api } from "./api.js";
import markup from "./landing/landing.html?raw";
import { mountLanding } from "./landing/landing.js";
import "./landing/landing.css";

/* ─────────────────────────────────────────────
   TROVIS — Public waitlist landing
   Logged-out front door of the SPA. The page itself (markup, styles,
   behaviour) lives in ./landing/ and is shared with the FastAPI-served
   trovisai.com page, so the two front doors can't drift apart.
   ───────────────────────────────────────────── */

const TITLE = "Trovis — Understand how work gets done across people, AI agents, and tools";
const DESCRIPTION =
  "Trovis uses AI to connect activity across your people, agents, and tools into a living picture of how your business works. Join the waitlist.";

export default function TrovisLanding({ onSignIn = () => {} }) {
  const ref = useRef(null);
  const signIn = useRef(onSignIn);
  signIn.current = onSignIn;

  useEffect(() => {
    const prevTitle = document.title;
    document.title = TITLE;
    const meta = document.querySelector('meta[name="description"]');
    const prevDesc = meta?.getAttribute("content");
    if (meta) meta.setAttribute("content", DESCRIPTION);
    const unmount = mountLanding(ref.current, {
      joinWaitlist: api.joinWaitlist,
      saveWaitlistFollowup: api.saveWaitlistFollowup,
      onSignIn: () => signIn.current(),
    });
    return () => {
      unmount();
      document.title = prevTitle;
      if (meta && prevDesc != null) meta.setAttribute("content", prevDesc);
    };
  }, []);

  return <div ref={ref} dangerouslySetInnerHTML={{ __html: markup }} />;
}
