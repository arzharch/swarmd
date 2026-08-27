"""CLI entry point.

Commands:

    swarmd demo kernel [--kill-rate F] [--tasks N] [--seed I]
        Runs the kill-and-resume determinism demo. Exit code 0 = hashes match.

    swarmd approve|reject|list
        HITL CLI — Phase 3 wires these.

    swarmd bench
        Benchmark suite — Phase 6 wires this.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

from swarmd.demo import demo_kernel


def _ensure_examples_importable() -> None:
    """Make examples/ importable when running from an installed console script.

    examples/ is deliberately NOT a package dependency (kernel purity, ADR-002):
    the flagship app must be embeddable-by-example, not bundled. When the CLI
    runs from source checkout, we add the repo root to sys.path.
    """
    root = Path(__file__).resolve().parents[2]  # src/swarmd/cli.py -> repo root
    if (root / "examples").is_dir() and str(root) not in sys.path:
        sys.path.insert(0, str(root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swarmd")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="determinism demos")
    demo_sub = demo.add_subparsers(dest="demo_command", required=True)
    kernel = demo_sub.add_parser("kernel", help="kill-and-resume output-hash demo")
    kernel.add_argument(
        "--kill-rate",
        type=float,
        default=0.3,
        help="probability a live agent is killed per chaos tick (0..1). "
        "0.3 hits every recovery path quickly without starving progress.",
    )
    kernel.add_argument(
        "--tasks", type=int, default=20, help="number of tasks in the run"
    )
    kernel.add_argument(
        "--seed", type=int, default=42, help="chaos RNG seed; same seed = same kills"
    )

    leadops = sub.add_parser("leadops", help="run the LeadOps flagship pipeline")
    leadops.add_argument(
        "--provider",
        choices=["mock", "openrouter"],
        default="mock",
        help="LLM backend. 'mock' is deterministic and offline; 'openrouter' uses "
        "the free-model fallback chain (needs OPENROUTER_API_KEY, degrades to "
        "mock without one).",
    )
    leadops.add_argument(
        "--limit", type=int, default=20, help="max leads to process from fixtures"
    )
    leadops.add_argument(
        "--trace-jsonl",
        default=None,
        metavar="PATH",
        help="export every span (stages, LLM calls, CoT decisions) as JSONL — "
        "the drop-in format Langfuse-style observability backends ingest. "
        "Combine with OTel for dual-backend tracing.",
    )
    leadops.add_argument(
        "--otel",
        action="store_true",
        help="export spans to OpenTelemetry (Jaeger at localhost:4318 via "
        "docker compose up jaeger). Needs the otel extra; degrades to a warning "
        "without it.",
    )

    providers = sub.add_parser("providers", help="inspect the LLM provider pool")
    providers_sub = providers.add_subparsers(dest="providers_command", required=True)
    probe = providers_sub.add_parser(
        "probe",
        help="send one tiny request per provider to discover what is actually live",
    )
    probe.add_argument(
        "--allow-data-training",
        action="store_true",
        help="admit the Mistral Experiment tier, whose free quota is granted in "
        "exchange for consenting to have submitted prompts used for training. "
        "Off by default: that tier's price is paid in data, not dollars, and "
        "that should be a deliberate choice.",
    )
    probe.add_argument(
        "--allow-paid",
        action="store_true",
        help="admit the paid overflow tier (GLM 5.3 Flash). Off by default, so "
        "exhausting free capacity stops the run instead of quietly spending.",
    )

    swarm = sub.add_parser("swarm", help="run the generalist swarm on a task")
    swarm_sub = swarm.add_subparsers(dest="swarm_command", required=True)
    swarm_run = swarm_sub.add_parser("run", help="run one unknown task end to end")
    swarm_run.add_argument("task", help="the task, in plain language")
    swarm_run.add_argument(
        "--profile",
        default="standard",
        choices=["smoke", "standard", "deep", "eval"],
        help="run size, derived from docs/CAPACITY.md rather than chosen: "
        "smoke ~2min/60 calls (CI), standard 12-18min/600 calls, deep "
        "~40min/1800 calls (enough curve points to mean something).",
    )
    swarm_run.add_argument(
        "--chaos", action="store_true",
        help="kill agents mid-run. On by default in every deployed environment: "
        "turning it off would make production the one place the recovery "
        "guarantee is never tested.",
    )
    swarm_run.add_argument(
        "--kill-rate", type=float, default=0.2,
        help="probability an agent is killed per scheduling tick. 0.2 hits "
        "every recovery path within one run while leaving enough survivors to "
        "prove partial progress is preserved.",
    )
    swarm_run.add_argument(
        "--ceiling", type=float, default=0.05, metavar="USD",
        help="hard spend limit for the run, checked at the harness boundary. "
        "Breach aborts cleanly with an itemised report rather than truncating, "
        "because a truncated run still emits numbers that look like results.",
    )
    swarm_run.add_argument(
        "--no-skills", action="store_true",
        help="the CONTROL ARM. Disables skill retrieval with everything else "
        "identical, which is what an improvement claim is measured against.",
    )
    swarm_run.add_argument("--skills", default=None, metavar="PATH",
                           help="skill library file (default: no library)")
    swarm_run.add_argument("--ledger", default=None, metavar="PATH",
                           help="write the append-only cost ledger here")
    swarm_run.add_argument("--json", action="store_true",
                           help="emit the full report as JSON")

    ev = sub.add_parser("eval", help="run the evaluation suite with a control arm")
    ev.add_argument(
        "--arms", default="both", choices=["both", "public", "custom"],
        help="public answers 'is this self-graded?'; custom answers 'does it "
        "handle what it was not built for?'. Reported separately so a strong "
        "result on one cannot hide a weak result on the other.",
    )
    ev.add_argument(
        "--repeats", type=int, default=5,
        help="runs per task per arm. Below 3 a bootstrap interval is "
        "meaningless; each repeat costs a full run against a ~45 req/min "
        "ceiling, so 100 tasks x 2 arms x 5 repeats is most of a day's quota.",
    )
    ev.add_argument(
        "--holdout", action="store_true",
        help="include the held-out tasks. Opt-in so a routine eval cannot "
        "silently consume the set reserved for acceptance.",
    )
    ev.add_argument("--profile", default="smoke",
                    choices=["smoke", "standard", "deep", "eval"])
    ev.add_argument("--benchmarks", default=None, metavar="PATH",
                    help="generate BENCHMARKS.md here (refuses simulated data)")
    ev.add_argument("--json", default=None, metavar="PATH",
                    help="write the full report as JSON")

    ledger = sub.add_parser("ledger", help="read an append-only ledger file")
    ledger_sub = ledger.add_subparsers(dest="ledger_command", required=True)
    lreport = ledger_sub.add_parser(
        "report", help="cost and call breakdown, aggregated from rows"
    )
    lreport.add_argument("path", help="path to the ledger JSONL file")
    lreport.add_argument("--ceiling", type=float, default=0.05)
    lreport.add_argument("--json", action="store_true")
    lverify = ledger_sub.add_parser(
        "verify",
        help="check a ledger for damage. Distinguishes a torn tail (expected "
        "after a hard kill, surviving rows trustworthy) from sequence gaps "
        "(rows missing from the middle, aggregates understate the run).",
    )
    lverify.add_argument("path")
    lverify.add_argument("--json", action="store_true")

    runcmd = sub.add_parser("run", help="inspect a completed run")
    run_sub = runcmd.add_subparsers(dest="run_command", required=True)
    rinspect = run_sub.add_parser(
        "inspect", help="criterion, plan, containments and spend from a ledger"
    )
    rinspect.add_argument("path", help="path to the ledger JSONL file")
    rinspect.add_argument("--criterion", action="store_true",
                          help="print only the frozen criterion")
    rinspect.add_argument("--containments", action="store_true",
                          help="print only the containment audit")
    rinspect.add_argument("--json", action="store_true")

    serve = sub.add_parser("serve", help="run the control plane and event stream")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--skills", default=None, metavar="PATH")

    approve = sub.add_parser("approve", help="approve a pending draft (HITL)")
    approve.add_argument("request_id")
    approve.add_argument("--actor", default="cli-user")

    reject = sub.add_parser("reject", help="reject a pending draft (HITL)")
    reject.add_argument("request_id")
    reject.add_argument("--actor", default="cli-user")

    sub.add_parser("list", help="list pending approvals (HITL)")

    args = parser.parse_args(argv)

    if args.command == "demo":
        result = asyncio.run(demo_kernel(args.kill_rate, args.tasks, args.seed))
        print(f"tasks={result['tasks']} kill_rate={result['kill_rate']} seed={result['seed']}")
        print(f"clean_hash = {result['clean_hash']}")
        print(f"chaos_hash = {result['chaos_hash']}")
        print(
            f"kills={result['kills']} requeues={result['requeues']} "
            f"ticks={result['chaos_ticks']}"
        )
        print("INTEGRITY: MATCH — chaos did not change the result" if result["match"]
              else "INTEGRITY: MISMATCH — RECOVERY BUG")
        return 0 if result["match"] else 1

    if args.command == "leadops":
        _ensure_examples_importable()
        from examples.leadops.pipeline import LeadOpsPipeline
        from examples.leadops.sources.fixtures import load_leads
        from swarmd.router.providers import make_router

        router = make_router(args.provider)

        from swarmd.observability.tracing import TraceSink

        trace_sink: TraceSink | None = None
        sinks: list[TraceSink] = []
        if args.trace_jsonl:
            from swarmd.observability.tracing import JsonlTraceSink

            sinks.append(JsonlTraceSink(args.trace_jsonl))
        if getattr(args, "otel", False):
            from swarmd.observability.otel_bridge import make_sink

            sinks.append(make_sink("otel"))
        if len(sinks) > 1:
            from swarmd.observability.tracing import CompositeSink

            trace_sink = CompositeSink(*sinks)
        elif sinks:
            trace_sink = sinks[0]

        pipe = LeadOpsPipeline(router, trace_sink=trace_sink)
        leads = load_leads()[: args.limit]
        res = asyncio.run(pipe.run(leads))

        print(f"provider={args.provider} leads_in={res.leads_in}")
        print(
            f"enriched={res.enriched} deduped={res.deduped} scored={res.scored} "
            f"drafted={res.drafted} qa_passed={res.qa_passed}"
        )
        print(f"awaiting_review={res.awaiting_review} dead_lettered={res.dead_lettered}")
        if res.taxonomy:
            print(f"failure_taxonomy={res.taxonomy}")
        print(f"integrity_hash={res.integrity_hash}")
        print(f"trace_id={res.trace_id}")
        if args.trace_jsonl:
            print(f"trace_exported={args.trace_jsonl} (JSONL spans incl. LLM calls + CoT)")
        pending = asyncio.run(_pending_count(pipe))
        print(f"review_queue_pending={pending} (outreach never auto-sends)")
        return 0

    if args.command == "providers":
        return asyncio.run(_providers_command(args))

    if args.command == "swarm":
        return asyncio.run(_swarm_command(args))

    if args.command == "eval":
        return asyncio.run(_eval_command(args))

    if args.command in ("ledger", "run"):
        return _ledger_command(args)

    if args.command == "serve":
        return _serve_command(args)

    if args.command in ("approve", "reject", "list"):
        return asyncio.run(_hitl_command(args))

    parser.error(f"unknown command: {args.command}")
    return 2


async def _providers_command(args: argparse.Namespace) -> int:
    """Discover live provider capacity by asking the providers.

    Published free-tier limits disagree across sources and change without
    notice, so the pool treats them as hints and this command replaces them
    with observation. Exit code is non-zero when nothing is reachable, so it
    works as a preflight check in a script.
    """
    from swarmd.router.pool import ProviderPool

    try:
        pool = ProviderPool.from_env(
            allow_data_training=args.allow_data_training,
            allow_paid=args.allow_paid,
        )
    except RuntimeError as exc:
        print(f"pool unavailable: {exc}")
        return 2

    rows = await pool.probe()
    await pool.aclose()

    width = max(len(r["provider"]) for r in rows)
    for row in sorted(rows, key=lambda r: (not r["ok"], r["provider"])):
        name = row["provider"].ljust(width)
        tier = row["tier"].ljust(19)
        if row["ok"]:
            print(
                f"{name}  {tier}  OK    {row['latency_s']:>6.3f}s  {row['model']}"
            )
        else:
            detail = row.get("reason", "")
            if row.get("retry_after_s") is not None:
                detail = f"{detail} retry_after={row['retry_after_s']}s"
            print(f"{name}  {tier}  FAIL  {detail}")

    live = sum(1 for r in rows if r["ok"])
    print(f"\n{live}/{len(rows)} providers live")
    return 0 if live else 1


async def _swarm_command(args: argparse.Namespace) -> int:
    """Run one unknown task end to end and print what happened."""
    import json as _json

    from swarmd.chaos import ChaosHook
    from swarmd.harnesses.sandbox import SandboxHarness
    from swarmd.router.pool import ProviderPool
    from swarmd.swarm.run import SwarmRun
    from swarmd.swarm.skills import SkillLibrary

    try:
        pool = ProviderPool.from_env()
    except RuntimeError as exc:
        print(f"no provider capacity: {exc}")
        print(
            "\nSet a provider key, or SWARMD_SIMULATED_PROVIDER=true "
            "to develop without one (results are marked simulated)."
        )
        return 2

    run = SwarmRun(
        pool,
        profile=args.profile,
        ceiling_usd=args.ceiling,
        use_skills=not args.no_skills,
        skills=SkillLibrary(args.skills) if args.skills else None,
        sandbox=SandboxHarness(),
        chaos=ChaosHook(kill_rate=args.kill_rate) if args.chaos else None,
        ledger_path=args.ledger,
        on_event=_print_event,
    )
    result = await run.run(args.task)
    report = run.report(result)
    await pool.aclose()

    if args.json:
        print(_json.dumps(report, indent=2))
        return 0 if result.status == "completed" else 1

    print(
        f"\nrun={result.run_id}  status={result.status}  "
        f"{result.duration_s:.1f}s"
    )
    if result.criterion:
        print(f"criterion={result.criterion.hash} "
              f"({len(result.criterion.criterion.checks)} checks, "
              f"attempts={result.criterion.attempts})")
    if result.plan:
        print(f"plan={result.plan.content_hash()} "
              f"({len(result.plan.nodes)} nodes, width={result.plan.width})")
    print(f"nodes_passed={len(result.passed)}/{len(result.results)}  "
          f"contained={len(result.contained)}")
    print(f"integrity_hash={result.integrity_hash()}")

    cost = report["cost"]
    marker = "  [SIMULATED]" if cost.get("simulated") else ""
    print(f"cost=${cost['total_usd']:.6f} of ${cost['ceiling_usd']} ceiling  "
          f"calls={cost['llm_calls']}  cache_hits={cost['cache_hits']}{marker}")
    econ = report["economy"]
    print(f"agents={econ['population']} alive={econ['alive']} "
          f"bankrupt={econ['bankruptcies']} contained={econ['contained']}")
    rt = report["redteam"]
    print(f"redteam: contained={rt['contained']} flagged={rt['flagged']} "
          f"llm_calls={rt['llm_calls_used']}")
    if result.error:
        print(f"error: {result.error}")
    return 0 if result.status == "completed" else 1


def _print_event(event: dict[str, Any]) -> None:
    """Terse live log. The dashboard is the rich view; this is the tail."""
    kind = event.get("kind", "")
    if kind == "thought":
        print(f"  . {event.get('agent_id', '')}: {event.get('decision', '')}")
    elif kind in {"criterion_frozen", "plan_selected", "run_started",
                  "containment", "agent_killed", "node_finished"}:
        detail = event.get("hash") or event.get("node") or event.get("reason") or ""
        print(f"  * {kind} {detail}")


async def _eval_command(args: argparse.Namespace) -> int:
    """Run both arms over the suite and report with confidence intervals."""
    _ensure_examples_importable()
    from examples.tasks.suite import suite
    from swarmd.harnesses.sandbox import SandboxHarness
    from swarmd.ledger import SimulatedDataRefused
    from swarmd.router.pool import ProviderPool
    from swarmd.swarm.evaluate import Evaluator
    from swarmd.swarm.run import SwarmRun

    try:
        pool = ProviderPool.from_env()
    except RuntimeError as exc:
        print(f"no provider capacity: {exc}")
        return 2

    sandbox = SandboxHarness()

    async def run_factory(task: Any, use_skills: bool, seed: int) -> Any:
        run = SwarmRun(
            pool,
            profile=args.profile,
            use_skills=use_skills,
            sandbox=sandbox,
            run_id=f"eval-{task.task_id}-{seed}-{'t' if use_skills else 'c'}",
        )
        result = await run.run(task.prompt)
        return result, run.report(result)

    tasks = suite(arms=args.arms, include_holdout=args.holdout)
    print(f"evaluating {len(tasks)} tasks x {args.repeats} repeats x 2 arms "
          f"= {len(tasks) * args.repeats * 2} runs")

    report = await Evaluator(run_factory, repeats=args.repeats).evaluate(tasks)
    await pool.aclose()

    print()
    print(report.render())

    if args.json:
        report.write_json(args.json)
        print(f"\nwrote {args.json}")
    if args.benchmarks:
        try:
            report.write_benchmarks(args.benchmarks)
            print(f"wrote {args.benchmarks}")
        except SimulatedDataRefused as exc:
            print(f"\nBENCHMARKS.md NOT written: {exc}")
            return 1
    return 0


def _ledger_command(args: argparse.Namespace) -> int:
    """Read a ledger file back. The commands docs/RUNBOOK.md tells you to run."""
    import json as _json

    from swarmd import ledger_cli

    try:
        if args.command == "ledger" and args.ledger_command == "report":
            data = ledger_cli.report(args.path, ceiling=args.ceiling)
            print(_json.dumps(data, indent=2) if args.json
                  else ledger_cli.render_report(data))
            return 0

        if args.command == "ledger" and args.ledger_command == "verify":
            data = ledger_cli.verify(args.path)
            print(_json.dumps(data, indent=2) if args.json
                  else ledger_cli.render_verify(data))
            # Non-zero on genuine damage so a script can branch on it. A torn
            # tail is not damage: it is what a hard kill looks like.
            return 0 if data["intact"] or data["tail_truncated"] else 1

        if args.command == "run" and args.run_command == "inspect":
            data = ledger_cli.inspect(args.path)
            if args.criterion:
                print(_json.dumps(data["criterion"], indent=2))
            elif args.containments:
                print(_json.dumps(data["containments"], indent=2))
            elif args.json:
                print(_json.dumps(data, indent=2))
            else:
                print(ledger_cli.render_inspect(data))
            return 0
    except ledger_cli.LedgerNotFound as exc:
        print(f"{exc}")
        return 2

    print(f"unknown subcommand for {args.command}")
    return 2


def _serve_command(args: argparse.Namespace) -> int:
    """Start the control plane. Blocks."""
    try:
        import uvicorn

        from swarmd.observability import logs
        from swarmd.server.app import create_app
        from swarmd.server.middleware import (
            ENV_TOKEN,
            InsecureConfiguration,
            require_safe_configuration,
        )
    except ImportError:
        print("serve needs the 'serve' extra: uv sync --extra serve")
        return 2

    logs.configure()
    token = os.environ.get(ENV_TOKEN, "")
    try:
        require_safe_configuration(args.host, token)
    except InsecureConfiguration as exc:
        print(f"refusing to start: {exc}")
        return 2

    app = create_app(skills_path=args.skills)
    print(f"swarmd control plane on http://{args.host}:{args.port}")
    print(
        "  auth:      operator token required"
        if token
        else "  auth:      OPEN (loopback only; set "
        f"{ENV_TOKEN} before exposing this)"
    )
    print(f"  stream:    ws://{args.host}:{args.port}/api/stream")
    print(f"  health:    http://{args.host}:{args.port}/healthz")
    print(f"  metrics:   http://{args.host}:{args.port}/metrics")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


async def _hitl_command(args: argparse.Namespace) -> int:
    """HITL CLI over the same durable store the pipeline writes to.

    `build_approval_store` picks Postgres when DATABASE_URL is set and SQLite
    otherwise. It never returns the in-memory store: doing so was the defect
    that made Phase 3's gate pass on paper while approvals silently vanished
    between processes, since every invocation got its own empty dict.
    """
    from swarmd.hitl.approvals import ApprovalManager
    from swarmd.hitl.stores import build_approval_store

    mgr = ApprovalManager(build_approval_store())

    if args.command == "list":
        pending = await mgr.pending()
        if not pending:
            print("no pending approvals")
            return 0
        for req in pending:
            summary = _summarise_item(req.item)
            age_s = int(time.time() - req.created_ts)
            print(f"{req.request_id}  [{req.stage}]  {_age(age_s):>6}  {summary}")
        print(f"\n{len(pending)} pending")
        return 0

    try:
        req = await mgr.decide(args.request_id, args.command, actor=args.actor)
    except KeyError:
        print(f"unknown request: {args.request_id}")
        return 1
    except ValueError as exc:
        print(f"rejected: {exc}")
        return 1
    print(f"{req.request_id} -> {req.state.value} (actor={args.actor})")
    trail = await mgr.audit()
    print(f"audit entries: {len(trail)} (append-only)")
    return 0


def _summarise_item(item: dict[str, Any]) -> str:
    """One readable line per queued item, whatever shape the item is.

    The review queue holds different things depending on the stage -- an
    outreach draft, a candidate skill, a contained agent -- so it cannot assume
    a schema. It shows the first few scalar fields rather than dumping JSON,
    because a queue nobody can skim is a queue nobody works through.
    """
    parts = []
    for key, value in item.items():
        if isinstance(value, (str, int, float, bool)):
            text = str(value)
            parts.append(f"{key}={text[:40]}")
        if len(parts) == 3:
            break
    return "  ".join(parts) or "<no scalar fields>"


def _age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


async def _pending_count(pipe: Any) -> int:
    return len(await pipe.approvals.pending())


if __name__ == "__main__":
    sys.exit(main())
