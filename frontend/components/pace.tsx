"use client";

/**
 * Pacing surfaces: what a run is waiting for, and when it comes back.
 *
 * A paused run and a hung one are indistinguishable from the outside -- no
 * events, no errors, nothing finishing. On a dashboard that renders only real
 * data (ADR-006) that means hours of blank screen for a run that is working
 * exactly as designed. These two banners are the difference.
 *
 * Both render the numbers the backend sent rather than re-deriving anything,
 * so the figure on the banner is the figure the gate actually applied.
 */

import { useEffect, useState } from "react";
import type { Forecast, PauseView } from "../lib/types";

/** "in 3h 20m", "in 45m", "in 30s". Empty once the moment has passed. */
function until(unixSeconds: number, now: number): string {
  const seconds = unixSeconds - now / 1000;
  if (seconds <= 0) return "";
  if (seconds < 90) return `in ${Math.round(seconds)}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `in ${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `in ${hours}h ${minutes % 60}m`;
}

function clockAt(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Live countdown while the run is parked.
 *
 * Ticks locally rather than waiting for the backend's heartbeat: the heartbeat
 * is a minute apart in production, and a countdown that freezes for a minute
 * looks exactly like the hang this banner exists to rule out.
 */
export function PauseBanner({ pause }: { pause: PauseView }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const stalled = pause.reason === "stalled";
  const back = until(pause.resumes_at, now);

  return (
    <div className={stalled ? "banner warn" : "banner info"} role="status">
      <strong>{stalled ? "Pause is overrunning." : "Run paused."}</strong>{" "}
      {pause.provider && (
        <>
          <code>{pause.provider}</code>
          {pause.credential ? ` (${pause.credential})` : ""} has used{" "}
          {pause.used.toLocaleString()} of {pause.envelope.toLocaleString()}{" "}
          {pause.dimension} for this session.{" "}
        </>
      )}
      {back
        ? `Resuming ${back}, around ${clockAt(pause.resumes_at)}.`
        : "Resuming now."}{" "}
      {typeof pause.waiting_agents === "number" && pause.waiting_agents > 0 && (
        <>{pause.waiting_agents} agent(s) parked. </>
      )}
      {stalled
        ? "The estimate has slipped several times, so treat the countdown as unreliable — the provider is refusing for longer than it declared."
        : "Nothing is lost: the criterion, plan and completed nodes are on disk, and the run continues from there."}
      {pause.checkpoint_path && (
        <>
          {" "}
          State at <code>{pause.checkpoint_path}</code>.
        </>
      )}
    </div>
  );
}

/**
 * Before the run starts: not whether it fits, but when it will be done.
 *
 * "Does not fit today" covers both "finishes this evening after one pause" and
 * "spans three days". Only the second is a reason not to press the button, so
 * conflating them either blocks work that would have been fine or waves
 * through a run nobody wanted.
 */
export function ForecastBanner({ forecast }: { forecast: Forecast }) {
  if (forecast.verdict === "fits_this_session") return null;

  const finish = forecast.projected_finish;
  const firstPause = forecast.first_pause_at;

  if (forecast.verdict === "exceeds_horizon") {
    return (
      <div className="banner warn" role="status">
        <strong>This run does not finish at the current allowance.</strong> It
        needs about {forecast.estimated_calls.toLocaleString()} provider
        requests against a session capacity of{" "}
        {forecast.session_capacity.toLocaleString()}. Lower the agent count, or
        add a provider credential — otherwise it will pause repeatedly for days.
      </div>
    );
  }

  const spansDays = forecast.verdict === "spans_days";
  return (
    <div className={spansDays ? "banner warn" : "banner info"} role="status">
      <strong>
        {spansDays
          ? "This run spans more than a day."
          : "This run will pause and resume."}
      </strong>{" "}
      About {forecast.estimated_calls.toLocaleString()} requests across{" "}
      {forecast.sessions_needed} session(s), with {forecast.expected_pauses}{" "}
      pause(s).
      {firstPause && <> First pause around {clockAt(firstPause)}.</>}
      {finish && <> Projected finish {clockAt(finish)}.</>}{" "}
      {spansDays
        ? "It will wait rather than fail, but it will not be done today."
        : "It waits rather than failing; the work already paid for is kept."}
    </div>
  );
}
