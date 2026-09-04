import React from "react";
import { createRoot } from "react-dom/client";
import Centre from "./Centre.jsx";
import { UiPreview } from "./ui-preview/UiPreview.jsx";

const IS_UI_PREVIEW = new URLSearchParams(window.location.search).get("ui-preview") === "1";

// VITE_HIVE_TOKEN is the gateway shared secret. We REFUSE to start the dashboard
// without it — otherwise an unconfigured build would silently ship with no
// working credentials (or a placeholder literal that hits a real running
// gateway).
const TOKEN = import.meta.env.VITE_HIVE_TOKEN;
if (!TOKEN && !IS_UI_PREVIEW) {
  // eslint-disable-next-line no-console
  console.error("[Centre] VITE_HIVE_TOKEN env var is required at build time. " +
                "Set it before running `npm run build` or `npm run dev`.");
}
const SESSION = (typeof crypto !== "undefined" && crypto.randomUUID)
  ? crypto.randomUUID()
  : `sess-${Date.now()}`;

createRoot(document.getElementById("root")).render(
  IS_UI_PREVIEW
    ? <UiPreview />
    : <Centre token={TOKEN || ""} sessionId={SESSION} />
);
