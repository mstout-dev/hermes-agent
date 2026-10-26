"""Native local commands share a bounded slice and retain their scope during adoption."""

from unittest.mock import MagicMock

import pytest

from tools import process_registry as registry
from tools.environments import local
from tools.environments.local import LocalEnvironment
from tools import process_workload_scope as workload

pytestmark = pytest.mark.platforms("linux")


@pytest.fixture
def native_worker(monkeypatch):
    monkeypatch.setattr(local, "_make_run_env", lambda env: {"PATH": "/usr/bin:/bin"})
    monkeypatch.setattr(registry.ProcessRegistry, "_spawn_env", lambda self, env: {"PATH": "/usr/bin:/bin"})
    monkeypatch.setenv("INVOCATION_ID", "native-test")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setattr(workload, "_local_config", dict)
    monkeypatch.setattr(registry, "_is_supervised_gateway_process", lambda: False)


def test_foreground_refuses_command_without_scope(native_worker, monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_systemd_run_user_scope_available", lambda: False)
    env = object.__new__(LocalEnvironment)
    env.cwd, env.env = str(tmp_path), {}
    marker = tmp_path / "foreground-ran"
    with pytest.raises(workload.WorkloadScopeUnavailable):
        proc = env._run_bash(f"touch '{marker}'")
        proc.communicate(timeout=3)
    assert not marker.exists()


def test_background_refuses_command_without_scope(native_worker, monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_systemd_run_user_scope_available", lambda: False)
    monkeypatch.setattr(registry, "worker_output_log", lambda _id: None)
    process_registry = registry.ProcessRegistry()
    marker = tmp_path / "background-ran"
    try:
        with pytest.raises(workload.WorkloadScopeUnavailable):
            session = process_registry.spawn_local(f"touch '{marker}'", cwd=str(tmp_path))
            session.process.wait(timeout=3)
        assert not marker.exists()
        assert not process_registry._running
    finally:
        process_registry.kill_all()


def test_foreground_adoption_preserves_scope(monkeypatch, tmp_path):
    process_registry = registry.ProcessRegistry()
    proc = MagicMock(pid=12345, _hermes_systemd_unit="hermes-worker-owned.scope")
    monkeypatch.setattr(process_registry, "_track_started", lambda *args: None)
    session = process_registry.adopt_local(proc, command="sleep 5", cwd=str(tmp_path))
    assert session.systemd_unit == "hermes-worker-owned.scope"


@pytest.mark.parametrize("source", ["registered", "cron", "gateway"])
def test_native_compute_context_requires_scope(native_worker, monkeypatch, source):
    from agent import runtime_session_store
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    monkeypatch.setattr(runtime_session_store, "is_worker_process", lambda: source == "registered")
    monkeypatch.setattr(registry, "_is_supervised_gateway_process", lambda: source == "gateway")
    if source == "cron":
        monkeypatch.setenv("_HERMES_CRON_EXTERNAL_WORKER", "test-execution")
    assert workload.required_workload_scope()


def test_inherited_invocation_alone_keeps_cli_direct(native_worker, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    monkeypatch.setattr(registry, "_systemd_run_user_scope_available", lambda: pytest.fail("CLI probed systemd"))
    command = ["/bin/bash", "-c", "printf '%s' '${UNCHANGED}'"]
    assert workload.local_workload_argv(command) == (command, "")


@pytest.mark.parametrize("explicit", [False, True])
def test_cli_opt_in_refuses_unavailable_scope(native_worker, monkeypatch, explicit):
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    monkeypatch.setattr(workload, "_local_config", lambda: {"require_workload_scope": not explicit})
    monkeypatch.setattr(registry, "_systemd_run_user_scope_available", lambda: False)
    with pytest.raises(workload.WorkloadScopeUnavailable):
        workload.local_workload_argv(["/bin/true"], required=explicit)


def test_required_scope_keeps_shell_argv_literal_and_tightens_limit(native_worker, monkeypatch):
    monkeypatch.setattr(registry, "_systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr(workload, "_require_bounded_slice", lambda: None)
    monkeypatch.setattr(workload, "_local_config", lambda: {"memory_max_mb": 64})
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/systemd-run")
    monkeypatch.setattr(registry, "_worker_memory_max_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(registry, "_SYSTEMD_RUN_NO_EXPAND", True)
    command = ["/bin/bash", "-c", "printf '%s' '${LITERAL} $$'", "argument with spaces"]
    scoped, unit = workload.local_workload_argv(command, "contract-test")
    assert scoped[scoped.index("--") + 1:] == command
    assert "--expand-environment=no" in scoped
    assert scoped[scoped.index("--slice") + 1] == "hermes-workloads.slice"
    assert "MemorySwapMax=0" in scoped
    assert "TasksMax=256" in scoped
    assert "MemoryMax=67108864" in scoped
    assert unit == "hermes-worker-contract-test.scope"


@pytest.mark.parametrize("limits", [
    "LoadState=not-found\nMemoryMax=1024\nMemorySwapMax=0\nTasksMax=512\n",
    "LoadState=loaded\nMemoryMax=infinity\nMemorySwapMax=0\nTasksMax=512\n",
    "LoadState=loaded\nMemoryMax=1024\nMemorySwapMax=infinity\nTasksMax=512\n",
    "LoadState=loaded\nMemoryMax=1024\nMemorySwapMax=0\nTasksMax=infinity\n",
])
def test_missing_or_unbounded_aggregate_is_refused(monkeypatch, limits):
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr(workload.subprocess, "run", lambda *args, **kwargs: MagicMock(returncode=0, stdout=limits.encode()))
    with pytest.raises(workload.WorkloadScopeUnavailable):
        workload._require_bounded_slice()


def test_cancellation_stops_scope_after_group_signal(monkeypatch):
    calls = []
    proc = MagicMock(_hermes_systemd_unit="hermes-worker-contract.scope")
    monkeypatch.setattr(local, "_kill_process_group_posix", lambda target: calls.append(("group", target)))
    monkeypatch.setattr(registry, "_stop_systemd_unit", lambda unit: calls.append(("scope", unit)) or True)
    object.__new__(LocalEnvironment)._kill_process(proc)
    assert calls == [("group", proc), ("scope", "hermes-worker-contract.scope")]


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.live_system_guard_bypass
def test_scope_cleanup_reaps_double_forked_daemon(native_worker, tmp_path, force):
    import shlex
    import sys
    from tests.waiting import wait_until

    psutil = pytest.importorskip("psutil")
    if not registry._systemd_run_user_scope_available():
        pytest.skip("requires a usable systemd user manager")
    try:
        workload._require_bounded_slice()
    except workload.WorkloadScopeUnavailable:
        pytest.skip("requires a bounded native workload slice")
    marker = tmp_path / "daemon.pid"
    code = (
        "import os, sys, time\n"
        "if os.fork() == 0:\n"
        " os.setsid()\n"
        " if os.fork(): os._exit(0)\n"
        " with open(sys.argv[1], 'w', encoding='utf-8') as file: file.write(str(os.getpid()))\n"
        " time.sleep(30)\n"
        " os._exit(0)\n"
        "time.sleep(30)\n"
    )
    env = object.__new__(LocalEnvironment)
    env.cwd, env.env = str(tmp_path), {}
    proc = env._run_bash(f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} {shlex.quote(str(marker))}")
    daemon = None
    try:
        assert wait_until(lambda: marker.exists() and marker.stat().st_size, timeout=10)
        daemon = psutil.Process(int(marker.read_text(encoding="utf-8")))
        wait_until(lambda: daemon.pid not in [child.pid for child in psutil.Process(proc.pid).children(recursive=True)])
        (env._force_kill_process if force else env._kill_process)(proc)
        assert wait_until(lambda: not daemon.is_running() or daemon.status() == psutil.STATUS_ZOMBIE, timeout=10)
    finally:
        env._kill_process(proc)
        if daemon is not None and daemon.is_running() and daemon.status() != psutil.STATUS_ZOMBIE:
            daemon.kill()
        proc.communicate(timeout=5)
