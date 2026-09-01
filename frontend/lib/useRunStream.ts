"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, onTokenChange, streamUrl } from "./api";
import type {
  Preflight,
  AgentState,
  ConnectionState,
  CriterionView,
  PauseView,
  PlanView,
  SwarmEvent,
  Thought,
} from "./types";

/**
 * The dashboard's only data source.
 *
 * THERE IS NO FIXTURE PATH IN THIS FILE, and there is not one anywhere else in
 * the app. The page renders the websocket stream or it renders an empty state
 * (ADR-006). A CI check fails the build on sample-data imports, because a
 * dashboard fed by fixtures is pixel-identical to one fed by a real run, and
 * that is exactly how a demo ends up lying.
 *
 * Reconnection is deliberate rather than automatic-and-silent: a viewer must
 * be able to tell "the run is quiet" from "my connection died". `state` is
 * surfaced so the UI can say which.
 */

// Cap on retained events. A `deep` profile emits tens of thousands, and an
// unbounded array in a browser tab left open for 40 minutes is a memory leak
// with a nice chart on top.
const MAX_EVENTS = 4000;
const MAX_THOUGHTS_PER_AGENT = 60;

// Reconnect backoff. Starts fast because the common case is a control-plane
// rolling deploy that finishes in seconds; caps at 10s so a genuinely down
// backend is not hammered by every open tab.
const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 10_000;

interface StreamState {
  events: SwarmEvent[];
  agents: Map<string, AgentState>;
  criterion: CriterionView | null;
  plan: PlanView | null;
  activeRun: string | null;
  runStatus: string | null;
  skillsProposed: string[];
  containments: SwarmEvent[];
  chaosEvents: SwarmEvent[];
  /** One entry per batched generation: the requests a pool did not make. */
  batches: SwarmEvent[];
  /** Cost of this run against the remaining budget, known before it starts. */
  preflight: Preflight | null;
  /**
   * Set while the run is parked on a spent provider ration.
   *
   * A paused run and a hung one look identical from here -- no events, no
   * errors, nothing finishing -- so without this the dashboard's honest
   * rendering of a working run is a blank screen for hours.
   */
  pause: PauseView | null;
  /** Pauses this run has already come back from, newest last. */
  pauseHistory: PauseView[];
}

const EMPTY: StreamState = {
  events: [],
  agents: new Map(),
  criterion: null,
  plan: null,
  activeRun: null,
  runStatus: null,
  skillsProposed: [],
  containments: [],
  chaosEvents: [],
  batches: [],
  preflight: null,
  pause: null,
  pauseHistory: [],
};

function reduce(state: StreamState, event: SwarmEvent): StreamState {
  const agents = new Map(state.agents);
  const next: StreamState = {
    ...state,
    events: [...state.events, event].slice(-MAX_EVENTS),
    agents,
  };

  const upsert = (id: string, patch: Partial<AgentState>) => {
    const existing = agents.get(id) ?? {
      agent_id: id,
      node: "",
      status: "spawned" as const,
      credits_spent: 0,
      attempts: 0,
      skill_used: "",
      thoughts: [],
    };
    agents.set(id, { ...existing, ...patch });
  };

  switch (event.kind) {
    case "run_started":
      // A new run resets the view. Keeping the previous run's agents would
      // show a grid that is half stale and give no indication which half.
      return {
        ...EMPTY,
        events: next.events,
        activeRun: String(event.run_id),
        runStatus: "running",
      };

    case "criterion_frozen":
      next.criterion = event as unknown as CriterionView;
      break;

    case "plan_selected":
      next.plan = event as unknown as PlanView;
      break;

    case "agent_spawned":
      upsert(String(event.agent_id), {
        node: String(event.node ?? ""),
        status: "running",
      });
      break;

    case "agent_killed":
      upsert(String(event.agent_id), { status: "killed" });
      next.chaosEvents = [...state.chaosEvents, event].slice(-200);
      break;

    case "agent_requeued":
      next.chaosEvents = [...state.chaosEvents, event].slice(-200);
      break;

    case "preflight":
      next.preflight = event as unknown as Preflight;
      break;

    case "batch_generated":
      next.batches = [...state.batches, event].slice(-200);
      break;

    case "containment":
      upsert(String(event.agent_id), { status: "contained" });
      next.containments = [...state.containments, event].slice(-200);
      break;

    case "thought": {
      const id = String(event.agent_id);
      const existing = agents.get(id);
      const thought: Thought = {
        seq: event.seq,
        agent_id: id,
        decision: String(event.decision ?? ""),
        reasoning: String(event.reasoning ?? ""),
        tick: Number(event.tick ?? 0),
      };
      upsert(id, {
        thoughts: [...(existing?.thoughts ?? []), thought].slice(
          -MAX_THOUGHTS_PER_AGENT,
        ),
      });
      break;
    }

    case "node_finished":
      upsert(String(event.agent_id), {
        node: String(event.node ?? ""),
        status: event.contained
          ? "contained"
          : event.passed
            ? "passed"
            : "failed",
        credits_spent: Number(event.credits_spent ?? 0),
        attempts: Number(event.attempts ?? 0),
        skill_used: String(event.skill_used ?? ""),
      });
      break;

    case "skill_proposed":
      next.skillsProposed = [
        ...state.skillsProposed,
        String(event.skill_id),
      ];
      break;

    // The pause. Every one of these carries the numbers, so the banner can say
    // "groq, 225 of 225 requests this session, back at 18:40" rather than the
    // "waiting for capacity" that nobody can act on.
    case "run_paused":
    case "run_pause_updated":
      next.pause = event as unknown as PauseView;
      next.runStatus = "paused";
      break;

    // The heartbeat. Its only job is to keep the countdown honest and prove the
    // backend is still there; a pause with no tick is indistinguishable from a
    // dropped socket.
    case "pace_waiting":
      next.pause = state.pause
        ? { ...state.pause, ...(event as unknown as Partial<PauseView>) }
        : (event as unknown as PauseView);
      break;

    case "run_resumed":
      next.pauseHistory = state.pause
        ? [
            ...state.pauseHistory,
            { ...state.pause, ...(event as unknown as Partial<PauseView>) },
          ].slice(-50)
        : state.pauseHistory;
      next.pause = null;
      next.runStatus = "running";
      break;

    // The ETA has stopped meaning anything: the run keeps being told it will
    // resume and keeps not resuming. Surfaced because the alternative is a
    // countdown that silently resets forever.
    case "pace_stalled":
      next.pause = state.pause
        ? { ...state.pause, reason: "stalled" }
        : (event as unknown as PauseView);
      break;

    // --no-wait was set and the ration was spent. The run stops rather than
    // parks, so this is terminal.
    case "run_pace_refused":
      next.pause = event as unknown as PauseView;
      next.runStatus = "paced_out";
      break;

    case "run_finished":
      next.runStatus = String(event.status ?? "finished");
      next.pause = null;
      break;

    case "run_failed":
      next.runStatus = "failed";
      break;
  }

  return next;
}

export function useRunStream() {
  const [state, setState] = useState<StreamState>(EMPTY);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const socketRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  const closedRef = useRef(false);
  // Bumped when the operator token changes, which re-runs the effect below.
  // The token is in the handshake URL, so a new one only takes effect on a
  // new socket -- otherwise pasting the right token leaves the stream dead
  // until a reload.
  const [tokenEpoch, setTokenEpoch] = useState(0);

  useEffect(() => onTokenChange(() => setTokenEpoch((n) => n + 1)), []);

  useEffect(() => {
    closedRef.current = false;

    const connect = () => {
      if (closedRef.current) return;
      // Token in the URL because a browser cannot set a header on a
      // handshake. Without it a gated control plane closes the socket with
      // 1008 and the dashboard reconnects forever showing nothing.
      const socket = new WebSocket(streamUrl());
      socketRef.current = socket;
      setConnection("connecting");

      socket.onopen = () => {
        attemptRef.current = 0;
        setConnection("open");
      };

      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(message.data) as SwarmEvent;
          setState((current) => reduce(current, event));
        } catch {
          // A malformed frame must not kill the stream. Dropping one event is
          // recoverable; tearing down the socket loses the whole run's view.
        }
      };

      socket.onclose = (event) => {
        // 1008 is the policy close the control plane sends when the token is
        // missing or wrong. Retrying it is pointless -- the next handshake
        // carries the same token -- and a reconnect loop hides the reason
        // behind a flickering "Connecting". Stop, and say which it is.
        if (event.code === 1008) {
          setConnection("unauthorized");
          return;
        }
        setConnection("closed");
        if (closedRef.current) return;
        const delay = Math.min(
          RECONNECT_MAX_MS,
          RECONNECT_MIN_MS * 2 ** attemptRef.current,
        );
        attemptRef.current += 1;
        setTimeout(connect, delay);
      };

      socket.onerror = () => socket.close();
    };

    connect();
    return () => {
      closedRef.current = true;
      socketRef.current?.close();
    };
  }, [tokenEpoch]);

  const submit = useCallback(
    async (
      task: string,
      profile: string,
      chaos: boolean,
      useSkills: boolean,
      agents: number | null,
      seedRogues: string,
    ) => {
      const response = await apiFetch("/api/runs", {
        method: "POST",
        body: JSON.stringify({
          task,
          profile,
          chaos,
          use_skills: useSkills,
          // null, not 0: omitting the field lets the profile decide, which is
          // a different request from asking for no agents.
          agents,
          seed_rogues: seedRogues,
        }),
      });
      if (!response.ok) {
        if (response.status === 401) {
          throw new Error("operator token required — set it in the top bar");
        }
        throw new Error(`${response.status}: ${await response.text()}`);
      }
      return (await response.json()) as { run_id: string };
    },
    [],
  );

  const agents = useMemo(
    () =>
      [...state.agents.values()].sort((a, b) =>
        a.agent_id.localeCompare(b.agent_id),
      ),
    [state.agents],
  );

  return { ...state, agents, connection, submit };
}
