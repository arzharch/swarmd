/**
 * Event shapes emitted by the control plane.
 *
 * These mirror `SwarmRun._emit` in src/swarmd/swarm/run.py. There is no
 * generated client and no shared schema package: one small hand-written file
 * that the type checker enforces is cheaper to maintain than a codegen step,
 * and when the backend adds a field the compiler points at the exactly one
 * place that needs updating.
 */

export type EventKind =
  | "run_started"
  | "run_finished"
  | "run_failed"
  | "stage_started"
  | "criterion_proposal"
  | "criterion_frozen"
  | "plan_proposal"
  | "plan_selected"
  | "level_started"
  | "agent_spawned"
  | "agent_killed"
  | "agent_requeued"
  | "node_finished"
  | "node_error"
  | "thought"
  | "containment"
  | "skill_proposed";

export interface SwarmEvent {
  seq: number;
  run_id: string;
  kind: EventKind | string;
  [key: string]: unknown;
}

export interface Thought {
  seq: number;
  agent_id: string;
  decision: string;
  reasoning: string;
  tick: number;
}

export interface AgentState {
  agent_id: string;
  node: string;
  status: "spawned" | "running" | "passed" | "failed" | "killed" | "contained";
  credits_spent: number;
  attempts: number;
  skill_used: string;
  thoughts: Thought[];
}

export interface PlanNode {
  name: string;
  instruction: string;
  depends_on: string[];
  pool_size: number;
}

export interface PlanView {
  hash: string;
  rationale: string;
  width: number;
  depth: number;
  nodes: PlanNode[];
}

export interface CriterionCheck {
  kind: string;
  params: Record<string, unknown>;
}

export interface CriterionView {
  hash: string;
  attempts: number;
  agreement: number;
  attack: string;
  criterion: { description: string; hash: string; checks: CriterionCheck[] };
  history: string[];
}

export interface CostView {
  /**
   * True when ANY ledger row came from the simulated provider. Taint
   * propagates from row to report to here (ADR-012), which is why the banner
   * cannot be forgotten: it is data, not a UI flag someone must remember.
   */
  simulated: boolean;
  simulated_rows: number;
  total_usd: number;
  ceiling_usd: number;
  llm_calls: number;
  cache_hits: number;
  cache_hit_rate: number;
  cache_savings_usd: number;
  by_provider: Record<string, number>;
  by_stage: Record<string, number>;
}

export interface RunSummary {
  run_id: string;
  task: string;
  status: string;
  profile?: string;
  started?: number;
  finished?: number;
  report?: {
    cost: CostView;
    economy: Record<string, number | null>;
    redteam: { contained: number; flagged: number; by_pattern: Record<string, number> };
    redteam_audit: Array<Record<string, unknown>>;
    leaderboard: Array<Record<string, unknown>>;
    skills: Record<string, number> | null;
    ablation: { skills_enabled: boolean };
  };
}

export type ConnectionState = "connecting" | "open" | "closed";
