import React from "react";
import { createRoot } from "react-dom/client";
import Centre from "./Centre.jsx";

const TOKEN = import.meta.env.VITE_HIVE_TOKEN || "change_me";
const SESSION = (typeof crypto !== "undefined" && crypto.randomUUID)
  ? crypto.randomUUID()
  : `sess-${Date.now()}`;

createRoot(document.getElementById("root")).render(
  <Centre token={TOKEN} sessionId={SESSION} />
);
