"use client";

import { useEffect, useMemo, useState } from "react";
import {
  AgentGrid,
  CostPanel,
  CriterionPanel,
  EventLog,
  PlanPanel,
  ReasoningPanel,
  RedTeamPanel,
} from "@/components/panels";
import { useRunStream } from "@/lib/useRunStream";
import type { CostView, RunSummary } from "@/lib/types";

export default function Dashboard() {
  const stream = useRunStream();
  const [task, setTask] = useState(
    "extract every numeric claim from the supplied report and verify each one",
  );
  const [profile, setProfile] = useState("smoke");
  const [chaos, setChaos] = useState(true);
  const [useSkills, setUseSkills] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<RunSummary | null>(null);

  // The final report (cost, economy, red-team totals) is written when the run
  // finishes, so it is fetched then rather than streamed -- streaming a
  // whole-run aggregate on every event would be a lot of traffic to show a
  // number that only changes at the end.
  useEffect(() => {
    if (!stream.activeRun) return;
    if (stream.runStatus === "running" || stream.runStatus === null) return;
    let cancelled = false;
    fetch(`/api/runs/${stream.activeRun}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => {
        if (!cancelled && body) setSummary(body as RunSummary);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [stream.activeRun, stream.runStatus]);

  const cost: CostView | null = summary?.report?.cost ?? null;

  const selectedAgent = useMemo(
    () => stream.agents.find((a) => a.agent_id === selected) ?? null,
    [stream.agents, selected],
  );

  // Taint comes from the ledger, not from a UI setting. If any row in the run
  // was synthetic the banner shows, and there is no way to configure it away.
  const simulated = cost?.simulated === true;

  const start = async () => {
    setSubmitting(true);
    setError(null);
    setSummary(null);
    try {
      await stream.submit(task, profile, chaos, useSkills);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="shell">
      {simulated && (
        <div className="simulated-banner">
          SIMULATED RUN — every response came from the synthetic provider.
          These numbers are not evidence of anything and `swarmd eval` will
          refuse to report from them.
        </div>
      )}

      <header className="bar">
        <span className="brand">swarmd</span>

        <span className={`status-dot ${stream.connection}`}>
          <i />
          {stream.connection === "open"
            ? "live"
            : stream.connection === "connecting"
              ? "connecting"
              : "disconnected"}
        </span>

        <input
          type="text"
          value={task}
          onChange={(e) => setTask(e.target.value)}
          placeholder="describe a task nobody scoped for this system"
          aria-label="task"
        />

        <select
          value={profile}
          onChange={(e) => setProfile(e.target.value)}
          aria-label="profile"
        >
          <option value="smoke">smoke · ~2 min</option>
          <option value="standard">standard · 12–18 min</option>
          <option value="deep">deep · ~40 min</option>
        </select>

        <label className="toggle">
          <input
            type="checkbox"
            checked={chaos}
            onChange={(e) => setChaos(e.target.checked)}
          />
          chaos
        </label>

        <label className="toggle" title="Unchecking runs the control arm: skills disabled, everything else identical.">
          <input
            type="checkbox"
            checked={useSkills}
            onChange={(e) => setUseSkills(e.target.checked)}
          />
          skills
        </label>

        <button onClick={start} disabled={submitting || !task.trim()}>
          {submitting ? "starting…" : "run"}
        </button>

        {stream.activeRun && (
          <span style={{ color: "var(--muted)" }}>
            {stream.activeRun} · {stream.runStatus}
          </span>
        )}
        {error && <span style={{ color: "var(--bad)" }}>{error}</span>}
      </header>

      {!useSkills && (
        <div className="simulated-banner" style={{ background: "#1b2635", borderColor: "var(--accent)", color: "var(--accent)" }}>
          CONTROL ARM — skill retrieval disabled. This is the ablation an
          improvement claim is measured against.
        </div>
      )}

      <div className="grid">
        <CriterionPanel criterion={stream.criterion} />
        <PlanPanel plan={stream.plan} agents={stream.agents} />
        <EventLog events={stream.events} />

        <AgentGrid
          agents={stream.agents}
          selected={selected}
          onSelect={setSelected}
        />
        <ReasoningPanel agent={selectedAgent} />
        <div style={{ display: "grid", gridTemplateRows: "1fr 1fr", gap: 10, minHeight: 0 }}>
          <CostPanel cost={cost} />
          <RedTeamPanel
            containments={stream.containments}
            chaos={stream.chaosEvents}
          />
        </div>
      </div>
    </div>
  );
}
