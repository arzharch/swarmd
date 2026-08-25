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

from swarmd.demo import demo_kernel


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

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
