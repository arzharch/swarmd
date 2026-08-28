"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AgentGrid,
  CostPanel,
  EfficiencyPanel,
  CriterionPanel,
  EventLog,
  PlanPanel,
  ReasoningPanel,
  RedTeamPanel,
  RogueGatePanel,
  SummaryPanel,
} from "@/components/panels";
import {
  LedgerPanel,
  ObservabilityLinks,
  ProvenancePanel,
  ReasoningTape,
} from "@/components/trace";
import {
  EvalPanel,
  EvalReportPanel,
  HarnessPanel,
  BudgetPanel,
  ProviderPanel,
  ReviewPanel,
  SessionPanel,
} from "@/components/control";
import { Rail, TopBar, VIEWS, type ViewId } from "@/components/shell";
import { useRunStream } from "@/lib/useRunStream";
import type {
  CostView,
  JobSummary,
  LedgerResponse,
  RunSummary,
} from "@/lib/types";

export default function Dashboard() {
  const stream = useRunStream();
  const [view, setView] = useState<ViewId>("run");

  // The view lives in the URL fragment, so a view is a place you can link to.
  // Without it "look at the cost panel for run X" is a set of instructions
  // rather than a link, browser Back does nothing, and a reload always lands
  // on the live run -- which is the wrong view for anyone arriving to read a
  // finished one.
  //
  // The fragment rather than a path: this is a single-page dashboard whose
  // server route is the same for every view, and a fragment needs no router,
  // no server change, and survives the static export.
  useEffect(() => {
    const apply = () => {
      const id = window.location.hash.replace(/^#/, "");
      if (VIEWS.some((v) => v.id === id)) setView(id as ViewId);
    };
    apply();
    window.addEventListener("hashchange", apply);
    return () => window.removeEventListener("hashchange", apply);
  }, []);

  const selectView = useCallback((next: ViewId) => {
    setView(next);
    // replaceState, not a hash assignment: pushing an entry per click would
    // make Back walk through every panel someone glanced at.
    window.history.replaceState(null, "", `#${next}`);
  }, []);
  const [task, setTask] = useState(
    "extract every numeric claim from the supplied report and verify each one",
  );
  const [profile, setProfile] = useState("smoke");
  const [chaos, setChaos] = useState(true);
  const [useSkills, setUseSkills] = useState(true);
  // A string, not a number: an empty box means "let the profile decide", which
  // is a different request from asking for zero agents.
  const [agents, setAgents] = useState("");
  const [seedRogues, setSeedRogues] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<RunSummary | null>(null);

  // The whole-run report is written when the run finishes, so it is fetched
  // then rather than streamed — pushing an aggregate on every event would be a
  // lot of traffic to update a number that only changes at the end.
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

  // Fetched once per finished run so Provenance can report whether the
  // in-memory view reconciles with what actually reached disk.
  const [ledgerVerify, setLedgerVerify] = useState<
    LedgerResponse["verify"] | undefined
  >(undefined);
  useEffect(() => {
    if (!stream.activeRun || stream.runStatus === "running") return;
    let cancelled = false;
    fetch(`/api/runs/${stream.activeRun}/ledger?limit=1`)
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => {
        if (!cancelled && body) setLedgerVerify((body as LedgerResponse).verify);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [stream.activeRun, stream.runStatus]);

  const selectedAgent = useMemo(
    () => stream.agents.find((a) => a.agent_id === selected) ?? null,
    [stream.agents, selected],
  );

  // Show SOMEONE's reasoning rather than an empty third of the screen. The
  // first agent with recorded thoughts is picked, not simply the first agent:
  // an idle or killed agent has nothing to show, so defaulting to it would
  // trade an empty panel for a panel that looks broken.
  //
  // Only while nothing is selected, so this never fights the user's click, and
  // a selection that disappears between runs falls back rather than sticking.
  useEffect(() => {
    if (selected && stream.agents.some((a) => a.agent_id === selected)) return;
    const speaking = stream.agents.find((a) => a.thoughts.length > 0);
    if (speaking) setSelected(speaking.agent_id);
  }, [stream.agents, selected]);

  // Taint is read from the ledger, not from a UI setting. If any row in the
  // run was synthetic the banner shows, and there is no way to configure it
  // away (ADR-012).
  const simulated = cost?.simulated === true;

  const start = async () => {
    setSubmitting(true);
    setError(null);
    setSummary(null);
    try {
      await stream.submit(
        task,
        profile,
        chaos,
        useSkills,
        agents.trim() ? Number(agents) : null,
        seedRogues,
      );
      setView("run");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSubmitting(false);
    }
  };

  // Jobs are polled rather than derived from the stream: a session is hours
  // long and its progress events are coarse, so a periodic read is both
  // simpler and enough.
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetch("/api/jobs")
        .then((r) => (r.ok ? r.json() : null))
        .then((body) => {
          if (!cancelled && body) setJobs(body.jobs as JobSummary[]);
        })
        .catch(() => undefined);
    load();
    const timer = setInterval(load, 4000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const activeJobs = jobs.filter(
    (j) => j.state === "running" || j.state === "queued",
  ).length;

  const counts: Record<string, number> = {
    run: stream.agents.length,
    decisions: (stream.criterion ? 1 : 0) + (stream.plan ? 1 : 0),
    cost: stream.containments.length,
    trace: 0,
    evals: activeJobs,
    harness: 0,
  };

  return (
    <div className="shell">
      <Rail
        view={view}
        onView={selectView}
        counts={counts}
        runId={stream.activeRun}
        runStatus={stream.runStatus}
      />

      <div className="workspace">
        <TopBar
          task={task}
          onTask={setTask}
          profile={profile}
          onProfile={setProfile}
          chaos={chaos}
          onChaos={setChaos}
          useSkills={useSkills}
          agents={agents}
          onAgents={setAgents}
          seedRogues={seedRogues}
          onSeedRogues={setSeedRogues}
          onUseSkills={setUseSkills}
          onRun={start}
          submitting={submitting}
          connection={stream.connection}
          error={error}
        />

        <div>
          {simulated && (
            <div className="banner warn" role="status">
              <strong>Simulated run.</strong> Every response came from the
              synthetic provider. These numbers are not evidence of anything,
              and <code>swarmd eval</code> will refuse to report from them.
            </div>
          )}
          {seedRogues && (
            <div className="banner warn" role="status">
              <strong>Seeded run.</strong> Deliberate misbehaviour is being
              injected ({seedRogues === "all" ? "all five patterns" : seedRogues}).
              Containments below are expected. The red-team was not told which
              agents are seeded, and a rogue stopped by the wrong detector
              counts as a failure, not a catch.
            </div>
          )}
          {!useSkills && (
            <div className="banner info" role="status">
              <strong>Control arm.</strong> Skill retrieval is disabled and
              everything else is identical. This is the ablation an improvement
              claim is measured against.
            </div>
          )}
        </div>

        {view === "run" && (
          <div className="board cols-3">
            <AgentGrid
              agents={stream.agents}
              selected={selected}
              onSelect={setSelected}
            />
            <ReasoningPanel agent={selectedAgent} />
            <EventLog events={stream.events} />
          </div>
        )}

        {view === "decisions" && (
          <div className="board cols-2">
            <CriterionPanel criterion={stream.criterion} />
            <PlanPanel plan={stream.plan} agents={stream.agents} />
          </div>
        )}

        {view === "trace" && (
          <>
            <div className="board cols-2">
              <ProvenancePanel
                runId={stream.activeRun}
                criterion={stream.criterion}
                plan={stream.plan}
                integrityHash={summary?.report?.run?.integrity_hash}
                verify={ledgerVerify}
              />
              <ObservabilityLinks />
            </div>
            <div className="board cols-2" style={{ paddingTop: 0 }}>
              <LedgerPanel runId={stream.activeRun} />
              <ReasoningTape events={stream.events} />
            </div>
          </>
        )}

        {view === "evals" && (
          <>
            <div className="board cols-2">
              <EvalPanel jobs={jobs} />
              <SessionPanel jobs={jobs} />
            </div>
            <div className="board cols-2" style={{ paddingTop: 0 }}>
              <EvalReportPanel jobs={jobs} />
              <ReviewPanel />
            </div>
          </>
        )}

        {view === "harness" && (
          <div className="board cols-3">
            <HarnessPanel />
            <ProviderPanel />
            <BudgetPanel />
          </div>
        )}

        {view === "cost" && (
          <div className="board cols-3">
            <CostPanel cost={cost} />
            <EfficiencyPanel
              cache={summary?.report?.cache ?? null}
              batches={stream.batches}
              agents={summary?.report?.profile?.agents ?? 0}
            />
            <RogueGatePanel rogues={summary?.report?.rogues ?? null} />
            <SummaryPanel
              economy={summary?.report?.economy}
              redteam={summary?.report?.redteam}
              skills={summary?.report?.skills}
              ablation={summary?.report?.ablation}
            />
            <RedTeamPanel
              containments={stream.containments}
              chaos={stream.chaosEvents}
            />
          </div>
        )}
      </div>
    </div>
  );
}
