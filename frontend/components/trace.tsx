"use client";

import { useEffect, useMemo, useState } from "react";
import { Card } from "@/components/panels";
import type { CriterionView, LedgerResponse, LedgerRow, PlanView, SwarmEvent } from "@/lib/types";

/**
 * Traceability.
 *
 * Built on the LEDGER rather than on the event stream, and the distinction is
 * the whole point of the page. The event stream is what the run said as it
 * happened; the ledger is the append-only record every reported number is an
 * aggregate over (ADR-007). If a figure in a report cannot be traced to rows
 * here, the figure is wrong.
 *
 * So this view answers one question the rest of the dashboard cannot: *where
 * did that number come from?*
 */

/* ------------------------------------------------------------- provenance */

export function ProvenancePanel({
  runId,
  criterion,
  plan,
  integrityHash,
  verify,
}: {
  runId: string | null;
  criterion: CriterionView | null;
  plan: PlanView | null;
  integrityHash?: string;
  verify?: LedgerResponse["verify"];
}) {
  return (
    <Card title="Provenance" meta={runId ?? undefined}>
      {!runId ? (
        <p className="empty">
          No run yet.
          <span className="hint">
            Every result traces to a criterion hash, a plan hash, and the ledger
            rows that produced it.
          </span>
        </p>
      ) : (
        <>
          <dl className="kv">
            <dt>criterion</dt>
            <dd>{criterion?.hash ?? "—"}</dd>
            <dt>plan</dt>
            <dd>{plan?.hash ?? "—"}</dd>
            <dt>integrity</dt>
            <dd>{integrityHash ?? "—"}</dd>
          </dl>

          <div className="rail-section" style={{ padding: "16px 0 6px" }}>
            Ledger reconciliation
          </div>
          {verify?.durable ? (
            <>
              <dl className="kv">
                <dt>rows in memory</dt>
                <dd>{verify.rows_in_memory}</dd>
                <dt>rows on disk</dt>
                <dd>{verify.rows_on_disk}</dd>
              </dl>
              <div style={{ marginTop: 8 }}>
                <span className={`pill ${verify.match ? "passed" : "failed"}`}>
                  {verify.match
                    ? "memory matches disk"
                    : "mismatch — torn write"}
                </span>
              </div>
            </>
          ) : (
            <p className="empty" style={{ paddingTop: 4 }}>
              Non-durable ledger.
              <span className="hint">
                Pass <code>--ledger PATH</code> to write an append-only file
                that survives the process.
              </span>
            </p>
          )}
        </>
      )}
    </Card>
  );
}

/* ----------------------------------------------------------------- ledger */

const COST_KINDS = new Set(["llm_call", "cache_hit"]);

export function LedgerPanel({ runId }: { runId: string | null }) {
  const [data, setData] = useState<LedgerResponse | null>(null);
  const [kind, setKind] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!runId) {
      setData(null);
      return;
    }
    let cancelled = false;
    const query = kind ? `?kind=${encodeURIComponent(kind)}` : "";
    fetch(`/api/runs/${runId}/ledger${query}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((body) => {
        if (!cancelled) {
          setData(body as LedgerResponse);
          setError(null);
        }
      })
      .catch((exc) => {
        if (!cancelled) setError(String(exc));
      });
    return () => {
      cancelled = true;
    };
  }, [runId, kind]);

  const rows = data?.rows ?? [];

  return (
    <Card
      title="Ledger"
      tall
      meta={data ? `${rows.length} of ${data.total}` : undefined}
    >
      {!runId ? (
        <p className="empty">
          No run yet.
          <span className="hint">
            The append-only record. Every reported number is a sum over these
            rows, never a counter.
          </span>
        </p>
      ) : error ? (
        <p className="empty">Ledger unavailable ({error}).</p>
      ) : (
        <>
          <div
            style={{
              display: "flex",
              gap: 6,
              flexWrap: "wrap",
              marginBottom: 12,
            }}
          >
            <button
              className={kind === "" ? "" : "ghost"}
              style={{ height: 28, fontSize: 12, padding: "0 10px" }}
              onClick={() => setKind("")}
            >
              all
            </button>
            {(data?.kinds ?? []).map((k) => (
              <button
                key={k}
                className={kind === k ? "" : "ghost"}
                style={{ height: 28, fontSize: 12, padding: "0 10px" }}
                onClick={() => setKind(k)}
              >
                {k}
              </button>
            ))}
          </div>

          {rows.length === 0 ? (
            <p className="empty">No rows of this kind.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th style={{ width: 44 }}>seq</th>
                  <th>kind</th>
                  <th>stage</th>
                  <th>agent</th>
                  <th>provider / model</th>
                  <th style={{ textAlign: "right" }}>tokens</th>
                  <th style={{ textAlign: "right" }}>cost</th>
                </tr>
              </thead>
              <tbody>
                {rows
                  .slice()
                  .reverse()
                  .map((row) => (
                    <LedgerRowView key={row.seq} row={row} />
                  ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </Card>
  );
}

function LedgerRowView({ row }: { row: LedgerRow }) {
  const [open, setOpen] = useState(false);
  const hasDetail = row.detail && Object.keys(row.detail).length > 0;
  return (
    <>
      <tr onClick={() => hasDetail && setOpen((v) => !v)}>
        <td className="num">{row.seq}</td>
        <td>
          <span className={`pill ${ledgerTone(row)}`}>{row.kind}</span>
        </td>
        <td>{row.stage || "—"}</td>
        <td className="mono">{row.agent_id || "—"}</td>
        <td className="mono" style={{ fontSize: 12 }}>
          {row.provider ? `${row.provider} / ${row.model}` : "—"}
        </td>
        <td className="num" style={{ textAlign: "right" }}>
          {COST_KINDS.has(row.kind) ? row.tokens_in + row.tokens_out : "—"}
        </td>
        <td className="num" style={{ textAlign: "right" }}>
          {COST_KINDS.has(row.kind) ? `$${row.cost_usd.toFixed(6)}` : "—"}
          {row.simulated && (
            <span
              title="This row came from the simulated provider"
              style={{ marginLeft: 6, color: "var(--amber)" }}
            >
              ●
            </span>
          )}
        </td>
      </tr>
      {open && hasDetail && (
        <tr>
          <td colSpan={7} style={{ background: "var(--bg-fill-tertiary)" }}>
            <pre
              className="mono"
              style={{ margin: 0, fontSize: 12, whiteSpace: "pre-wrap" }}
            >
              {JSON.stringify(row.detail, null, 2)}
            </pre>
          </td>
        </tr>
      )}
    </>
  );
}

function ledgerTone(row: LedgerRow): string {
  if (row.kind === "success") return "passed";
  if (row.kind === "containment") return "contained";
  if (row.kind === "abort") return "failed";
  if (row.kind === "cache_hit") return "running";
  return "neutral";
}

/* ---------------------------------------------------------- reasoning tape */

/**
 * Every agent's chain of thought on one timeline, ordered by the monotonic
 * tick stamped at record time rather than by span. Spans close after their
 * children, so ordering by span scrambles chronology — this is the bug the
 * tick was added to fix, and this view is where it would show.
 */
export function ReasoningTape({ events }: { events: SwarmEvent[] }) {
  const thoughts = useMemo(
    () =>
      events
        .filter((e) => e.kind === "thought")
        .slice(-250)
        .sort((a, b) => Number(a.tick ?? 0) - Number(b.tick ?? 0)),
    [events],
  );

  return (
    <Card title="Reasoning tape" tall meta={thoughts.length ? `${thoughts.length}` : undefined}>
      {thoughts.length === 0 ? (
        <p className="empty">
          No reasoning recorded yet.
          <span className="hint">
            Every agent&apos;s thoughts, interleaved in true chronological order.
          </span>
        </p>
      ) : (
        thoughts.map((thought) => (
          <div className="thought" key={thought.seq}>
            <span className="decision">
              <span style={{ color: "var(--text-faint)", marginRight: 6 }}>
                {String(thought.agent_id ?? "")}
              </span>
              {String(thought.decision ?? "")}
            </span>
            <span className="reasoning">{String(thought.reasoning ?? "")}</span>
          </div>
        ))
      )}
    </Card>
  );
}

/* ------------------------------------------------------------ system links */

export function ObservabilityLinks() {
  return (
    <Card title="Deep observability">
      <p style={{ margin: "0 0 12px", color: "var(--text-muted)" }}>
        This page shows what the run recorded about itself. The stack below
        holds the same events with longer retention and cross-run queries.
      </p>
      <dl className="kv">
        <dt>traces</dt>
        <dd className="mono">Jaeger :16686</dd>
        <dt>metrics</dt>
        <dd className="mono">Prometheus :9090</dd>
        <dt>dashboards</dt>
        <dd className="mono">Grafana :3000</dd>
        <dt>scrape endpoint</dt>
        <dd className="mono">/metrics</dd>
      </dl>
      <p
        style={{
          marginTop: 12,
          marginBottom: 0,
          color: "var(--text-soft)",
          fontSize: 12,
        }}
      >
        Prometheus is for operating the system and may lose a scrape or reset on
        restart. The ledger above is for claims and does neither. Where they
        disagree, the ledger is right.
      </p>
    </Card>
  );
}
