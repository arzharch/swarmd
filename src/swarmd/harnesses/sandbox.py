"""SandboxHarness: run generated code without letting it run the machine.

Threat model, stated plainly so the controls can be judged against it. The code
being executed was written by a model, in a system whose agents are selected on
passing a check. That is not a hypothetical adversary -- selection pressure
actively searches for whatever passes the check most cheaply, and "delete the
test file" is cheaper than "solve the problem".

Controls, in the order they matter:

  1. Separate PROCESS, not a thread or `exec`. A thread shares the interpreter,
     so `sys.exit`, a segfault, or a monkeypatched builtin takes the run with
     it. `exec` in-process is not a sandbox at all.
  2. Wall-clock timeout with process-tree kill. A timeout that kills only the
     parent leaves orphaned children holding the CPU.
  3. Working directory confined to a per-execution temp dir, deleted after.
  4. Environment stripped to a minimal allowlist. Inheriting the parent env
     hands generated code every provider key in the process.
  5. Resource limits (CPU seconds, address space, file size, process count) via
     `setrlimit` where the platform provides it.
  6. Output truncation. A program printing an infinite stream would otherwise
     exhaust memory in the PARENT while the child looks well-behaved.

WHAT THIS IS NOT. It is not a security boundary against a determined attacker.
Real isolation is a container with seccomp and no network -- which is what the
Kubernetes Job provides in deployment (deploy/k8s/base/run-job.yaml), and this
harness is the in-process layer beneath it. `setrlimit` is unavailable on
Windows, so on Windows the limits degrade to timeout plus environment
stripping, and `limits_enforced` reports that rather than implying protection
that is not there.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# POSIX-only. Guarded on sys.platform rather than a try/except import so the
# type checker can narrow it too -- running mypy on Windows against code that
# only executes on Linux otherwise reports eleven errors for correct code.
if sys.platform != "win32":  # pragma: no cover - platform dependent
    import resource
    import signal

    RLIMIT_AVAILABLE = True
else:  # pragma: no cover - Windows
    RLIMIT_AVAILABLE = False


# Environment allowlist. Everything else is dropped, because the parent process
# holds provider API keys and a DATABASE_URL, and generated code has no reason
# to see any of it.
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "TEMP", "TMP")


class SandboxViolation(RuntimeError):
    """A policy was breached. Structured, not swallowed."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Resource caps for one execution.

    ANATOMY: timeout_s
      Wall clock before the process tree is killed. Why 30: long enough for a
      real computation on a small dataset, short enough that a wedged candidate
      cannot consume a run's wall-clock budget. A `demo` profile is 12-18
      minutes total (docs/CAPACITY.md), so a handful of 30s timeouts is
      recoverable and a single 5-minute one is not.

    ANATOMY: cpu_seconds
      CPU time, distinct from wall clock. Set slightly below timeout_s so a
      CPU-bound infinite loop is killed by the limit -- which returns a clean
      signal -- rather than by the timeout, which is indistinguishable from a
      slow-but-legitimate computation.

    ANATOMY: memory_mb
      Address-space cap. Why 512: enough for numpy on a small dataset, small
      enough that a runaway allocation is killed before the node notices. The
      pod's own limit is 4Gi, so several sandboxes can misbehave at once
      without evicting the run.

    ANATOMY: max_output_bytes
      Truncation point for stdout/stderr. Why 256KB: comfortably more than any
      legitimate program's diagnostic output, and bounded so a program printing
      forever exhausts nothing but its own patience. Truncation is REPORTED,
      never silent -- silently truncated output would make a criterion check
      on stdout fail for reasons invisible to the agent trying to repair it.

    ANATOMY: max_processes
      Cap on forks. Guards against a fork bomb from generated code, which is
      otherwise the fastest way for a candidate to take down the node.
    """

    timeout_s: float = 30.0
    cpu_seconds: int = 25
    memory_mb: int = 512
    max_output_bytes: int = 256 * 1024
    max_file_size_mb: int = 64
    max_processes: int = 64


@dataclass(slots=True)
class SandboxResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    truncated: bool = False
    violation: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)
    limits_enforced: bool = RLIMIT_AVAILABLE

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.violation


def _apply_limits(limits: SandboxLimits) -> None:  # pragma: no cover - subprocess
    """Applied in the child between fork and exec. POSIX only."""
    if sys.platform == "win32":
        return
    resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
    mem = limits.memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    size = limits.max_file_size_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
    resource.setrlimit(resource.RLIMIT_NPROC, (limits.max_processes, limits.max_processes))
    # New session, so killpg reaches the whole tree. Without it a timeout kills
    # the parent and leaves grandchildren running.
    os.setsid()


def _clean_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    # Unbuffered so output survives a kill; without it a timed-out process
    # loses everything it printed, which is exactly the output you need.
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class SandboxHarness:
    """Executes candidate code in an isolated subprocess."""

    def __init__(self, limits: SandboxLimits | None = None) -> None:
        self.limits = limits or SandboxLimits()

    async def run_python(
        self, code: str, *, files: dict[str, str] | None = None
    ) -> SandboxResult:
        """Write `code` to a temp dir and execute it there.

        Artifacts: the script may write JSON to `artifacts.json` in its working
        directory. That file is the ONLY channel from sandbox to criterion --
        parsing numbers out of stdout with a regex would make any program that
        prints a number able to claim success.
        """
        workdir = Path(tempfile.mkdtemp(prefix="swarmd-sandbox-"))
        try:
            for name, content in (files or {}).items():
                target = (workdir / name).resolve()
                # Reject traversal before writing. A candidate that supplies
                # "../../.ssh/authorized_keys" is not a bug report, it is the
                # threat model.
                if not str(target).startswith(str(workdir.resolve())):
                    return SandboxResult(
                        None, "", "", 0.0,
                        violation=f"path escapes sandbox: {name!r}",
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            script = workdir / "candidate.py"
            script.write_text(code, encoding="utf-8")
            result = await self._exec(
                [sys.executable, "-I", str(script)], cwd=workdir
            )

            artifacts_path = workdir / "artifacts.json"
            if artifacts_path.exists():
                import json

                try:
                    loaded = json.loads(artifacts_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        result.artifacts = loaded
                    else:
                        result.violation = "artifacts.json is not a JSON object"
                except json.JSONDecodeError as exc:
                    # Not a violation of policy -- just an unusable artifact.
                    # Recorded so the agent can repair it.
                    result.violation = f"artifacts.json unparseable: {exc}"
            return result
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _exec(self, argv: list[str], *, cwd: Path) -> SandboxResult:
        loop = asyncio.get_running_loop()
        start = loop.time()

        popen_kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "env": _clean_env(),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
        }
        if RLIMIT_AVAILABLE:  # pragma: no branch
            popen_kwargs["preexec_fn"] = lambda: _apply_limits(self.limits)

        try:
            proc = await asyncio.create_subprocess_exec(*argv, **popen_kwargs)
        except OSError as exc:
            return SandboxResult(None, "", "", 0.0, violation=f"spawn failed: {exc}")

        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.limits.timeout_s
            )
        except TimeoutError:
            timed_out = True
            _kill_tree(proc)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            except (TimeoutError, ProcessLookupError):
                stdout, stderr = b"", b""

        out, truncated_out = _truncate(stdout, self.limits.max_output_bytes)
        err, truncated_err = _truncate(stderr, self.limits.max_output_bytes)

        return SandboxResult(
            exit_code=None if timed_out else proc.returncode,
            stdout=out,
            stderr=err,
            duration_s=round(loop.time() - start, 4),
            timed_out=timed_out,
            truncated=truncated_out or truncated_err,
        )


def _kill_tree(proc: Any) -> None:
    """Kill the process and anything it spawned.

    Killing only the parent is the common mistake: a candidate that forked a
    worker leaves it holding CPU for the rest of the run, and the symptom
    appears later as unexplained slowness in an unrelated stage.
    """
    try:
        if sys.platform != "win32":  # pragma: no cover - POSIX only
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _truncate(raw: bytes, limit: int) -> tuple[str, bool]:
    """Decode and cap. Truncation is reported, never silent."""
    truncated = len(raw) > limit
    if truncated:
        raw = raw[:limit]
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        text += f"\n...[truncated at {limit} bytes]"
    return text, truncated
