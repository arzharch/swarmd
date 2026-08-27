"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  AgentState,
  ConnectionState,
  CriterionView,
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

    case "run_finished":
      next.runStatus = String(event.status ?? "finished");
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

  useEffect(() => {
    closedRef.current = false;

    const connect = () => {
      if (closedRef.current) return;
      const protocol = window.location.protocol === "https:" ? "wss" : "ws";
      const url = `${protocol}://${window.location.host}/api/stream`;
      const socket = new WebSocket(url);
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

      socket.onclose = () => {
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
  }, []);

  const submit = useCallback(
    async (task: string, profile: string, chaos: boolean, useSkills: boolean) => {
      const response = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task,
          profile,
          chaos,
          use_skills: useSkills,
        }),
      });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`${response.status}: ${detail}`);
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
