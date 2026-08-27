"use client";

import type {
  AgentState,
  CostView,
  CriterionView,
  PlanView,
  SwarmEvent,
} from "@/lib/types";

/**
 * Presentational cards in OpenDesign's visual language.
 *
 * Every one renders props or an empty state, and the empty states say what is
 * missing rather than showing plausible placeholder numbers — a screenshot of
 * a placeholder is indistinguishable from a screenshot of a result, which is
 * the whole reason ADR-006 exists.
 */

export function Card({
  title,
  meta,
  tall,
  children,
}: {
  title: string;
  meta?: React.ReactNode;
  tall?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section className={tall ? "card tall" : "card"}>
      <header>
        <h2>{title}</h2>
        {meta ? <span className="meta">{meta}</span> : null}
      </header>
      <div className="body">{children}</div>
    </section>
  );
}

function Empty({ children, hint }: { children: React.ReactNode; hint?: string }) {
  return (
    <p className="empty">
      {children}
      {hint ? <span className="hint">{hint}</span> : null}
    </p>
  );
}

/* ---------------------------------------------------------------- criterion */

export function CriterionPanel({ criterion }: { criterion: CriterionView | null }) {
  if (!criterion) {
    return (
      <Card title="Success criterion">
        <Empty hint="Nothing may be solved until success is defined and has survived an adversarial pass.">
          Not yet frozen.
        </Empty>
      </Card>
    );
  }
  return (
    <Card title="Success criterion" meta={criterion.hash}>
      <p style={{ margin: "0 0 10px", color: "var(--text-strong)" }}>
        {criterion.criterion.description}
      </p>

      <div style={{ marginBottom: 10 }}>
        <span className="pill passed">{criterion.attack}</span>
      </div>

      <dl className="kv" style={{ marginBottom: 12 }}>
        <dt>attempts</dt>
        <dd>{criterion.attempts}</dd>
        <dt>proposer agreement</dt>
        <dd>{(criterion.agreement * 100).toFixed(0)}%</dd>
      </dl>

      {criterion.criterion.checks.map((check, i) => (
        <div className="check" key={i}>
          <code>{check.kind}</code>
          <span className="params">{JSON.stringify(check.params)}</span>
        </div>
      ))}

      {criterion.history.length > 0 && (
        <details style={{ marginTop: 10, color: "var(--text-soft)", fontSize: 12 }}>
          <summary style={{ cursor: "pointer" }}>synthesis history</summary>
          {criterion.history.map((line, i) => (
            <div key={i} style={{ padding: "2px 0" }}>
              {line}
            </div>
          ))}
        </details>
      )}
    </Card>
  );
}

/* --------------------------------------------------------------------- plan */

export function PlanPanel({
  plan,
  agents,
}: {
  plan: PlanView | null;
  agents: AgentState[];
}) {
  if (!plan) {
    return (
      <Card title="Generated plan">
        <Empty hint="The DAG is an agent output, not human code — it appears once proposals have been validated.">
          Not yet synthesized.
        </Empty>
      </Card>
    );
  }

  const levels: string[][] = [];
  const remaining = new Map(plan.nodes.map((n) => [n.name, new Set(n.depends_on)]));
  while (remaining.size > 0) {
    const ready = [...remaining.entries()]
      .filter(([, deps]) => deps.size === 0)
      .map(([name]) => name);
    if (ready.length === 0) break; // malformed; the backend rejects these
    levels.push(ready);
    ready.forEach((name) => remaining.delete(name));
    remaining.forEach((deps) => ready.forEach((name) => deps.delete(name)));
  }

  const statusOf = (node: string) => {
    const relevant = agents.filter((a) => a.node === node);
    if (relevant.some((a) => a.status === "passed")) return "passed";
    if (relevant.some((a) => a.status === "running")) return "running";
    if (relevant.some((a) => a.status === "failed")) return "failed";
    return "";
  };

  return (
    <Card
      title="Generated plan"
      meta={`${plan.hash} · w${plan.width} · d${plan.depth}`}
    >
      {plan.rationale && (
        <p style={{ margin: "0 0 12px", color: "var(--text-muted)" }}>
          {plan.rationale}
        </p>
      )}
      <div className="dag">
        {levels.map((level, i) => (
          <div key={i}>
            <div className="level">
              {level.map((name) => (
                <div
                  className={`node ${statusOf(name)}`}
                  key={name}
                  title={plan.nodes.find((n) => n.name === name)?.instruction ?? ""}
                >
                  {name}
                </div>
              ))}
            </div>
            {i < levels.length - 1 && <div className="arrow">↓</div>}
          </div>
        ))}
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------- agents */

export function AgentGrid({
  agents,
  selected,
  onSelect,
}: {
  agents: AgentState[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <Card title="Agents" meta={agents.length ? `${agents.length}` : undefined}>
      {agents.length === 0 ? (
        <Empty hint="One generic worker type; role, skill and budget are injected at runtime.">
          No agents yet.
        </Empty>
      ) : (
        <table>
          <thead>
            <tr>
              <th>agent</th>
              <th>node</th>
              <th>state</th>
              <th style={{ textAlign: "right" }}>credits</th>
              <th style={{ textAlign: "right" }}>try</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((agent) => (
              <tr
                key={agent.agent_id}
                aria-selected={selected === agent.agent_id}
                onClick={() => onSelect(agent.agent_id)}
              >
                <td className="mono">{agent.agent_id}</td>
                <td>{agent.node || "—"}</td>
                <td>
                  <span className={`pill ${agent.status}`}>{agent.status}</span>
                </td>
                <td className="num" style={{ textAlign: "right" }}>
                  {agent.credits_spent.toFixed(0)}
                </td>
                <td className="num" style={{ textAlign: "right" }}>
                  {agent.attempts || "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

export function ReasoningPanel({ agent }: { agent: AgentState | null }) {
  return (
    <Card title="Reasoning" meta={agent?.agent_id}>
      {!agent ? (
        <Empty hint="Thought, action, observation — in the order they happened.">
          Select an agent.
        </Empty>
      ) : agent.thoughts.length === 0 ? (
        <Empty>No thoughts recorded yet.</Empty>
      ) : (
        agent.thoughts.map((thought, i) => (
          <div className="thought" key={i}>
            <span className="decision">{thought.decision}</span>
            <span className="reasoning">{thought.reasoning}</span>
          </div>
        ))
      )}
    </Card>
  );
}

/* ---------------------------------------------------------------------- log */

const ALERT_KINDS = new Set(["containment", "run_failed", "node_error", "agent_killed"]);

export function EventLog({ events, tall }: { events: SwarmEvent[]; tall?: boolean }) {
  const recent = events.slice(-400).reverse();
  return (
    <Card title="Event stream" meta={events.length ? `${events.length}` : undefined} tall={tall}>
      {recent.length === 0 ? (
        <Empty hint="This panel renders the websocket stream. There is no fixture path.">
          Waiting for a run.
        </Empty>
      ) : (
        <div className="log">
          {recent.map((event) => (
            <div
              key={event.seq}
              className={`log-row ${event.kind === "thought" ? "thought" : ""} ${
                ALERT_KINDS.has(String(event.kind)) ? "alert" : ""
              }`}
            >
              <span className="seq">{String(event.seq).padStart(4, "0")}</span>
              <span className="kind">{event.kind}</span>
              <span className="detail">{summarise(event)}</span>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function summarise(event: SwarmEvent): string {
  const interesting = [
    "agent_id", "node", "hash", "status", "reason", "decision",
    "skill_id", "passed", "detail", "nodes",
  ];
  return interesting
    .filter((key) => event[key] !== undefined)
    .map((key) => `${key}=${JSON.stringify(event[key])}`)
    .join("  ")
    .slice(0, 200);
}

/* --------------------------------------------------------------------- cost */

export function CostPanel({ cost }: { cost: CostView | null }) {
  if (!cost) {
    return (
      <Card title="Cost">
        <Empty hint="Every figure is a sum over the append-only ledger, never a counter.">
          No completed run yet.
        </Empty>
      </Card>
    );
  }
  const fraction = Math.min(1, cost.total_usd / (cost.ceiling_usd || 1));
  const tone = fraction > 0.9 ? "bad" : fraction > 0.7 ? "warn" : "";

  return (
    <Card
      title="Cost"
      meta={cost.simulated ? undefined : `$${cost.ceiling_usd} ceiling`}
    >
      {cost.simulated && (
        <div style={{ marginBottom: 10 }}>
          <span className="pill killed">simulated · not evidence</span>
        </div>
      )}

      <div className={`meter ${tone}`}>
        <span style={{ width: `${fraction * 100}%` }} />
      </div>

      <dl className="kv" style={{ marginTop: 12 }}>
        <dt>spend</dt>
        <dd>${cost.total_usd.toFixed(6)}</dd>
        <dt>ceiling</dt>
        <dd>${cost.ceiling_usd}</dd>
        <dt>model calls</dt>
        <dd>{cost.llm_calls}</dd>
        <dt>cache hits</dt>
        <dd>
          {cost.cache_hits} ({(cost.cache_hit_rate * 100).toFixed(0)}%)
        </dd>
        <dt>saved by cache</dt>
        <dd>${cost.cache_savings_usd.toFixed(6)}</dd>
      </dl>

      {Object.keys(cost.by_provider).length > 0 && (
        <>
          <div className="rail-section" style={{ padding: "16px 0 4px" }}>
            By provider
          </div>
          <dl className="kv">
            {Object.entries(cost.by_provider).map(([name, value]) => (
              <div key={name} style={{ display: "contents" }}>
                <dt>{name}</dt>
                <dd>${value.toFixed(6)}</dd>
              </div>
            ))}
          </dl>
        </>
      )}
    </Card>
  );
}

/* ----------------------------------------------------------------- red team */

export function RedTeamPanel({
  containments,
  chaos,
}: {
  containments: SwarmEvent[];
  chaos: SwarmEvent[];
}) {
  const empty = containments.length === 0 && chaos.length === 0;
  return (
    <Card
      title="Red team & chaos"
      meta={containments.length ? `${containments.length} contained` : undefined}
    >
      {empty ? (
        <Empty hint="Detectors are pure code — the organ spends no provider quota.">
          Nothing contained. Chaos quiet.
        </Empty>
      ) : (
        <div className="log">
          {containments
            .slice(-30)
            .reverse()
            .map((event) => (
              <div className="log-row alert" key={`c-${event.seq}`}>
                <span className="seq">{String(event.seq).padStart(4, "0")}</span>
                <span className="kind">contained</span>
                <span className="detail">
                  {String(event.agent_id)} — {String(event.reason ?? "")}
                </span>
              </div>
            ))}
          {chaos
            .slice(-30)
            .reverse()
            .map((event) => (
              <div className="log-row" key={`k-${event.seq}`}>
                <span className="seq">{String(event.seq).padStart(4, "0")}</span>
                <span className="kind" style={{ color: "var(--amber)" }}>
                  {event.kind}
                </span>
                <span className="detail">{String(event.agent_id ?? "")}</span>
              </div>
            ))}
        </div>
      )}
    </Card>
  );
}

/* -------------------------------------------------------------------- stats */

export function SummaryPanel({
  economy,
  redteam,
  skills,
  ablation,
}: {
  economy: Record<string, number | null> | undefined;
  redteam: { contained: number; flagged: number } | undefined;
  skills: Record<string, number> | null | undefined;
  ablation: { skills_enabled: boolean } | undefined;
}) {
  if (!economy) {
    return (
      <Card title="Run summary">
        <Empty>No completed run yet.</Empty>
      </Card>
    );
  }
  return (
    <Card title="Run summary">
      <dl className="kv">
        <dt>population</dt>
        <dd>{economy.population ?? 0}</dd>
        <dt>alive at end</dt>
        <dd>{economy.alive ?? 0}</dd>
        <dt>verified successes</dt>
        <dd>{economy.successes ?? 0}</dd>
        <dt>bankruptcies</dt>
        <dd>{economy.bankruptcies ?? 0}</dd>
        <dt>contained</dt>
        <dd>{redteam?.contained ?? 0}</dd>
        <dt>flagged</dt>
        <dd>{redteam?.flagged ?? 0}</dd>
        {skills && (
          <>
            <dt>skills pending review</dt>
            <dd>{skills.pending ?? 0}</dd>
          </>
        )}
      </dl>
      <div style={{ marginTop: 12 }}>
        <span className={`pill ${ablation?.skills_enabled ? "neutral" : "running"}`}>
          {ablation?.skills_enabled ? "treatment arm" : "control arm"}
        </span>
      </div>
    </Card>
  );
}
