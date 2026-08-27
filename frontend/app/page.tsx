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
  SummaryPanel,
} from "@/components/panels";
import {
  LedgerPanel,
  ObservabilityLinks,
  ProvenancePanel,
  ReasoningTape,
} from "@/components/trace";
import { Rail, TopBar, type ViewId } from "@/components/shell";
import { useRunStream } from "@/lib/useRunStream";
import type { CostView, LedgerResponse, RunSummary } from "@/lib/types";

export default function Dashboard() {
  const stream = useRunStream();
  const [view, setView] = useState<ViewId>("run");
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

  // Taint is read from the ledger, not from a UI setting. If any row in the
  // run was synthetic the banner shows, and there is no way to configure it
  // away (ADR-012).
  const simulated = cost?.simulated === true;

  const start = async () => {
    setSubmitting(true);
    setError(null);
    setSummary(null);
    try {
      await stream.submit(task, profile, chaos, useSkills);
      setView("run");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSubmitting(false);
    }
  };

  const counts: Record<string, number> = {
    run: stream.agents.length,
    decisions: (stream.criterion ? 1 : 0) + (stream.plan ? 1 : 0),
    cost: stream.containments.length,
    trace: 0,
  };

  return (
    <div className="shell">
      <Rail
        view={view}
        onView={setView}
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

        {view === "cost" && (
          <div className="board cols-3">
            <CostPanel cost={cost} />
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
