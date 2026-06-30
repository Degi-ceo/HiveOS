import React from "react";
import { createRoot } from "react-dom/client";
import Centre from "./Centre.jsx";

// VITE_HIVE_TOKEN is the gateway shared secret. We REFUSE to start the dashboard
// without it — otherwise an unconfigured build would silently ship with no
// working credentials (or a placeholder literal that hits a real running
// gateway).
const TOKEN = import.meta.env.VITE_HIVE_TOKEN;
if (!TOKEN) {
  // eslint-disable-next-line no-console
  console.error("[Centre] VITE_HIVE_TOKEN env var is required at build time. " +
                "Set it before running `npm run build` or `npm run dev`.");
}
const SESSION = (typeof crypto !== "undefined" && crypto.randomUUID)
  ? crypto.randomUUID()
  : `sess-${Date.now()}`;

createRoot(document.getElementById("root")).render(
  <Centre token={TOKEN || ""} sessionId={SESSION} />
);
