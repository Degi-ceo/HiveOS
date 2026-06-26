import React, { useState, useEffect, useRef, useCallback } from "react";

// HiveOS Mission Control — command-deck dashboard.
// Talks to the gateway: /health, /budget, /chat/stream (SSE), /approvals, /approvals/decide,
// /telemetry, /audit, /tasks.

const GATEWAY = import.meta.env.VITE_HIVE_GATEWAY || "http://localhost:8088";
const TOKEN = import.meta.env.VITE_HIVE_TOKEN || "change_me";
const hdr = { "Content-Type": "application/json", "x-hive-token": TOKEN };

// SPRINT_6 P-C: tool-loop iteration chip colors.
const chipBg = (t) => ({
  tool_call_start: "#0a2a55", tool_call_end: "#0a4d4d",
  model_decision: "#3a1454", loop_guard: "#5a1820",
  final: "#0a4d2a", max_turns: "#5a4218", error: "#5a1820",
}[t] || "#1c2b24");
const chipFg = (t) => ({
  tool_call_start: "#7fdfff", tool_call_end: "#5fffe0",
  model_decision: "#c77dff", loop_guard: "#ff6b9d",
  final: "#39ff14", max_turns: "#ffbf3a", error: "#ff6b6b",
}[t] || "#bfe");
const summarise = (ev) => {
  if (ev.type === "model_decision") {
    const tcs = ev.tool_calls || [];
    return (ev.text ? `"${ev.text}"` : "") + (tcs.length ? ` → calls: ${tcs.map((c) => c.name).join(", ")}` : "");
  }
  if (ev.type === "tool_call_start") return `${ev.name}(${JSON.stringify(ev.arguments || {})})`;
  if (ev.type === "tool_call_end") return `${ev.name} → ${ev.status}${ev.status === "error" ? ": " + (ev.content || "").slice(0, 80) : ""}`;
  if (ev.type === "loop_guard") return `${ev.tool}: ${ev.reason}`;
  if (ev.type === "final") return `"${ev.text}" (${ev.tool_calls} tool calls)`;
  if (ev.type === "max_turns") return `"${ev.text}" (${ev.tool_calls} tool calls)`;
  if (ev.type === "error") return ev.class || "exception";
  return JSON.stringify(ev).slice(0, 200);
};

export default function MissionControl() {
  const [online, setOnline] = useState(false);
  const [budget, setBudget] = useState(null);
  const [approvals, setApprovals] = useState([]);
  const [log, setLog] = useState([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [telemetry, setTelemetry] = useState(null);
  const [auditEntries, setAuditEntries] = useState([]);
  const [tasks, setTasks] = useState({ pending: 0, tasks: [] });
  const [skills, setSkills] = useState(null);
  const [skillFilter, setSkillFilter] = useState("active");
  // SPRINT_6 P-C: live tool-loop iteration log. Cleared per turn; capped at 200 rows.
  const [iterLog, setIterLog] = useState([]);
  const feedRef = useRef(null);
  const streamIdRef = useRef(null);   // id of the in-progress hive entry

  const poll = useCallback(async () => {
    try {
      const h = await fetch(`${GATEWAY}/health`).then((r) => r.json());
      setOnline(h.status === "ok");
      const a = await fetch(`${GATEWAY}/approvals`, { headers: hdr }).then((r) => r.json());
      setApprovals(a.pending || []);
      const b = await fetch(`${GATEWAY}/budget`, { headers: hdr }).then((r) => r.json());
      setBudget(b);
    } catch {
      setOnline(false);
    }
  }, []);

  const pollTelemetry = useCallback(async () => {
    try {
      const t = await fetch(`${GATEWAY}/telemetry`, { headers: hdr }).then((r) => r.json());
      setTelemetry(t);
    } catch { /* ignore */ }
  }, []);

  const pollAudit = useCallback(async () => {
    try {
      const d = await fetch(`${GATEWAY}/audit?limit=20`, { headers: hdr }).then((r) => r.json());
      setAuditEntries(d.entries || []);
    } catch { /* ignore */ }
  }, []);

  const pollTasks = useCallback(async () => {
    try {
      const d = await fetch(`${GATEWAY}/tasks`, { headers: hdr }).then((r) => r.json());
      setTasks(d);
    } catch { /* ignore */ }
  }, []);

  const pollSkills = useCallback(async () => {
    try {
      const d = await fetch(`${GATEWAY}/skills/`, { headers: hdr }).then((r) => r.json());
      setSkills(d);
    } catch { /* ignore */ }
  }, []);

  const pinSkill = async (name, pinned) => {
    await fetch(`${GATEWAY}/skills/${encodeURIComponent(name)}/pin`, {
      method: "POST", headers: hdr,
      body: JSON.stringify({ pinned }),
    }).catch(() => {});
    pollSkills();
  };

  const archiveSkill = async (name) => {
    await fetch(`${GATEWAY}/skills/${encodeURIComponent(name)}/state`, {
      method: "POST", headers: hdr,
      body: JSON.stringify({ state: "archived" }),
    }).catch(() => {});
    pollSkills();
  };

  useEffect(() => {
    poll();
    const t = setInterval(poll, 4000);
    return () => clearInterval(t);
  }, [poll]);

  useEffect(() => {
    pollTelemetry();
    const t = setInterval(pollTelemetry, 10000);
    return () => clearInterval(t);
  }, [pollTelemetry]);

  useEffect(() => {
    pollAudit();
    const t = setInterval(pollAudit, 6000);
    return () => clearInterval(t);
  }, [pollAudit]);

  useEffect(() => {
    pollTasks();
    const t = setInterval(pollTasks, 5000);
    return () => clearInterval(t);
  }, [pollTasks]);

  useEffect(() => {
    pollSkills();
    const t = setInterval(pollSkills, 15000);
    return () => clearInterval(t);
  }, [pollSkills]);

  useEffect(() => {
    feedRef.current?.scrollTo(0, feedRef.current.scrollHeight);
  }, [log]);

  const send = async () => {
    const msg = input.trim();
    if (!msg || streaming) return;
    setInput("");

    const entryId = Date.now();
    streamIdRef.current = entryId;
    setLog((l) => [
      ...l,
      { id: `u${entryId}`, who: "you", text: msg },
      { id: entryId, who: "hive", text: "", live: true },
    ]);
    // SPRINT_6 P-C: reset iteration log for this turn.
    setIterLog([]);
    setStreaming(true);

    try {
      // SPRINT_6 P-C: SINGLE fetch to /chat/stream/iterations. The orchestrator
      // emits per-iteration events; we extract text from model_decision.text and
      // surface tool chips from tool_call_start/end/loop_guard/final/max_turns/error.
      // One LLM call per turn (was previously 2x via dual fetch — see review fix).
      const resp = await fetch(`${GATEWAY}/chat/stream/iterations`, {
        method: "POST",
        headers: hdr,
        body: JSON.stringify({ session_id: "dashboard", message: msg }),
      });
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const blocks = buf.split("\n\n");
        buf = blocks.pop() ?? "";
        for (const block of blocks) {
          const m = block.match(/^event: (\S+)\ndata: (.+)$/m);
          if (!m) continue;
          const evType = m[1];
          if (evType === "[DONE]" || m[2] === "[DONE]") break;
          let payload = {};
          try { payload = JSON.parse(m[2]); } catch (_e) { /* ignore */ }
          // model_decision.text feeds the chat bubble (preserves live-stream feel).
          if (evType === "model_decision" && payload.text) {
            setLog((l) =>
              l.map((m) =>
                m.id === entryId ? { ...m, text: m.text + payload.text } : m
              )
            );
          }
          // All iteration events (including model_decision) populate the TOOL LOOP panel.
          setIterLog((log) => {
            const next = [...log, { type: evType, ...payload }];
            return next.length > 200 ? next.slice(-200) : next;
          });
        }
      }
    } catch (e) {
      setLog((l) =>
        l.map((m) =>
          m.id === entryId ? { ...m, text: `[error: ${e.message}]` } : m
        )
      );
    } finally {
      setLog((l) => l.map((m) => (m.id === entryId ? { ...m, live: false } : m)));
      setStreaming(false);
    }
  };

  const decide = async (id, approved) => {
    await fetch(`${GATEWAY}/approvals/decide`, {
      method: "POST",
      headers: hdr,
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
        {/* ── MODEL USAGE ── */}
        <section style={{ ...S.panel, minHeight: "auto" }}>
          <h2 style={{ ...S.h2, color: "#7fdfff" }}>MODEL USAGE</h2>
          <div style={{ ...S.feed, marginTop: 8 }}>
            {!telemetry && <div style={S.muted}>polling…</div>}
            {telemetry && (
              <>
                <div style={S.statRow}>
                  <span style={S.statLabel}>inference calls</span>
                  <span style={S.statVal}>{telemetry.inference_calls}</span>
                </div>
                <div style={S.statRow}>
                  <span style={S.statLabel}>input tokens</span>
                  <span style={S.statVal}>{telemetry.input_tokens.toLocaleString()}</span>
                </div>
                <div style={S.statRow}>
                  <span style={S.statLabel}>output tokens</span>
                  <span style={S.statVal}>{telemetry.output_tokens.toLocaleString()}</span>
                </div>
                <div style={S.statRow}>
                  <span style={S.statLabel}>total cost</span>
                  <span style={{ ...S.statVal, color: "#ff9f0a" }}>${telemetry.cost_usd.toFixed(4)}</span>
                </div>
                {Object.entries(telemetry.by_model).map(([model, calls]) => (
                  <div key={model} style={S.modelRow}>
                    <span style={S.modelName}>{model}</span>
                    <span style={S.muted}>{calls} calls</span>
                    {telemetry.cost_by_model[model] != null && (
                      <span style={{ ...S.muted, marginLeft: 8 }}>
                        ${telemetry.cost_by_model[model].toFixed(4)}
                      </span>
                    )}
                  </div>
                ))}
              </>
            )}
          </div>
        </section>

        {/* ── RECENT EXECUTIONS ── */}
        <section style={{ ...S.panel, minHeight: "auto" }}>
          <h2 style={{ ...S.h2, color: "#c77dff" }}>RECENT EXECUTIONS</h2>
          <div style={{ ...S.feed, marginTop: 8 }}>
            {auditEntries.length === 0 && <div style={S.muted}>no tool calls yet</div>}
            {auditEntries.map((e, i) => (
              <div key={i} style={S.auditRow}>
                <span style={{ color: e.status === "ok" ? "#39ff14" : "#ff3b30", minWidth: 8 }}>
                  {e.status === "ok" ? "✓" : "✗"}
                </span>
                <span style={S.auditTool}>{e.tool}</span>
                {e.approved && <span style={S.auditBadge}>approved</span>}
                {e.error && <span style={{ ...S.muted, marginLeft: 6 }}>{e.error}</span>}
              </div>
            ))}
          </div>
        </section>

        {/* ── TASK QUEUE ── */}
        <section style={{ ...S.panel, minHeight: "auto", gridColumn: "1 / -1" }}>
          <h2 style={{ ...S.h2, color: "#ff9f0a" }}>
            TASK QUEUE {tasks.pending > 0 && <span style={{ color: "#ff3b30" }}>({tasks.pending} pending)</span>}
          </h2>
          <div style={{ ...S.feed, marginTop: 8 }}>
            {tasks.tasks.length === 0 && <div style={S.muted}>no tasks</div>}
            {tasks.tasks.slice(0, 5).map((t) => (
              <div key={t.id} style={S.taskRow}>
                <span style={{ ...S.taskState, color: stateColor(t.state) }}>{t.state}</span>
                <span style={S.auditTool}>{t.kind}</span>
                <span style={S.muted}>{t.source}</span>
                {t.attempts > 0 && <span style={{ ...S.muted, marginLeft: 6 }}>×{t.attempts}</span>}
              </div>
            ))}
          </div>
        </section>

        {/* ── SKILLS ── */}
        <section style={{ ...S.panel, minHeight: "auto", gridColumn: "1 / -1" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
            <h2 style={{ ...S.h2, color: "#c77dff", margin: 0 }}>
              SKILLS {skills && `(${skills.total ?? 0})`}
            </h2>
            {["active", "stale", "archived", "all"].map((f) => (
              <button
                key={f}
                style={{
                  ...S.btn,
                  padding: "3px 10px",
                  fontSize: 11,
                  background: skillFilter === f ? "#2a1f44" : "#1c2b24",
                  color: skillFilter === f ? "#c77dff" : "#4a6a5a",
                  border: skillFilter === f ? "1px solid #c77dff44" : "1px solid transparent",
                }}
                onClick={() => setSkillFilter(f)}
              >
                {f}
              </button>
            ))}
          </div>
          <div style={{ ...S.feed, marginTop: 0, maxHeight: 200 }}>
            {!skills && <div style={S.muted}>polling…</div>}
            {skills && (skills.skills || []).filter((s) => skillFilter === "all" || s.state === skillFilter).length === 0 && (
              <div style={S.muted}>no {skillFilter === "all" ? "" : skillFilter + " "}skills</div>
            )}
            {skills && (skills.skills || [])
              .filter((s) => skillFilter === "all" || s.state === skillFilter)
              .map((s) => (
                <div key={s.name} style={S.skillRow}>
                  <span style={{ ...S.auditTool, flex: 2 }} title={s.description}>{s.name}</span>
                  <span style={{ ...S.muted, minWidth: 60 }}>{s.state}</span>
                  <span style={{ ...S.muted, minWidth: 40 }}>×{s.use_count ?? 0}</span>
                  <span style={{ minWidth: 20, textAlign: "center", color: "#ff9f0a" }}>
                    {s.pinned ? "📌" : ""}
                  </span>
                  <button
                    style={{ ...S.btn, padding: "2px 8px", fontSize: 11 }}
                    onClick={() => pinSkill(s.name, !s.pinned)}
                  >
                    {s.pinned ? "unpin" : "pin"}
                  </button>
                  {s.state !== "archived" && (
                    <button
                      style={{ ...S.btn, padding: "2px 8px", fontSize: 11, color: "#ff3b3088" }}
                      onClick={() => archiveSkill(s.name)}
                    >
                      archive
                    </button>
                  )}
                </div>
              ))}
          </div>
        </section>

        <section style={S.panel}>
          <h2 style={S.h2}>CONVERSATION{streaming && <span style={S.streamDot}> ▋</span>}</h2>
          <div ref={feedRef} style={S.feed}>
            {log.map((m) => (
              <div key={m.id} style={S.line}>
                <span
                  style={{
                    color: m.who === "you" ? "#7fdfff" : m.who === "sys" ? "#ff9f0a" : "#39ff14",
                  }}
                >
                  {m.who}&gt;{" "}
                </span>
                <span style={S.txt}>
                  {m.text}
                  {m.live && <span style={S.cursor}>▋</span>}
                </span>
              </div>
            ))}
          </div>
          <div style={S.inputRow}>
            <input
              style={S.input}
              value={input}
              placeholder="mów do Hive…"
              disabled={streaming}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()}
            />
            <button style={{ ...S.btn, opacity: streaming ? 0.4 : 1 }} onClick={send} disabled={streaming}>
              {streaming ? "…" : "SEND"}
            </button>
          </div>
        </section>

        <section style={{ ...S.panel, gridColumn: "1 / -1" }}>
          <h2 style={{ ...S.h2, color: "#c77dff" }}>
            TOOL LOOP {iterLog.length > 0 && `(${iterLog.length} events)`}
          </h2>
          <div style={{ ...S.feed, maxHeight: 180 }}>
            {iterLog.length === 0 && (
              <div style={S.muted}>no tool activity yet — send a turn to see live iterations</div>
            )}
            {iterLog.map((ev, i) => (
              <div key={i} style={S.auditRow}>
                <span style={{
                  ...S.auditBadge,
                  background: chipBg(ev.type),
                  color: chipFg(ev.type),
                  minWidth: 110, display: "inline-block",
                }}>
                  {ev.type}
                </span>
                <span style={{ flex: 1, color: "#cfd8e3" }}>
                  {summarise(ev)}
                </span>
              </div>
            ))}
          </div>
        </section>

        <section style={S.panel}>
          <h2 style={{ ...S.h2, color: "#ff9f0a" }}>
            APPROVAL INBOX {approvals.length > 0 && `(${approvals.length})`}
          </h2>
          <div style={S.feed}>
            {approvals.length === 0 && (
              <div style={S.muted}>no pending approvals — Hive is acting freely</div>
            )}
            {approvals.map((a) => (
              <div key={a.id} style={S.appCard}>
                <div style={S.appTool}>
                  ⚠ {a.tool} <span style={S.kind}>[{a.kind}]</span>
                </div>
                <div style={S.appArgs}>{JSON.stringify(a.args)}</div>
                <div style={S.muted}>{a.reason}</div>
                <div style={{ marginTop: 8 }}>
                  <button
                    style={{ ...S.btn, background: "#39ff14", color: "#000" }}
                    onClick={() => decide(a.id, true)}
                  >
                    APPROVE
                  </button>
                  <button
                    style={{ ...S.btn, background: "#ff3b30", marginLeft: 8 }}
                    onClick={() => decide(a.id, false)}
                  >
                    REJECT
                  </button>
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

function stateColor(state) {
  if (state === "pending") return "#ff9f0a";
  if (state === "running") return "#7fdfff";
  if (state === "done") return "#39ff14";
  if (state === "failed") return "#ff3b30";
  return "#4a6a5a";
}

const mono = "'JetBrains Mono','SF Mono',ui-monospace,monospace";
const S = {
  root: {
    minHeight: "100vh",
    background: "radial-gradient(circle at 30% 0%,#0a1410,#05080a 60%)",
    color: "#cfe",
    fontFamily: mono,
    padding: 20,
  },
  top: {
    display: "flex",
    alignItems: "baseline",
    gap: 12,
    borderBottom: "1px solid #1c2b24",
    paddingBottom: 12,
    flexWrap: "wrap",
  },
  brand: { fontSize: 28, fontWeight: 800, letterSpacing: 4, color: "#39ff14", textShadow: "0 0 12px #39ff1466" },
  sub: { color: "#4a6a5a", fontSize: 13 },
  status: { marginLeft: "auto", fontSize: 13, letterSpacing: 1 },
  budget: { fontSize: 12, color: "#7fdfff", width: "100%", textAlign: "right" },
  streamDot: { color: "#39ff14", animation: "blink 1s step-end infinite" },
  grid: { display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 16, marginTop: 16, alignItems: "start" },
  panel: {
    border: "1px solid #1c2b24",
    borderRadius: 10,
    background: "#08110d99",
    padding: 14,
    display: "flex",
    flexDirection: "column",
    minHeight: "70vh",
  },
  h2: { margin: 0, fontSize: 12, letterSpacing: 3, color: "#39ff14", opacity: 0.8 },
  feed: { flex: 1, overflowY: "auto", marginTop: 10, fontSize: 13, lineHeight: 1.6 },
  line: { marginBottom: 6 },
  txt: { color: "#bfe" },
  cursor: { color: "#39ff14", animation: "blink 1s step-end infinite" },
  inputRow: { display: "flex", gap: 8, marginTop: 10 },
  input: {
    flex: 1,
    background: "#05080a",
    border: "1px solid #1c2b24",
    color: "#cfe",
    padding: "10px 12px",
    borderRadius: 6,
    fontFamily: mono,
    outline: "none",
  },
  btn: {
    background: "#1c2b24",
    color: "#cfe",
    border: "none",
    padding: "10px 14px",
    borderRadius: 6,
    cursor: "pointer",
    fontFamily: mono,
    fontWeight: 700,
    letterSpacing: 1,
  },
  muted: { color: "#4a6a5a", fontSize: 12 },
  appCard: {
    border: "1px solid #3a2e10",
    background: "#1a140622",
    borderRadius: 8,
    padding: 12,
    marginBottom: 10,
  },
  appTool: { color: "#ff9f0a", fontWeight: 700 },
  kind: { color: "#7a6a3a", fontSize: 11 },
  appArgs: { color: "#bfe", fontSize: 12, margin: "4px 0", wordBreak: "break-all" },
  statRow: { display: "flex", justifyContent: "space-between", fontSize: 12, padding: "2px 0", borderBottom: "1px solid #1c2b2422" },
  statLabel: { color: "#4a6a5a" },
  statVal: { color: "#cfe", fontWeight: 700 },
  modelRow: { display: "flex", alignItems: "center", gap: 8, fontSize: 12, padding: "2px 0" },
  modelName: { color: "#7fdfff", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  auditRow: { display: "flex", alignItems: "center", gap: 6, fontSize: 12, padding: "2px 0" },
  auditTool: { color: "#bfe", flex: 1 },
  auditBadge: { fontSize: 10, background: "#1c2b24", color: "#39ff14", padding: "1px 5px", borderRadius: 4 },
  taskRow: { display: "flex", alignItems: "center", gap: 8, fontSize: 12, padding: "2px 0" },
  taskState: { fontWeight: 700, minWidth: 56 },
  skillRow: { display: "flex", alignItems: "center", gap: 8, fontSize: 12, padding: "3px 0", borderBottom: "1px solid #1c2b2422" },
};
