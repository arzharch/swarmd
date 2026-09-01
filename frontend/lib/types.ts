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

/**
 * One node's result, including the artifact it produced.
 *
 * The artifact is the point of the whole run and was the one thing the
 * dashboard never showed: it could tell you a node passed, what it cost and
 * which agent did it, but not what came out.
 */
export interface NodeOutcome {
  agent_id: string;
  node: string;
  passed: boolean;
  attempts: number;
  contained: boolean;
  skill_used: string;
  failures: string[];
  output_preview: string;
  artifacts: Record<string, string>;
}

export interface RunSummary {
  run_id: string;
  task: string;
  status: string;
  profile?: string;
  started?: number;
  finished?: number;
  report?: {
    run?: {
      integrity_hash?: string;
      /** What the run actually produced. The reason someone started it. */
      results?: NodeOutcome[];
    };
    cost: CostView;
    economy: Record<string, number | null>;
    redteam: { contained: number; flagged: number; by_pattern: Record<string, number> };
    redteam_audit: Array<Record<string, unknown>>;
    leaderboard: Array<Record<string, unknown>>;
    skills: Record<string, number> | null;
    ablation: { skills_enabled: boolean };
    /** Null when the run had no cache, which is not the same as a zero hit
     *  rate: one means the mechanism was absent, the other that it missed. */
    cache: CacheView | null;
    /** Null unless rogues were seeded for this run. */
    rogues: RogueReport | null;
    profile?: {
      name: string;
      agents: number;
      profile_agents: number;
      agents_explicit: boolean;
    };
  };
}

export interface CacheView {
  hits: number;
  misses: number;
  hit_rate: number;
  entries: number;
  evictions: number;
}

export interface RogueReport {
  requested: string[];
  seeded: string[];
  caught: string[];
  blocked_upstream: string[];
  /** pattern -> the detector that fired instead. A non-empty map fails the
   *  gate: the agent was stopped, but the detector under test was not. */
  misattributed: Record<string, string>;
  escaped: string[];
  unexercised: string[];
  passed: boolean;
}

export interface LedgerRow {
  run_id: string;
  seq: number;
  ts: number;
  kind: string;
  agent_id: string;
  stage: string;
  provider: string;
  model: string;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  would_have_cost: number;
  /** True when this row came from the simulated provider (ADR-012). */
  simulated: boolean;
  detail: Record<string, unknown>;
}

export interface LedgerResponse {
  run_id: string;
  rows: LedgerRow[];
  total: number;
  kinds: string[];
  /** Memory-vs-disk reconciliation. A mismatch means a torn write. */
  verify: {
    durable: boolean;
    rows_in_memory?: number;
    rows_on_disk?: number;
    cost_in_memory?: number;
    cost_on_disk?: number;
    match?: boolean;
    reason?: string;
  };
}

// "unauthorized" is separate from "closed" because the two need different
// words on screen and different actions from the operator: one reconnects on
// its own, the other never will until a token is supplied.
export type ConnectionState =
  | "connecting"
  | "open"
  | "closed"
  | "unauthorized";

/* --- service control ---------------------------------------------------- */

export interface JobSummary {
  job_id: string;
  kind: "run" | "eval" | "session";
  label: string;
  state: "queued" | "running" | "completed" | "failed" | "cancelled";
  submitted_ts: number;
  duration_s: number;
  /** Counts, not a percentage: 50% hides whether it is 1 of 2 or 500 of 1000. */
  done: number;
  total: number;
  error: string;
  params?: Record<string, unknown>;
  report?: Record<string, unknown>;
}

export interface ArmSummary {
  runs: number;
  solved: number;
  success_rate: number;
  cost_per_solved: number | null;
  first_pass_rate: number;
}

export interface EvalReport {
  total_runs: number;
  repeats: number;
  duration_s: number;
  simulated: boolean;
  arms: Record<
    string,
    {
      treatment: ArmSummary;
      control: ArmSummary;
      comparison: { verdict: string; note?: string; reason?: string };
    }
  >;
}

export interface ProviderRow {
  provider: string;
  tier: string;
  credential?: string;
  available?: boolean;
  ok?: boolean;
  reason?: string;
  rate_limits?: number;
  simulated?: boolean;
}

export interface ConfigResponse {
  adjustable: {
    default_profile: string;
    default_ceiling_usd: number;
    chaos_kill_rate: number;
    sandbox_timeout_s: number;
    sandbox_memory_mb: number;
    allow_paid: boolean;
  };
  fixed: {
    ceiling_max_usd: number;
    profiles: Record<
      string,
      { agents: number; target_calls: number; description: string }
    >;
    notes: string[];
  };
}

export interface PendingApproval {
  request_id: string;
  stage: string;
  item: Record<string, unknown>;
  waited_s: number;
}

export interface SkillsResponse {
  stats: { total: number; approved: number; pending: number; retired: number };
  pending: Array<Record<string, unknown>>;
  approved: Array<Record<string, unknown>>;
}

export interface BudgetWindow {
  window: string;
  used_requests: number;
  limit_requests: number | null;
  remaining_requests: number | null;
  fraction_used: number;
  resets_in_s: number;
  exhausted: boolean;
}

export interface BudgetGrant {
  total: number;
  used: number;
  remaining: number;
  fraction_used: number;
  expires_days: number | null;
  exhausted: boolean;
}

export interface ProviderBudget {
  provider: string;
  /** "rate" replenishes, "quota" resets on a schedule, "grant" never comes
   *  back. What running out MEANS differs by kind, so the badge shows it. */
  kind: string;
  source: string;
  checked: string;
  note: string;
  windows: BudgetWindow[];
  grant: BudgetGrant | null;
  blocked: string;
}

export interface BudgetPlan {
  sustainable_daily_requests: number;
  grant_backed_daily_requests: number;
  rate_extrapolated_upper_bound: number;
  week_requests: number;
  month_requests: number;
}

export interface BudgetResponse {
  providers: ProviderBudget[];
  plan: BudgetPlan;
}

/** What a run will cost against today's remaining budget, emitted before any
 *  work starts. The one moment an operator can still act on the number. */
export interface Preflight {
  agents: number;
  profile: string;
  estimated_calls: number;
  remaining_today: number;
  fits: boolean;
  shortfall: number;
  fraction_of_remaining: number | null;
  forecast?: Forecast;
}

/**
 * When this run's calls will actually be spent.
 *
 * A yes/no verdict was the right shape when running out meant failing. Now
 * that a run pauses and resumes, "does not fit today" covers both "finishes
 * this evening after one pause" and "spans three days", and only the second is
 * a reason not to press the button.
 */
export interface Forecast {
  verdict:
    | "fits_this_session"
    | "fits_today_with_pauses"
    | "spans_days"
    | "exceeds_horizon";
  estimated_calls: number;
  sessions_needed: number;
  expected_pauses: number;
  /** Unix seconds, or null when the run never has to stop. */
  first_pause_at: number | null;
  projected_finish: number | null;
  session_capacity: number;
}

/**
 * Why the run is waiting, carrying the numbers rather than a sentence so every
 * surface renders the same fact its own way. A pause reported as "waiting for
 * capacity" is a pause nobody can act on.
 */
export interface PauseView {
  reason: string;
  provider: string;
  credential: string;
  /** Which ceiling was reached: "requests" or "tokens". */
  dimension: string;
  used: number;
  envelope: number;
  /** Unix seconds. */
  resumes_at: number;
  resumes_in_s: number;
  human: string;
  waiting_agents?: number;
  checkpoint_path?: string;
  /** Set once the run has come back; seconds actually spent parked. */
  paused_for_s?: number;
}

/* --- parked runs -------------------------------------------------------- */

/**
 * A run on DISK that has not finished. Not the in-process registry: that is
 * emptied by a restart, and a run parked on a spent ration is exactly the run
 * most likely to outlive the process that started it.
 */
export type ResumableRun = {
  run_id: string;
  task: string;
  profile: string;
  agents: number;
  status: string;
  paused_reason: string;
  resumes_at: number;
  nodes_done: number;
  has_criterion: boolean;
  has_plan: boolean;
  /** This control plane is still working on it, so resuming would be a 409. */
  live: boolean;
};
