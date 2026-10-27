"""Local terminal memory stays contained by the aggregate workload slice."""

from unittest.mock import MagicMock

import pytest

from tools import process_registry as registry
from tools import process_workload_scope as workload

pytestmark = pytest.mark.platforms("linux")


def test_bounded_slice_does_not_require_a_task_cap(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/systemctl")
    output = b"LoadState=loaded\nMemoryMax=17179869184\nMemorySwapMax=2147483648\nTasksMax=infinity\n"
    monkeypatch.setattr(
        workload.subprocess, "run",
        lambda *args, **kwargs: MagicMock(returncode=0, stdout=output),
    )

    workload._require_bounded_slice()


def test_worker_memory_helper_keeps_its_existing_default(monkeypatch):
    monkeypatch.setattr(registry.Path, "read_text", lambda *args, **kwargs: "")
    monkeypatch.setattr(registry.os, "sysconf", lambda name: {
        "SC_PHYS_PAGES": 16 * 1024**3 // 4096,
        "SC_PAGE_SIZE": 4096,
    }[name])

    assert registry._worker_memory_max_bytes() == 4 * 1024**3


def test_worker_memory_helper_can_use_a_caller_supplied_ceiling(monkeypatch):
    monkeypatch.setattr(registry.Path, "read_text", lambda *args, **kwargs: "")
    monkeypatch.setattr(registry.os, "sysconf", lambda name: {
        "SC_PHYS_PAGES": 32 * 1024**3 // 4096,
        "SC_PAGE_SIZE": 4096,
    }[name])

    assert registry._worker_memory_max_bytes(ceiling_bytes=8 * 1024**3) == 8 * 1024**3


def test_local_command_budget_can_exceed_worker_default_but_not_tighter_bounds(monkeypatch):
    monkeypatch.setattr(registry, "_worker_memory_max_bytes", lambda **kwargs: 8 * 1024**3)
    monkeypatch.setattr(registry, "_systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr(workload, "_require_bounded_slice", lambda: 16 * 1024**3)
    monkeypatch.setattr(workload, "_local_config", dict)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/systemd-run")
    monkeypatch.setattr(registry, "_SYSTEMD_RUN_NO_EXPAND", True)

    scoped, _unit = workload.local_workload_argv(["/bin/true"], required=True)
    memory = next(value for value in scoped if value.startswith("MemoryMax="))
    assert memory == f"MemoryMax={8 * 1024**3}"

    monkeypatch.setattr(registry, "_worker_memory_max_bytes", lambda **kwargs: 3 * 1024**3)
    scoped, _unit = workload.local_workload_argv(["/bin/true"], required=True)
    memory = next(value for value in scoped if value.startswith("MemoryMax="))
    assert memory == f"MemoryMax={3 * 1024**3}"


def test_local_memory_setting_can_only_tighten_budget(monkeypatch):
    monkeypatch.setattr(registry, "_worker_memory_max_bytes", lambda **kwargs: 8 * 1024**3)
    monkeypatch.setattr(registry, "_systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr(workload, "_require_bounded_slice", lambda: 16 * 1024**3)
    monkeypatch.setattr(workload, "_local_config", lambda: {"memory_max_mb": 1024})
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/systemd-run")
    monkeypatch.setattr(registry, "_SYSTEMD_RUN_NO_EXPAND", True)

    scoped, _unit = workload.local_workload_argv(["/bin/true"], required=True)
    memory = next(value for value in scoped if value.startswith("MemoryMax="))
    assert memory == f"MemoryMax={1024**3}"
