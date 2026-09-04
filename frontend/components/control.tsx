"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch, apiJson } from "@/lib/api";
import { Card } from "@/components/panels";
import type {
  BudgetResponse,
  ConfigResponse,
  EvalReport,
  JobSummary,
  PendingApproval,
  ProviderRow,
  ResumableRun,
  SkillsResponse,
} from "@/lib/types";

/**
 * Operating surfaces: evals, sessions, providers, harness config, review.
 *
 * All of this used to be CLI-only, which made the terminal the product and the
 * dashboard a viewer. Everything here calls the same endpoints the CLI does, so
 * the service is the thing that runs and this is how it is operated.
 */

// The operator token rides on every one of these. This was a bare `fetch`
// that sent none, so every panel below rendered its controls and then
// answered 401 the moment one was used.
const api = apiJson;

function useError() {
  const [error, setError] = useState<string | null>(null);
  const guard = useCallback(async (fn: () => Promise<void>) => {
    try {
      setError(null);
      await fn();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }, []);
  return { error, guard };
}

function ErrorLine({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <p style={{ color: "var(--red)", fontSize: 12, margin: "8px 0 0" }}>{error}</p>
  );
}

/* ------------------------------------------------------------------ evals */

export function EvalPanel({ jobs }: { jobs: JobSummary[] }) {
  const [arms, setArms] = useState("custom");
  const [repeats, setRepeats] = useState(3);
  const [profile, setProfile] = useState("smoke");
  const [holdout, setHoldout] = useState(false);
  const [busy, setBusy] = useState(false);
  const { error, guard } = useError();

  const start = () =>
    guard(async () => {
      setBusy(true);
      try {
        await api("/api/evals", {
          method: "POST",
          body: JSON.stringify({ arms, repeats, profile, holdout }),
        });
      } finally {
        setBusy(false);
      }
    });

  return (
    <Card title="Run an evaluation">
      <p style={{ margin: "0 0 12px", color: "var(--text-muted)" }}>
        Both arms always run. The harness refuses to emit an improvement figure
        without a paired control, so a treatment-only sweep would only produce a
        report that declines to conclude anything.
      </p>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <select value={arms} onChange={(e) => setArms(e.target.value)} aria-label="Arms">
          <option value="both">both arms</option>
          <option value="public">public only</option>
          <option value="custom">custom only</option>
        </select>

        <select
          value={String(repeats)}
          onChange={(e) => setRepeats(Number(e.target.value))}
          aria-label="Repeats"
        >
          {[1, 3, 5, 10].map((n) => (
            <option key={n} value={n}>
              {n} repeat{n > 1 ? "s" : ""}
            </option>
          ))}
        </select>

        <select
          value={profile}
          onChange={(e) => setProfile(e.target.value)}
          aria-label="Profile"
        >
          <option value="smoke">smoke</option>
          <option value="standard">standard</option>
          <option value="deep">deep</option>
        </select>

        <label
          className="toggle"
          title="The held-out set is reserved for acceptance. Opt-in so a routine eval cannot consume it."
        >
          <input
            type="checkbox"
            checked={holdout}
            onChange={(e) => setHoldout(e.target.checked)}
          />
          Holdout
        </label>

        <button className="primary" onClick={start} disabled={busy}>
          {busy ? "Starting…" : "Start eval"}
        </button>
      </div>

      <ErrorLine error={error} />

      {repeats * (arms === "both" ? 10 : 5) * 2 > 40 && (
        <p style={{ color: "var(--amber)", fontSize: 12, marginBottom: 0 }}>
          ≈{repeats * (arms === "both" ? 10 : 5) * 2} runs. At ~45 requests a
          minute this is a long sweep — see the capacity plan.
        </p>
      )}

      <JobList jobs={jobs.filter((j) => j.kind === "eval")} empty="No evals yet." />
    </Card>
  );
}

export function SessionPanel({ jobs }: { jobs: JobSummary[] }) {
  const [tasks, setTasks] = useState(10);
  const [useSkills, setUseSkills] = useState(true);
  const [autoApprove, setAutoApprove] = useState(false);
  const [busy, setBusy] = useState(false);
  const { error, guard } = useError();

  const start = () =>
    guard(async () => {
      setBusy(true);
      try {
        await api("/api/sessions", {
          method: "POST",
          body: JSON.stringify({
            tasks,
            profile: "smoke",
            use_skills: useSkills,
            auto_approve: autoApprove,
          }),
        });
      } finally {
        setBusy(false);
      }
    });

  return (
    <Card title="Run a session">
      <p style={{ margin: "0 0 12px", color: "var(--text-muted)" }}>
        Many tasks with consolidation and curriculum between them. This is where
        learning happens — a single run retrieves but cannot add.
      </p>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <select
          value={String(tasks)}
          onChange={(e) => setTasks(Number(e.target.value))}
          aria-label="Tasks"
        >
          {[5, 10, 20, 40].map((n) => (
            <option key={n} value={n}>
              {n} tasks
            </option>
          ))}
        </select>

        <label className="toggle" title="Unchecking runs the control arm.">
          <input
            type="checkbox"
            checked={useSkills}
            onChange={(e) => setUseSkills(e.target.checked)}
          />
          Skills
        </label>

        <label
          className="toggle"
          title="DEVELOPMENT ONLY. Skills enter the library with no human review. Every auto-approval is recorded with actor 'auto-approve' so the bypass stays visible."
        >
          <input
            type="checkbox"
            checked={autoApprove}
            onChange={(e) => setAutoApprove(e.target.checked)}
          />
          Auto-approve
        </label>

        <button className="primary" onClick={start} disabled={busy}>
          {busy ? "Starting…" : "Start session"}
        </button>
      </div>

      {autoApprove && (
        <p style={{ color: "var(--amber)", fontSize: 12, margin: "10px 0 0" }}>
          The human gate is what stops the library poisoning itself. Every
          bypass is recorded in the audit trail.
        </p>
      )}

      <ErrorLine error={error} />
      <JobList
        jobs={jobs.filter((j) => j.kind === "session")}
        empty="No sessions yet."
      />
    </Card>
  );
}

function JobList({ jobs, empty }: { jobs: JobSummary[]; empty: string }) {
  if (jobs.length === 0) {
    return <p className="empty">{empty}</p>;
  }
  return (
    <table style={{ marginTop: 12 }}>
      <thead>
        <tr>
          <th>job</th>
          <th>state</th>
          <th>progress</th>
          <th style={{ textAlign: "right" }}>duration</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {jobs.slice(0, 12).map((job) => (
          <tr key={job.job_id}>
            <td className="mono" title={job.label}>
              {job.label.slice(0, 32)}
            </td>
            <td>
              <span className={`pill ${jobTone(job.state)}`}>{job.state}</span>
            </td>
            <td className="num">
              {job.total ? `${job.done}/${job.total}` : job.done || "—"}
            </td>
            <td className="num" style={{ textAlign: "right" }}>
              {job.duration_s.toFixed(1)}s
            </td>
            <td style={{ textAlign: "right" }}>
              {job.state === "running" || job.state === "queued" ? (
                <button
                  className="ghost"
                  style={{ height: 24, fontSize: 11, padding: "0 8px" }}
                  onClick={() =>
                    apiFetch(`/api/jobs/${job.job_id}`, { method: "DELETE" })
                  }
                >
                  cancel
                </button>
              ) : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function jobTone(state: string): string {
  if (state === "completed") return "passed";
  if (state === "running" || state === "queued") return "running";
  if (state === "failed") return "failed";
  return "killed";
}

/* ------------------------------------------------------------ eval report */

export function EvalReportPanel({ jobs }: { jobs: JobSummary[] }) {
  const [report, setReport] = useState<EvalReport | null>(null);
  const latest = jobs.find((j) => j.kind === "eval" && j.state === "completed");

  useEffect(() => {
    if (!latest) return;
    let cancelled = false;
    apiFetch(`/api/jobs/${latest.job_id}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => {
        if (!cancelled && body?.report) setReport(body.report as EvalReport);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [latest?.job_id]);

  if (!report) {
    return (
      <Card title="Latest eval result">
        <p className="empty">
          No completed eval.
          <span className="hint">
            The report is what the project is judged on; everything else exists
            to make these numbers mean something.
          </span>
        </p>
      </Card>
    );
  }

  return (
    <Card title="Latest eval result" meta={`${report.total_runs} runs`}>
      {report.simulated && (
        <div style={{ marginBottom: 12 }}>
          <span className="pill killed">simulated · not evidence</span>
        </div>
      )}
      {Object.entries(report.arms).map(([arm, entry]) => (
        <div key={arm} style={{ marginBottom: 20 }}>
          <div className="rail-section" style={{ padding: "0 0 8px" }}>
            {arm} arm
          </div>
          <table>
            <thead>
              <tr>
                <th />
                <th style={{ textAlign: "right" }}>treatment</th>
                <th style={{ textAlign: "right" }}>control</th>
              </tr>
            </thead>
            <tbody>
              <MetricRow
                label="success rate"
                a={pct(entry.treatment.success_rate)}
                b={pct(entry.control.success_rate)}
              />
              <MetricRow
                label="solved"
                a={`${entry.treatment.solved}/${entry.treatment.runs}`}
                b={`${entry.control.solved}/${entry.control.runs}`}
              />
              <MetricRow
                label="$/solved"
                a={usd(entry.treatment.cost_per_solved)}
                b={usd(entry.control.cost_per_solved)}
              />
              <MetricRow
                label="first-pass"
                a={pct(entry.treatment.first_pass_rate)}
                b={pct(entry.control.first_pass_rate)}
              />
            </tbody>
          </table>
          <div style={{ marginTop: 10 }}>
            <span className={`pill ${verdictTone(entry.comparison.verdict)}`}>
              {entry.comparison.verdict}
            </span>
          </div>
          {entry.comparison.note && (
            <p style={{ color: "var(--text-soft)", fontSize: 12, margin: "8px 0 0" }}>
              {entry.comparison.note}
            </p>
          )}
        </div>
      ))}
    </Card>
  );
}

function MetricRow({ label, a, b }: { label: string; a: string; b: string }) {
  return (
    <tr>
      <td>{label}</td>
      <td className="num" style={{ textAlign: "right" }}>
        {a}
      </td>
      <td className="num" style={{ textAlign: "right" }}>
        {b}
      </td>
    </tr>
  );
}

function verdictTone(verdict: string): string {
  if (verdict === "improvement") return "passed";
  if (verdict === "regression") return "failed";
  return "neutral";
}

const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(0)}%`;
const usd = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `$${v.toFixed(6)}`;

/* -------------------------------------------------------------- providers */

export function ProviderPanel() {
  const [rows, setRows] = useState<ProviderRow[] | null>(null);
  const [probing, setProbing] = useState(false);
  const { error, guard } = useError();

  const load = useCallback(
    () =>
      guard(async () => {
        const body = await api<{ providers: ProviderRow[] }>("/api/providers");
        setRows(body.providers);
      }),
    [guard],
  );

  useEffect(() => {
    load();
  }, [load]);

  const probe = () =>
    guard(async () => {
      setProbing(true);
      try {
        const body = await api<{ providers: ProviderRow[] }>(
          "/api/providers/probe",
          { method: "POST" },
        );
        setRows(body.providers);
      } finally {
        setProbing(false);
      }
    });

  return (
    <Card
      title="Providers"
      meta={rows ? `${rows.length}` : undefined}
    >
      <p style={{ margin: "0 0 12px", color: "var(--text-muted)" }}>
        Published free-tier limits disagree across sources and change without
        notice, so probing replaces documentation with observation.
      </p>

      <button className="ghost" onClick={probe} disabled={probing}>
        {probing ? "Probing…" : "Probe now"}
      </button>

      <ErrorLine error={error} />

      {!rows ? (
        <p className="empty">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="empty">
          No providers configured.
          <span className="hint">
            Add a key to .env, or set SWARMD_SIMULATED_PROVIDER=true.
          </span>
        </p>
      ) : (
        <table style={{ marginTop: 12 }}>
          <thead>
            <tr>
              <th>provider</th>
              <th>tier</th>
              <th>state</th>
              <th style={{ textAlign: "right" }}>429s</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.provider}-${row.credential ?? ""}`}>
                <td className="mono">{row.provider}</td>
                <td>{row.tier}</td>
                <td>
                  <span
                    className={`pill ${
                      row.ok ?? row.available ? "passed" : "failed"
                    }`}
                  >
                    {row.ok ?? row.available ? "ok" : (row.reason ?? "backed off")}
                  </span>
                </td>
                <td className="num" style={{ textAlign: "right" }}>
                  {row.rate_limits ?? 0}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

/* ----------------------------------------------------------- parked runs */

/**
 * Runs that stopped without finishing, and the button that picks them back up.
 *
 * This was the last thing only the terminal could do. A run that spends the
 * day's ration parks rather than fails -- correct behaviour, and the whole
 * point of the pacer -- but from the dashboard it simply stopped, and the only
 * way to continue it was `swarmd runs resume` in a shell. An operator watching
 * this screen could see the pause and could do nothing about it.
 *
 * Resuming buys nothing the run already paid for: the criterion, the plan and
 * the batch drafts come off disk. Re-deriving the criterion would grade the
 * second half of a run against a target the first half never saw.
 */
export function ParkedPanel() {
  const [runs, setRuns] = useState<ResumableRun[] | null>(null);
  const [busy, setBusy] = useState("");
  const { error, guard } = useError();

  const load = useCallback(
    () =>
      guard(async () => {
        const body = await api<{ runs: ResumableRun[] }>("/api/runs/resumable");
        setRuns(body.runs);
      }),
    [guard],
  );

  useEffect(() => {
    load();
  }, [load]);

  const resume = (runId: string) =>
    guard(async () => {
      setBusy(runId);
      try {
        // The idempotency key makes a double click one resume rather than a
        // 409 the operator has to interpret.
        await api(`/api/runs/${runId}/resume`, {
          method: "POST",
          headers: { "Idempotency-Key": `dash-resume-${runId}` },
        });
        await load();
      } finally {
        setBusy("");
      }
    });

  return (
    <Card title="Parked runs" meta={runs ? `${runs.length}` : undefined}>
      <p style={{ margin: "0 0 12px", color: "var(--text-muted)" }}>
        Unfinished runs on disk. A run that exhausts the day&apos;s ration
        parks here instead of failing, and resuming reuses its frozen criterion
        and plan rather than paying for them twice.
      </p>

      <ErrorLine error={error} />

      {!runs ? (
        <p className="empty">Loading…</p>
      ) : runs.length === 0 ? (
        <p className="empty">
          Nothing parked.
          <span className="hint">Every stored run reached a terminal state.</span>
        </p>
      ) : (
        <table style={{ marginTop: 12 }}>
          <thead>
            <tr>
              <th>run</th>
              <th>task</th>
              <th>why it stopped</th>
              <th style={{ textAlign: "right" }}>nodes</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.run_id}>
                <td className="mono">{run.run_id}</td>
                <td title={run.task}>{run.task.slice(0, 48)}</td>
                <td>
                  <span className={`pill ${run.live ? "running" : "neutral"}`}>
                    {run.live
                      ? "running here"
                      : run.paused_reason || run.status}
                  </span>
                  {run.resumes_at > 0 && (
                    <span className="hint">
                      {" "}
                      capacity back{" "}
                      {new Date(run.resumes_at * 1000).toLocaleTimeString()}
                    </span>
                  )}
                </td>
                <td className="num" style={{ textAlign: "right" }}>
                  {run.nodes_done}
                </td>
                <td style={{ textAlign: "right" }}>
                  <button
                    className="ghost"
                    onClick={() => resume(run.run_id)}
                    disabled={busy === run.run_id || run.live}
                    title={
                      run.live
                        ? "Still running on this control plane. Resuming it would be refused: two runs sharing an id would interleave their writes into one ledger."
                        : run.has_criterion && run.has_plan
                          ? "Continues from the stored criterion and plan."
                          : "This run parked before it had both a criterion and a plan; resuming re-derives what is missing."
                    }
                  >
                    {busy === run.run_id ? "Resuming…" : "Resume"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ config */

export function HarnessPanel() {
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const { error, guard } = useError();

  const load = useCallback(
    () =>
      guard(async () => setConfig(await api<ConfigResponse>("/api/config"))),
    [guard],
  );

  useEffect(() => {
    load();
  }, [load]);

  const patch = (body: Record<string, unknown>) =>
    guard(async () => {
      const result = await api<{ config: ConfigResponse }>("/api/config", {
        method: "PATCH",
        body: JSON.stringify(body),
      });
      setConfig(result.config);
    });

  if (!config) {
    return (
      <Card title="Harness">
        <p className="empty">Loading…</p>
      </Card>
    );
  }

  const a = config.adjustable;
  return (
    <Card title="Harness">
      <div className="rail-section" style={{ padding: "0 0 8px" }}>
        Adjustable
      </div>

      <div style={{ display: "grid", gap: 10 }}>
        <Knob label="Chaos kill rate" hint="Probability an agent is killed per tick.">
          <select
            value={String(a.chaos_kill_rate)}
            onChange={(e) => patch({ chaos_kill_rate: Number(e.target.value) })}
          >
            {[0, 0.1, 0.2, 0.3, 0.5, 0.9].map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </Knob>

        <Knob
          label="Sandbox timeout"
          hint="Wall clock before the process tree is killed."
        >
          <select
            value={String(a.sandbox_timeout_s)}
            onChange={(e) => patch({ sandbox_timeout_s: Number(e.target.value) })}
          >
            {[10, 15, 30, 60, 120].map((v) => (
              <option key={v} value={v}>
                {v}s
              </option>
            ))}
          </select>
        </Knob>

        <Knob label="Sandbox memory" hint="Address-space cap per execution.">
          <select
            value={String(a.sandbox_memory_mb)}
            onChange={(e) => patch({ sandbox_memory_mb: Number(e.target.value) })}
          >
            {[128, 256, 512, 1024, 2048].map((v) => (
              <option key={v} value={v}>
                {v} MB
              </option>
            ))}
          </select>
        </Knob>

        <Knob
          label="Default ceiling"
          hint={`Hard USD limit per run. Cannot exceed $${config.fixed.ceiling_max_usd}.`}
        >
          <select
            value={String(a.default_ceiling_usd)}
            onChange={(e) =>
              patch({ default_ceiling_usd: Number(e.target.value) })
            }
          >
            {[0.01, 0.05, 0.1, 0.25].map((v) => (
              <option key={v} value={v}>
                ${v}
              </option>
            ))}
          </select>
        </Knob>

        <Knob
          label="Paid overflow"
          hint="Off means exhausting free capacity stops the run rather than spending."
        >
          <label className="toggle">
            <input
              type="checkbox"
              checked={a.allow_paid}
              onChange={(e) => patch({ allow_paid: e.target.checked })}
            />
            {a.allow_paid ? "enabled" : "disabled"}
          </label>
        </Knob>
      </div>

      <ErrorLine error={error} />

      <div className="rail-section" style={{ padding: "20px 0 8px" }}>
        Fixed
      </div>
      {config.fixed.notes.map((note, i) => (
        <p
          key={i}
          style={{ color: "var(--text-soft)", fontSize: 12, margin: "0 0 8px" }}
        >
          {note}
        </p>
      ))}

      <table style={{ marginTop: 8 }}>
        <thead>
          <tr>
            <th>profile</th>
            <th style={{ textAlign: "right" }}>agents</th>
            <th style={{ textAlign: "right" }}>calls</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(config.fixed.profiles).map(([name, p]) => (
            <tr key={name}>
              <td className="mono">{name}</td>
              <td className="num" style={{ textAlign: "right" }}>
                {p.agents}
              </td>
              <td className="num" style={{ textAlign: "right" }}>
                {p.target_calls}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function Knob({
  label,
  hint,
  children,
}: {
  label: string;
  hint: string;
  children: React.ReactNode;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
      }}
    >
      <div>
        <div style={{ color: "var(--text-strong)" }}>{label}</div>
        <div style={{ color: "var(--text-soft)", fontSize: 12 }}>{hint}</div>
      </div>
      {children}
    </div>
  );
}

/* ------------------------------------------------------------------ review */

export function ReviewPanel() {
  const [approvals, setApprovals] = useState<PendingApproval[] | null>(null);
  const [skills, setSkills] = useState<SkillsResponse | null>(null);
  const { error, guard } = useError();

  const load = useCallback(
    () =>
      guard(async () => {
        const [a, s] = await Promise.all([
          api<{ pending: PendingApproval[] }>("/api/approvals"),
          api<SkillsResponse>("/api/skills"),
        ]);
        setApprovals(a.pending);
        setSkills(s);
      }),
    [guard],
  );

  useEffect(() => {
    load();
    const timer = setInterval(load, 10_000);
    return () => clearInterval(timer);
  }, [load]);

  const decide = (requestId: string, action: "approve" | "reject") =>
    guard(async () => {
      await api(`/api/approvals/${requestId}/${action}`, { method: "POST" });
      await load();
    });

  return (
    <Card title="Awaiting review" meta={approvals ? `${approvals.length}` : undefined}>
      <p style={{ margin: "0 0 12px", color: "var(--text-muted)" }}>
        A skill approved here is inherited by every future run. This is the most
        consequential decision in the system and the least obvious one.
      </p>

      <ErrorLine error={error} />

      {!approvals ? (
        <p className="empty">Loading…</p>
      ) : approvals.length === 0 ? (
        <p className="empty">
          Nothing waiting.
          {skills && (
            <span className="hint">
              Library: {skills.stats.approved} approved,{" "}
              {skills.stats.pending} pending, {skills.stats.retired} retired.
            </span>
          )}
        </p>
      ) : (
        approvals.map((item) => (
          <div
            key={item.request_id}
            style={{
              padding: "12px 0",
              borderBottom: "1px solid var(--border-soft)",
            }}
          >
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                gap: 12,
                alignItems: "baseline",
              }}
            >
              <span className="pill neutral">{item.stage}</span>
              <span className="mono" style={{ fontSize: 12, color: "var(--text-soft)" }}>
                waiting {Math.round(item.waited_s)}s
              </span>
            </div>

            <div style={{ margin: "8px 0", color: "var(--text-strong)" }}>
              {String(item.item.name ?? item.item.subject ?? item.request_id)}
            </div>

            {item.item.instruction ? (
              <pre
                className="mono"
                style={{
                  margin: "0 0 8px",
                  fontSize: 12,
                  whiteSpace: "pre-wrap",
                  color: "var(--text-muted)",
                  maxHeight: 120,
                  overflow: "auto",
                }}
              >
                {String(item.item.instruction)}
              </pre>
            ) : null}

            {item.item.verified_successes ? (
              <div style={{ fontSize: 12, color: "var(--text-soft)", marginBottom: 8 }}>
                evidence: {String(item.item.verified_successes)} verified
                successes · run {String(item.item.provenance_run ?? "?")} ·
                criterion {String(item.item.provenance_criterion ?? "?")}
              </div>
            ) : null}

            <div style={{ display: "flex", gap: 8 }}>
              <button
                className="primary"
                style={{ height: 28, fontSize: 12 }}
                onClick={() => decide(item.request_id, "approve")}
              >
                Approve
              </button>
              <button
                className="ghost"
                style={{ height: 28, fontSize: 12 }}
                onClick={() => decide(item.request_id, "reject")}
              >
                Reject
              </button>
            </div>
          </div>
        ))
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ budget */

export function BudgetPanel() {
  const [data, setData] = useState<BudgetResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    apiFetch("/api/providers/budget")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then(setData)
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    load();
    // Windows as short as a minute go stale while you look at them, and a
    // budget page showing a stale number is worse than one showing none: it
    // is the number an operator decides to start a run on.
    const timer = setInterval(load, 15_000);
    return () => clearInterval(timer);
  }, [load]);

  if (error) {
    return (
      <Card title="Provider budgets">
        <p className="empty">Could not read budgets. {error}</p>
      </Card>
    );
  }
  if (!data) {
    return (
      <Card title="Provider budgets">
        <p className="empty">Loading…</p>
      </Card>
    );
  }

  const { plan } = data;

  return (
    <Card title="Provider budgets" meta={`${data.providers.length} providers`}>
      <dl className="kv">
        <dt>plannable / day</dt>
        <dd>{plan.sustainable_daily_requests.toLocaleString()} requests</dd>
        <dt>for a week</dt>
        <dd>{plan.week_requests.toLocaleString()}</dd>
        <dt>for a month</dt>
        <dd>{plan.month_requests.toLocaleString()}</dd>
        <dt>one-off grants</dt>
        <dd>{plan.grant_backed_daily_requests.toLocaleString()} / day</dd>
      </dl>
      <p className="hint">
        Only published <strong>daily</strong> allowances are counted as
        plannable. Grants stop when spent; a per-minute rate multiplied out to
        a day assumes 24 hours of perfect saturation and is not a plan.
      </p>

      {data.providers.map((provider) => (
        <div key={provider.provider} style={{ marginTop: 14 }}>
          <div className="budget-provider">
            {provider.provider}
            <span className="pill neutral" style={{ marginLeft: 8 }}>
              {provider.kind}
            </span>
            {provider.blocked ? (
              <span className="pill failed" style={{ marginLeft: 6 }}>
                {provider.blocked}
              </span>
            ) : null}
          </div>

          {provider.grant ? (
            <div style={{ marginBottom: 6 }}>
              <div className={`meter ${provider.grant.fraction_used > 0.8 ? "bad" : ""}`}>
                <span style={{ width: `${provider.grant.fraction_used * 100}%` }} />
              </div>
              <span className="hint">
                grant {provider.grant.remaining.toLocaleString()} of{" "}
                {provider.grant.total.toLocaleString()} left
                {provider.grant.expires_days
                  ? `, expires in ${provider.grant.expires_days}d`
                  : null}
              </span>
            </div>
          ) : null}

          {provider.windows.map((w) => (
            <div key={w.window} style={{ marginBottom: 4 }}>
              <div
                className={`meter ${
                  w.fraction_used > 0.9 ? "bad" : w.fraction_used > 0.7 ? "warn" : ""
                }`}
              >
                <span style={{ width: `${Math.min(1, w.fraction_used) * 100}%` }} />
              </div>
              <span className="hint">
                {w.window} · {w.used_requests.toLocaleString()}
                {w.limit_requests ? ` / ${w.limit_requests.toLocaleString()}` : ""} ·
                resets in {formatDuration(w.resets_in_s)}
              </span>
            </div>
          ))}
        </div>
      ))}
    </Card>
  );
}

function formatDuration(seconds: number): string {
  if (seconds >= 86400) return `${(seconds / 86400).toFixed(1)}d`;
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)}h`;
  if (seconds >= 60) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds)}s`;
}

