"""Sandbox tests.

The threat model is not hypothetical: the code being run was written by a model
whose agents are selected on passing a check, and "delete the test" is cheaper
than "solve the problem". These tests assert the controls actually hold, on the
platform they are running on.
"""

from __future__ import annotations

import sys

import pytest

from swarmd.harnesses.sandbox import (
    ENV_ALLOWLIST,
    RLIMIT_AVAILABLE,
    SandboxHarness,
    SandboxLimits,
)


@pytest.fixture
def sandbox():
    return SandboxHarness(SandboxLimits(timeout_s=10.0, cpu_seconds=8))


# --- basic execution -------------------------------------------------------


async def test_a_successful_script_reports_exit_zero(sandbox):
    result = await sandbox.run_python("print('hello')")
    assert result.ok
    assert result.exit_code == 0
    assert "hello" in result.stdout


async def test_a_failing_script_reports_its_exit_code(sandbox):
    result = await sandbox.run_python("import sys; sys.exit(3)")
    assert not result.ok
    assert result.exit_code == 3


async def test_a_crashing_script_is_contained_not_propagated(sandbox):
    """An exception in generated code must not surface as an exception here."""
    result = await sandbox.run_python("raise RuntimeError('boom')")
    assert result.exit_code != 0
    assert "boom" in result.stderr


async def test_stderr_is_captured_separately(sandbox):
    result = await sandbox.run_python(
        "import sys; print('out'); print('err', file=sys.stderr)"
    )
    assert "out" in result.stdout
    assert "err" in result.stderr


# --- artifacts -------------------------------------------------------------


async def test_artifacts_come_from_a_file_not_from_parsing_stdout(sandbox):
    """Regexing numbers out of stdout would let any printing program claim success."""
    result = await sandbox.run_python(
        "import json\n"
        "json.dump({'accuracy': 0.942}, open('artifacts.json', 'w'))\n"
        "print('accuracy: 0.999')\n"   # a lie, in stdout
    )
    assert result.artifacts == {"accuracy": 0.942}


async def test_a_run_with_no_artifacts_file_reports_none(sandbox):
    result = await sandbox.run_python("print('no artifacts here')")
    assert result.artifacts == {}


async def test_unparseable_artifacts_are_reported_for_repair(sandbox):
    result = await sandbox.run_python("open('artifacts.json','w').write('{not json')")
    assert "unparseable" in result.violation


async def test_non_object_artifacts_are_rejected(sandbox):
    result = await sandbox.run_python("open('artifacts.json','w').write('[1,2,3]')")
    assert "not a JSON object" in result.violation


# --- isolation -------------------------------------------------------------


async def test_provider_keys_are_not_visible_to_generated_code(sandbox, monkeypatch):
    """Inheriting the parent env hands generated code every key in the process."""
    monkeypatch.setenv("GROQ_API_KEY", "sk-should-never-be-visible")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")

    result = await sandbox.run_python(
        "import os; print(os.environ.get('GROQ_API_KEY', 'ABSENT')); "
        "print(os.environ.get('DATABASE_URL', 'ABSENT'))"
    )
    assert "sk-should-never-be-visible" not in result.stdout
    assert result.stdout.count("ABSENT") == 2


async def test_only_allowlisted_environment_variables_survive(sandbox, monkeypatch):
    monkeypatch.setenv("SOME_RANDOM_VAR", "leak-me")
    result = await sandbox.run_python(
        "import os,json; print(json.dumps(sorted(os.environ)))"
    )
    assert "SOME_RANDOM_VAR" not in result.stdout
    for name in ("PYTHONUNBUFFERED",):
        assert name in result.stdout


async def test_each_execution_gets_a_fresh_working_directory(sandbox):
    """State must not leak between candidates -- that would fake reproducibility."""
    await sandbox.run_python("open('left_behind.txt','w').write('x')")
    result = await sandbox.run_python(
        "import os; print('FOUND' if os.path.exists('left_behind.txt') else 'CLEAN')"
    )
    assert "CLEAN" in result.stdout


async def test_supplied_files_are_available_to_the_script(sandbox):
    result = await sandbox.run_python(
        "print(open('data.txt').read())", files={"data.txt": "fixture-content"}
    )
    assert "fixture-content" in result.stdout


async def test_path_traversal_in_supplied_files_is_refused(sandbox):
    """A candidate supplying ../../.ssh/authorized_keys is the threat model."""
    result = await sandbox.run_python(
        "print('ran')", files={"../../escaped.txt": "malicious"}
    )
    assert "escapes sandbox" in result.violation
    assert result.exit_code is None  # never executed


async def test_nested_paths_inside_the_sandbox_are_allowed(sandbox):
    result = await sandbox.run_python(
        "print(open('sub/dir/data.txt').read())",
        files={"sub/dir/data.txt": "nested-ok"},
    )
    assert "nested-ok" in result.stdout


# --- limits ----------------------------------------------------------------


async def test_an_infinite_loop_is_killed_by_the_timeout():
    sandbox = SandboxHarness(SandboxLimits(timeout_s=1.5, cpu_seconds=1))
    result = await sandbox.run_python("while True: pass")
    assert result.timed_out
    assert not result.ok
    assert result.exit_code is None


async def test_a_sleeping_process_is_killed_by_the_timeout():
    sandbox = SandboxHarness(SandboxLimits(timeout_s=1.0))
    result = await sandbox.run_python("import time; time.sleep(60)")
    assert result.timed_out
    assert result.duration_s < 10


async def test_runaway_output_is_truncated_and_says_so():
    """Silent truncation makes a stdout check fail for invisible reasons."""
    sandbox = SandboxHarness(
        SandboxLimits(timeout_s=15.0, max_output_bytes=2048)
    )
    result = await sandbox.run_python("print('A' * 100000)")
    assert result.truncated
    assert "truncated" in result.stdout
    assert len(result.stdout) < 10_000


@pytest.mark.skipif(not RLIMIT_AVAILABLE, reason="setrlimit is POSIX-only")
async def test_excessive_allocation_is_refused():
    sandbox = SandboxHarness(SandboxLimits(timeout_s=15.0, memory_mb=64))
    result = await sandbox.run_python("x = bytearray(512 * 1024 * 1024)")
    assert not result.ok


async def test_limit_enforcement_is_reported_rather_than_assumed(sandbox):
    """On Windows setrlimit is unavailable; claiming protection would be a lie."""
    result = await sandbox.run_python("print('x')")
    assert result.limits_enforced is RLIMIT_AVAILABLE


async def test_a_spawn_failure_is_a_result_not_an_exception(sandbox, monkeypatch):
    async def boom(*a, **kw):
        raise OSError("no processes available")

    monkeypatch.setattr("asyncio.create_subprocess_exec", boom)
    result = await sandbox.run_python("print(1)")
    assert "spawn failed" in result.violation
    assert not result.ok


async def test_concurrent_sandboxes_do_not_interfere(sandbox):
    """The worker pool runs many of these at once."""
    import asyncio

    results = await asyncio.gather(*[
        sandbox.run_python(
            f"import json; json.dump({{'n': {i}}}, open('artifacts.json','w'))"
        )
        for i in range(8)
    ])
    assert sorted(r.artifacts["n"] for r in results) == list(range(8))


async def test_the_isolated_flag_blocks_ambient_site_packages(sandbox):
    """`-I` keeps the sandbox from importing whatever the host happens to have."""
    result = await sandbox.run_python("import sys; print(sys.flags.isolated)")
    assert "1" in result.stdout


def test_env_allowlist_excludes_every_credential_shaped_name():
    forbidden = ("KEY", "TOKEN", "SECRET", "PASSWORD", "DATABASE")
    for name in ENV_ALLOWLIST:
        assert not any(f in name.upper() for f in forbidden), name


def test_python_executable_is_the_one_running_the_tests():
    """A sandbox running a different interpreter would test nothing useful."""
    assert sys.executable
