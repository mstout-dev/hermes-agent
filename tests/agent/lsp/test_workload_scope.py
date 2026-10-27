"""Behavioral regressions for LSP workload scope ownership."""
from __future__ import annotations

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

    def stop(scope_owner, *, force=False):
        stopped.append((scope_owner._hermes_systemd_unit, force))
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
        lambda owner, *, force=False: stopped.append(owner._hermes_systemd_unit) or True,
    )

    await client._cleanup_process()

    assert stopped == ["hermes-worker-lsp-natural.scope"]
