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
import sys
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

        trace_sink = None
        if args.trace_jsonl:
            from swarmd.observability.tracing import JsonlTraceSink

            trace_sink = JsonlTraceSink(args.trace_jsonl)

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

    if args.command in ("approve", "reject", "list"):
        return asyncio.run(_hitl_command(args))

    parser.error(f"unknown command: {args.command}")
    return 2


async def _hitl_command(args: argparse.Namespace) -> int:
    """HITL CLI over the same durable store the pipeline writes to.

    NOTE: uses an in-process store; Phase 6 swaps in Postgres so approvals
    survive across processes. The state machine is identical.
    """
    from swarmd.hitl.approvals import ApprovalManager, InMemoryApprovalStore

    mgr = ApprovalManager(InMemoryApprovalStore())

    if args.command == "list":
        pending = await mgr.pending()
        if not pending:
            print("no pending approvals")
            return 0
        for req in pending:
            company = req.item.get("company", "?")
            subject = req.item.get("subject", "?")
            print(f"{req.request_id}  [{req.stage}]  {company}: {subject}")
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


async def _pending_count(pipe: Any) -> int:
    return len(await pipe.approvals.pending())


if __name__ == "__main__":
    sys.exit(main())
