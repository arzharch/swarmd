"use client";

import { useEffect, useState } from "react";
import type { ConnectionState } from "@/lib/types";

/**
 * App shell: nav rail + frosted top bar, following OpenDesign's structure.
 *
 * The rail exists because the dashboard now has more to show than fits on one
 * screen without everything becoming a postage stamp. Grouping by *question*
 * rather than by data source is deliberate — "what is it doing", "what did it
 * decide", "what did it cost" are the three things someone actually walks up
 * to this screen asking.
 */

export type ViewId =
  | "run"
  | "decisions"
  | "cost"
  | "trace"
  | "evals"
  | "harness";

export const VIEWS: Array<{ id: ViewId; label: string; question: string }> = [
  { id: "run", label: "Live run", question: "what is it doing right now" },
  { id: "decisions", label: "Decisions", question: "what did it decide, and why" },
  { id: "cost", label: "Cost & safety", question: "what did it spend, what was contained" },
  { id: "trace", label: "Traceability", question: "where did that number come from" },
  { id: "evals", label: "Evals & sessions", question: "is it actually improving" },
  { id: "harness", label: "Harness", question: "what is it configured to do" },
];

type Theme = "light" | "dark" | "system";

/**
 * Theme is persisted and applied to <html> rather than held in React state
 * alone, so a reload does not flash the wrong palette. "system" deliberately
 * removes the attribute rather than resolving it, which is what lets the
 * prefers-color-scheme block in globals.css take over.
 */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>("system");

  useEffect(() => {
    const stored = window.localStorage.getItem("swarmd-theme") as Theme | null;
    if (stored) setTheme(stored);
  }, []);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    window.localStorage.setItem("swarmd-theme", theme);
  }, [theme]);

  const cycle = () =>
    setTheme((t) => (t === "system" ? "light" : t === "light" ? "dark" : "system"));

  return { theme, cycle };
}

export function Rail({
  view,
  onView,
  counts,
  runId,
  runStatus,
}: {
  view: ViewId;
  onView: (v: ViewId) => void;
  counts: Record<string, number>;
  runId: string | null;
  runStatus: string | null;
}) {
  const { theme, cycle } = useTheme();

  return (
    <nav className="rail" aria-label="Sections">
      <div className="brand">
        <span className="dot" aria-hidden />
        swarmd
      </div>

      <div className="rail-section">Workspace</div>
      {VIEWS.map((entry) => (
        <button
          key={entry.id}
          className="rail-item"
          aria-current={view === entry.id}
          onClick={() => onView(entry.id)}
          title={entry.question}
        >
          <span>{entry.label}</span>
          {counts[entry.id] > 0 && (
            <span className="count">{counts[entry.id]}</span>
          )}
        </button>
      ))}

      <div className="rail-section">Current run</div>
      <div style={{ padding: "0 12px", fontSize: 12 }}>
        {runId ? (
          <>
            <div className="mono" style={{ color: "var(--text-muted)" }}>
              {runId}
            </div>
            <div style={{ marginTop: 6 }}>
              <span className={`pill ${statusTone(runStatus)}`}>
                {runStatus ?? "idle"}
              </span>
            </div>
          </>
        ) : (
          <span style={{ color: "var(--text-faint)" }}>no run yet</span>
        )}
      </div>

      <div className="rail-footer">
        <button className="rail-item" onClick={cycle}>
          <span>Theme</span>
          <span className="count">{theme}</span>
        </button>
      </div>
    </nav>
  );
}

function statusTone(status: string | null): string {
  if (status === "completed") return "passed";
  if (status === "running") return "running";
  if (status === "failed" || status === "error") return "failed";
  if (status === "aborted" || status === "failed_criterion") return "killed";
  return "neutral";
}

export function TopBar({
  task,
  onTask,
  profile,
  onProfile,
  chaos,
  onChaos,
  useSkills,
  onUseSkills,
  agents,
  onAgents,
  seedRogues,
  onSeedRogues,
  onRun,
  submitting,
  connection,
  error,
}: {
  task: string;
  onTask: (v: string) => void;
  profile: string;
  onProfile: (v: string) => void;
  chaos: boolean;
  onChaos: (v: boolean) => void;
  useSkills: boolean;
  onUseSkills: (v: boolean) => void;
  agents: string;
  onAgents: (v: string) => void;
  seedRogues: string;
  onSeedRogues: (v: string) => void;
  onRun: () => void;
  submitting: boolean;
  connection: ConnectionState;
  error: string | null;
}) {
  return (
    <header className="topbar">
      <input
        type="text"
        value={task}
        onChange={(e) => onTask(e.target.value)}
        placeholder="Describe a task nobody scoped for this system…"
        aria-label="Task"
      />

      <select
        value={profile}
        onChange={(e) => onProfile(e.target.value)}
        aria-label="Run profile"
      >
        <option value="smoke">smoke · ~2 min</option>
        <option value="standard">standard · 12–18 min</option>
        <option value="deep">deep · ~40 min</option>
      </select>

      <input
        className="agents"
        type="number"
        min={1}
        max={2000}
        value={agents}
        onChange={(e) => onAgents(e.target.value)}
        placeholder="agents"
        aria-label="Agent count"
        title="How many agents to run. Empty uses the profile's figure. Above 16 per plan node the cost ceiling, not a cap, is what stops the run — batched generation and the semantic cache are not implemented yet."
      />

      <select
        value={seedRogues}
        onChange={(e) => onSeedRogues(e.target.value)}
        aria-label="Seed rogue agents"
        title="Inject deliberate misbehaviour. The red-team is not told which agents are seeded; the run reports whether each pattern's own detector caught it."
      >
        <option value="">No rogues</option>
        <option value="all">Seed all 5 rogues</option>
        <option value="budget_siphon">Rogue: budget siphon</option>
        <option value="loop">Rogue: loop</option>
        <option value="criterion_gaming">Rogue: criterion gaming</option>
        <option value="unsafe_tool_call">Rogue: unsafe tool call</option>
        <option value="library_poisoning">Rogue: library poisoning</option>
      </select>

      <label className="toggle" title="Kill agents mid-run. Recovery is the guarantee this tests.">
        <input
          type="checkbox"
          checked={chaos}
          onChange={(e) => onChaos(e.target.checked)}
        />
        Chaos
      </label>

      <label
        className="toggle"
        title="Unchecking runs the CONTROL ARM: skill retrieval disabled, everything else identical. This is what an improvement claim is measured against."
      >
        <input
          type="checkbox"
          checked={useSkills}
          onChange={(e) => onUseSkills(e.target.checked)}
        />
        Skills
      </label>

      <button className="primary" onClick={onRun} disabled={submitting || !task.trim()}>
        {submitting ? "Starting…" : "Run"}
      </button>

      <span className={`conn ${connection}`} title={`Event stream ${connection}`}>
        <i aria-hidden />
        {connection === "open"
          ? "Live"
          : connection === "connecting"
            ? "Connecting"
            : "Disconnected"}
      </span>

      {error && (
        <span style={{ color: "var(--red)", fontSize: 12 }}>{error}</span>
      )}
    </header>
  );
}
