import React, { useState, useEffect, useRef } from "react";

// HiveOS Mission Control — command-deck dashboard.
// Talks to the gateway: /health, /budget, /chat, /approvals, /approvals/decide.

const GATEWAY = import.meta.env.VITE_HIVE_GATEWAY || "http://localhost:8088";
const TOKEN = import.meta.env.VITE_HIVE_TOKEN || "change_me";
const hdr = { "Content-Type": "application/json", "x-hive-token": TOKEN };

export default function MissionControl() {
  const [online, setOnline] = useState(false);
  const [budget, setBudget] = useState(null);
  const [approvals, setApprovals] = useState([]);
  const [log, setLog] = useState([]);
  const [input, setInput] = useState("");
  const feedRef = useRef(null);

  const poll = async () => {
    try {
      const h = await fetch(`${GATEWAY}/health`).then((r) => r.json());
      setOnline(h.status === "ok");
      const a = await fetch(`${GATEWAY}/approvals`, { headers: hdr }).then((r) => r.json());
      setApprovals(a.pending || []);
      const b = await fetch(`${GATEWAY}/budget`, { headers: hdr }).then((r) => r.json());
      setBudget(b);
    } catch { setOnline(false); }
  };

  useEffect(() => { poll(); const t = setInterval(poll, 4000); return () => clearInterval(t); }, []);
  useEffect(() => { feedRef.current?.scrollTo(0, feedRef.current.scrollHeight); }, [log]);

  const send = async () => {
    const msg = input.trim(); if (!msg) return;
    setLog((l) => [...l, { who: "you", text: msg }]); setInput("");
    try {
      const r = await fetch(`${GATEWAY}/chat`, {
        method: "POST", headers: hdr,
        body: JSON.stringify({ session_id: "dashboard", message: msg }),
      }).then((r) => r.json());
      setLog((l) => [...l, { who: "hive", text: r.reply }]);
    } catch { setLog((l) => [...l, { who: "sys", text: "gateway unreachable" }]); }
  };

  const decide = async (id, approved) => {
    await fetch(`${GATEWAY}/approvals/decide`, {
      method: "POST", headers: hdr,
      body: JSON.stringify({ approval_id: id, approved }),
    });
    poll();
  };

  return (
    <div style={S.root}>
      <header style={S.top}>
        <span style={S.brand}>HIVE</span>
        <span style={S.sub}>// hiveos mission control</span>
        <span style={{ ...S.status, color: online ? "#39ff14" : "#ff3b30" }}>
          {online ? "● ONLINE" : "● OFFLINE"}
        </span>
        {budget && (
          <span style={S.budget}>
            calls {budget.calls_today}/{budget.daily_cap}
            {budget.remaining_pct != null && ` · window ${budget.remaining_pct}%`}
          </span>
        )}
      </header>

      <div style={S.grid}>
        <section style={S.panel}>
          <h2 style={S.h2}>CONVERSATION</h2>
          <div ref={feedRef} style={S.feed}>
            {log.map((m, i) => (
              <div key={i} style={S.line}>
                <span style={{ color: m.who === "you" ? "#7fdfff" : m.who === "sys" ? "#ff9f0a" : "#39ff14" }}>
                  {m.who}&gt;{" "}
                </span>
                <span style={S.txt}>{m.text}</span>
              </div>
            ))}
          </div>
          <div style={S.inputRow}>
            <input style={S.input} value={input} placeholder="mów do Hive…"
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()} />
            <button style={S.btn} onClick={send}>SEND</button>
          </div>
        </section>

        <section style={S.panel}>
          <h2 style={{ ...S.h2, color: "#ff9f0a" }}>
            APPROVAL INBOX {approvals.length > 0 && `(${approvals.length})`}
          </h2>
          <div style={S.feed}>
            {approvals.length === 0 && <div style={S.muted}>no pending approvals — Hive is acting freely</div>}
            {approvals.map((a) => (
              <div key={a.id} style={S.appCard}>
                <div style={S.appTool}>⚠ {a.tool} <span style={S.kind}>[{a.kind}]</span></div>
                <div style={S.appArgs}>{JSON.stringify(a.args)}</div>
                <div style={S.muted}>{a.reason}</div>
                <div style={{ marginTop: 8 }}>
                  <button style={{ ...S.btn, background: "#39ff14", color: "#000" }} onClick={() => decide(a.id, true)}>APPROVE</button>
                  <button style={{ ...S.btn, background: "#ff3b30", marginLeft: 8 }} onClick={() => decide(a.id, false)}>REJECT</button>
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

const mono = "'JetBrains Mono','SF Mono',ui-monospace,monospace";
const S = {
  root: { minHeight: "100vh", background: "radial-gradient(circle at 30% 0%,#0a1410,#05080a 60%)", color: "#cfe", fontFamily: mono, padding: 20 },
  top: { display: "flex", alignItems: "baseline", gap: 12, borderBottom: "1px solid #1c2b24", paddingBottom: 12, flexWrap: "wrap" },
  brand: { fontSize: 28, fontWeight: 800, letterSpacing: 4, color: "#39ff14", textShadow: "0 0 12px #39ff1466" },
  sub: { color: "#4a6a5a", fontSize: 13 },
  status: { marginLeft: "auto", fontSize: 13, letterSpacing: 1 },
  budget: { fontSize: 12, color: "#7fdfff", width: "100%", textAlign: "right" },
  grid: { display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 16, marginTop: 16 },
  panel: { border: "1px solid #1c2b24", borderRadius: 10, background: "#08110d99", padding: 14, display: "flex", flexDirection: "column", minHeight: "70vh" },
  h2: { margin: 0, fontSize: 12, letterSpacing: 3, color: "#39ff14", opacity: 0.8 },
  feed: { flex: 1, overflowY: "auto", marginTop: 10, fontSize: 13, lineHeight: 1.6 },
  line: { marginBottom: 6 }, txt: { color: "#bfe" },
  inputRow: { display: "flex", gap: 8, marginTop: 10 },
  input: { flex: 1, background: "#05080a", border: "1px solid #1c2b24", color: "#cfe", padding: "10px 12px", borderRadius: 6, fontFamily: mono, outline: "none" },
  btn: { background: "#1c2b24", color: "#cfe", border: "none", padding: "10px 14px", borderRadius: 6, cursor: "pointer", fontFamily: mono, fontWeight: 700, letterSpacing: 1 },
  muted: { color: "#4a6a5a", fontSize: 12 },
  appCard: { border: "1px solid #3a2e10", background: "#1a140622", borderRadius: 8, padding: 12, marginBottom: 10 },
  appTool: { color: "#ff9f0a", fontWeight: 700 }, kind: { color: "#7a6a3a", fontSize: 11 },
  appArgs: { color: "#bfe", fontSize: 12, margin: "4px 0", wordBreak: "break-all" },
};
