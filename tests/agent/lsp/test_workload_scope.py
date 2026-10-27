"""Behavioral regressions for LSP workload scope ownership."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.lsp.client import LSPClient
from agent.lsp.protocol import LSPProtocolError
from tools import process_workload_scope


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
async def test_required_scope_unavailable_refuses_to_spawn(monkeypatch, tmp_path):
    monkeypatch.setattr(process_workload_scope, "required_workload_scope", lambda: True)
    monkeypatch.setattr(
        process_workload_scope,
        "local_workload_argv",
        lambda argv, **kwargs: (_ for _ in ()).throw(
            process_workload_scope.WorkloadScopeUnavailable("scope unavailable")
        ),
    )
    spawn = AsyncMock(side_effect=AssertionError("server spawn was reached"))
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)

    client = LSPClient(server_id="required", workspace_root=str(tmp_path), command=["server"])
    with pytest.raises(LSPProtocolError, match="workload scope"):
        await client._spawn()

    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_stops_owned_scope_when_process_reference_is_gone(monkeypatch, tmp_path):
    client = LSPClient(server_id="exited", workspace_root=str(tmp_path), command=["server"])
    client._scope_unit = "hermes-worker-lsp-test.scope"
    stopped = []

    def stop(proc=None, *, unit="", force=False):
        stopped.append((unit, force))
        return True

    monkeypatch.setattr(process_workload_scope, "stop_foreground_scope", stop)
    await client._cleanup_process()

    assert stopped == [("hermes-worker-lsp-test.scope", False)]


@pytest.mark.asyncio
async def test_cleanup_stops_owned_scope_after_launcher_natural_exit(monkeypatch, tmp_path):
    client = LSPClient(server_id="natural-exit", workspace_root=str(tmp_path), command=["server"])
    client._scope_unit = "hermes-worker-lsp-natural.scope"
    client._proc = SimpleNamespace(returncode=0, _hermes_systemd_unit=client._scope_unit)
    stopped = []
    monkeypatch.setattr(
        process_workload_scope,
        "stop_foreground_scope",
        lambda proc=None, *, unit="", force=False: stopped.append(unit) or True,
    )

    await client._cleanup_process()

    assert stopped == ["hermes-worker-lsp-natural.scope"]


@pytest.mark.platforms("linux")
@pytest.mark.asyncio
async def test_cancelled_spawn_waits_for_scope_registration_before_cleanup(monkeypatch, tmp_path):
    admitted = asyncio.Event()
    release_spawn = asyncio.Event()
    stopped = []

    monkeypatch.setattr(
        process_workload_scope,
        "local_workload_argv",
        lambda argv, **kwargs: (["systemd-run", "--scope", *argv], "hermes-worker-lsp-deferred.scope"),
    )

    async def deferred_spawn(*args, **kwargs):
        admitted.set()
        await release_spawn.wait()

        async def wait():
            return 0

        return SimpleNamespace(returncode=0, wait=wait)

    monkeypatch.setattr("asyncio.create_subprocess_exec", deferred_spawn)
    monkeypatch.setattr(
        process_workload_scope,
        "stop_foreground_scope",
        lambda proc=None, *, unit="", force=False: stopped.append(unit) or True,
    )
    client = LSPClient(server_id="deferred", workspace_root=str(tmp_path), command=["server"])
    start = asyncio.create_task(client.start())
    await admitted.wait()
    start.cancel()

    assert not start.done()
    release_spawn.set()
    with pytest.raises(asyncio.CancelledError):
        await start

    assert stopped == ["hermes-worker-lsp-deferred.scope"]


@pytest.mark.asyncio
async def test_completed_spawn_cancellation_still_reaps_process(monkeypatch, tmp_path):
    killed = []
    process = SimpleNamespace(returncode=None, pid=42)

    def kill():
        killed.append(True)
        process.returncode = -9

    async def wait():
        return process.returncode

    process.kill, process.wait = kill, wait
    start = None

    async def completed_spawn(*args, **kwargs):
        asyncio.get_running_loop().call_soon(start.cancel)
        return process

    monkeypatch.setattr(process_workload_scope, "local_workload_argv", lambda argv, **kwargs: (argv, ""))
    monkeypatch.setattr("asyncio.create_subprocess_exec", completed_spawn)
    monkeypatch.setattr("agent.deadline.kill_process_tree", lambda pid: False)
    client = LSPClient(server_id="completed", workspace_root=str(tmp_path), command=["server"])
    start = asyncio.create_task(client.start())
    with pytest.raises(asyncio.CancelledError):
        await start
    assert killed == [True]


@pytest.mark.asyncio
async def test_process_cleanup_error_still_stops_scope(monkeypatch, tmp_path):
    client = LSPClient(server_id="cleanup-error", workspace_root=str(tmp_path), command=["server"])
    client._scope_unit = "hermes-worker-lsp-error.scope"
    client._proc = SimpleNamespace(returncode=None, pid=42)
    stopped = []

    def fail_tree_cleanup(pid):
        raise PermissionError("synthetic process cleanup failure")

    monkeypatch.setattr("agent.deadline.kill_process_tree", fail_tree_cleanup)
    monkeypatch.setattr(process_workload_scope, "stop_foreground_scope", lambda **kwargs: stopped.append(kwargs["unit"]) or True)
    with pytest.raises(PermissionError):
        await client._cleanup_process()
    assert stopped == ["hermes-worker-lsp-error.scope"]


@pytest.mark.asyncio
async def test_failed_scope_cleanup_can_be_retried(monkeypatch, tmp_path):
    client = LSPClient(server_id="cleanup-retry", workspace_root=str(tmp_path), command=["server"])
    client._scope_unit = "hermes-worker-lsp-retry.scope"
    stopped = []
    monkeypatch.setattr(process_workload_scope, "stop_foreground_scope", lambda **kwargs: stopped.append(kwargs) or False)
    await client._cleanup_process()
    await client._cleanup_process()
    assert len(stopped) == 4
