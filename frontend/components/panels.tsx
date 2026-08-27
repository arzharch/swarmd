"use client";

import type {
  AgentState,
  CostView,
  CriterionView,
  PlanView,
  SwarmEvent,
} from "@/lib/types";

/**
 * Presentational panels. Every one of them renders props or an empty state.
 *
 * The empty states are not filler: with no run in progress the dashboard shows
 * "no run yet" rather than plausible-looking placeholder numbers. A screenshot
 * of a placeholder is indistinguishable from a screenshot of a result, which
 * is the whole reason ADR-006 exists.
 */

export function Panel({
  title,
  right,
  children,
}: {
  title: string;
  right?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="panel">
      <h2>
        <span>{title}</span>
        {right ? <span>{right}</span> : null}
      </h2>
      <div className="body">{children}</div>
    </section>
  );
}

export function CriterionPanel({ criterion }: { criterion: CriterionView | null }) {
  if (!criterion) {
    return (
      <Panel title="Criterion">
        <p className="empty">
          Not yet frozen. Nothing may be solved until success is defined and has
          survived an attack.
        </p>
      </Panel>
    );
  }
  return (
    <Panel title="Criterion" right={<code>{criterion.hash}</code>}>
      <p style={{ marginTop: 0 }}>{criterion.criterion.description}</p>
      <dl className="kv">
        <dt>attempts</dt>
        <dd>{criterion.attempts}</dd>
        <dt>agreement</dt>
        <dd>{(criterion.agreement * 100).toFixed(0)}%</dd>
      </dl>
      <p style={{ color: "var(--ok)", marginBottom: 4 }}>{criterion.attack}</p>
      {criterion.criterion.checks.map((check, i) => (
        <div className="check" key={i}>
          <code>{check.kind}</code>{" "}
          <span style={{ color: "var(--muted)" }}>
            {JSON.stringify(check.params)}
          </span>
        </div>
      ))}
      {criterion.history.length > 0 && (
        <details style={{ marginTop: 8, color: "var(--muted)" }}>
          <summary>synthesis history</summary>
          {criterion.history.map((line, i) => (
            <div key={i}>{line}</div>
          ))}
        </details>
      )}
    </Panel>
  );
}

export function PlanPanel({
  plan,
  agents,
}: {
  plan: PlanView | null;
  agents: AgentState[];
}) {
  if (!plan) {
    return (
      <Panel title="Plan">
        <p className="empty">Not yet synthesized.</p>
      </Panel>
    );
  }

  // Recompute levels client-side so the graph renders even if the backend
  // only sent the node list.
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
    <Panel
      title="Plan"
      right={
        <code>
          {plan.hash} · w{plan.width} · d{plan.depth}
        </code>
      }
    >
      <p style={{ marginTop: 0, color: "var(--muted)" }}>{plan.rationale}</p>
      <div className="dag">
        {levels.map((level, i) => (
          <div key={i}>
            <div className="level">
              {level.map((name) => (
                <div className={`node ${statusOf(name)}`} key={name} title={
                  plan.nodes.find((n) => n.name === name)?.instruction ?? ""
                }>
                  {name}
                </div>
              ))}
            </div>
            {i < levels.length - 1 && <div className="arrow">↓</div>}
          </div>
        ))}
      </div>
    </Panel>
  );
}

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
    <Panel title="Agents" right={<span>{agents.length}</span>}>
      {agents.length === 0 ? (
        <p className="empty">No agents yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>agent</th>
              <th>node</th>
              <th>state</th>
              <th>credits</th>
              <th>try</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((agent) => (
              <tr
                key={agent.agent_id}
                className={selected === agent.agent_id ? "selected" : ""}
                onClick={() => onSelect(agent.agent_id)}
              >
                <td>{agent.agent_id}</td>
                <td>{agent.node || "—"}</td>
                <td>
                  <span className={`tag ${agent.status}`}>{agent.status}</span>
                </td>
                <td>{agent.credits_spent.toFixed(0)}</td>
                <td>{agent.attempts || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

export function ReasoningPanel({ agent }: { agent: AgentState | null }) {
  return (
    <Panel
      title="Reasoning"
      right={agent ? <code>{agent.agent_id}</code> : undefined}
    >
      {!agent ? (
        <p className="empty">Select an agent to follow its reasoning.</p>
      ) : agent.thoughts.length === 0 ? (
        <p className="empty">No thoughts recorded yet.</p>
      ) : (
        agent.thoughts.map((thought, i) => (
          <div className="thought-line" key={i}>
            <span className="decision">{thought.decision}</span>
            <span className="reasoning">{thought.reasoning}</span>
          </div>
        ))
      )}
    </Panel>
  );
}

export function EventLog({ events }: { events: SwarmEvent[] }) {
  const recent = events.slice(-300).reverse();
  return (
    <Panel title="Events" right={<span>{events.length}</span>}>
      {recent.length === 0 ? (
        <p className="empty">Waiting for a run.</p>
      ) : (
        <div className="log">
          {recent.map((event) => (
            <div
              key={event.seq}
              className={event.kind === "thought" ? "thought" : ""}
            >
              <span className="seq">{String(event.seq).padStart(5, "0")}</span>
              <span className="kind">{event.kind}</span>
              <span style={{ color: "var(--muted)" }}>{summarise(event)}</span>
            </div>
          ))}
        </div>
      )}
    </Panel>
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
    .join(" ")
    .slice(0, 160);
}

export function CostPanel({ cost }: { cost: CostView | null }) {
  if (!cost) {
    return (
      <Panel title="Cost">
        <p className="empty">No completed run yet.</p>
      </Panel>
    );
  }
  const fraction = Math.min(1, cost.total_usd / (cost.ceiling_usd || 1));
  const tone = fraction > 0.9 ? "bad" : fraction > 0.7 ? "warn" : "";
  return (
    <Panel
      title="Cost"
      right={cost.simulated ? <span style={{ color: "var(--warn)" }}>SIMULATED</span> : undefined}
    >
      <div className={`meter ${tone}`}>
        <span style={{ width: `${fraction * 100}%` }} />
      </div>
      <dl className="kv" style={{ marginTop: 8 }}>
        <dt>spend</dt>
        <dd>
          ${cost.total_usd.toFixed(6)} / ${cost.ceiling_usd}
        </dd>
        <dt>calls</dt>
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
          <div style={{ color: "var(--muted)", marginTop: 8 }}>by provider</div>
          <dl className="kv">
            {Object.entries(cost.by_provider).map(([name, value]) => (
              <>
                <dt key={`${name}-k`}>{name}</dt>
                <dd key={`${name}-v`}>${value.toFixed(6)}</dd>
              </>
            ))}
          </dl>
        </>
      )}
    </Panel>
  );
}

export function RedTeamPanel({
  containments,
  chaos,
}: {
  containments: SwarmEvent[];
  chaos: SwarmEvent[];
}) {
  return (
    <Panel
      title="Red team & chaos"
      right={<span>{containments.length} contained</span>}
    >
      {containments.length === 0 && chaos.length === 0 ? (
        <p className="empty">Nothing contained. Chaos quiet.</p>
      ) : (
        <div className="log">
          {containments
            .slice(-40)
            .reverse()
            .map((event) => (
              <div key={`c-${event.seq}`}>
                <span className="kind" style={{ color: "var(--contained)" }}>
                  contained
                </span>
                <span>
                  {String(event.agent_id)} — {String(event.reason ?? "")}
                </span>
              </div>
            ))}
          {chaos
            .slice(-40)
            .reverse()
            .map((event) => (
              <div key={`k-${event.seq}`}>
                <span className="kind" style={{ color: "var(--warn)" }}>
                  {event.kind}
                </span>
                <span>{String(event.agent_id ?? "")}</span>
              </div>
            ))}
        </div>
      )}
    </Panel>
  );
}
