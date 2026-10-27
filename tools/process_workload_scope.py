"""Bound native local terminal jobs outside the gateway's control-plane cgroup."""

import logging
import os
import shutil
import subprocess
import uuid

logger = logging.getLogger(__name__)

WORKLOAD_SLICE = "hermes-workloads.slice"
_AGGREGATE_LIMITS = ("MemoryMax", "MemorySwapMax", "TasksMax")


class WorkloadScopeUnavailable(RuntimeError):
    """The local command was refused before execution because isolation is unavailable."""


def _local_config() -> dict:
    from hermes_cli.config import load_config

    return ((load_config() or {}).get("terminal") or {}).get("local") or {}


def required_workload_scope() -> bool:
    """Native systemd gateway and managed compute children must never run unbounded jobs.

    INVOCATION_ID alone is inherited by unrelated interactive CLIs. Worker registration
    and the existing scheduler ownership markers cover compute children without claiming
    that they own the gateway PID file. Other Linux callers can opt in through config.
    """
    from agent.runtime_session_store import is_worker_process
    from tools import process_registry as registry

    if not registry._IS_LINUX:
        return False
    if os.environ.get("INVOCATION_ID") and (
        registry._is_supervised_gateway_process() or is_worker_process()
        or os.environ.get("_HERMES_CRON_EXTERNAL_WORKER")
        or os.environ.get("HERMES_KANBAN_RUN_ID")
    ):
        return True
    return _local_config().get("require_workload_scope") is True


def _require_bounded_slice() -> None:
    """Reject missing/unbounded slices: systemd would otherwise create one implicitly."""
    from tools import process_registry as registry

    binary = shutil.which("systemctl")
    if binary is None:
        raise WorkloadScopeUnavailable("local terminal requires systemctl and a bounded workload slice")
    try:
        result = subprocess.run(
            [binary, "--user", "show", WORKLOAD_SLICE, "--property=LoadState",
             *(f"--property={name}" for name in _AGGREGATE_LIMITS)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=3,
            env=registry.systemd_user_bus_env(), check=False,
        )
        values = dict(line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line)
        bounded = result.returncode == 0 and values.get("LoadState") == "loaded"
        for name in _AGGREGATE_LIMITS:
            raw = values.get(name, "")
            bounded = bounded and raw.isdigit()
            if name != "MemorySwapMax":
                bounded = bounded and raw.isdigit() and int(raw) > 0
        if bounded:
            return
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError) as exc:
        raise WorkloadScopeUnavailable(f"cannot verify bounded {WORKLOAD_SLICE}") from exc
    raise WorkloadScopeUnavailable(
        f"local terminal requires loaded {WORKLOAD_SLICE} with finite memory, swap and task limits"
    )


def local_workload_argv(
    argv: list[str], unit_suffix: str | None = None, *, required: bool = False,
) -> tuple[list[str], str]:
    """Return the unchanged command or its required bounded scope and owned unit name."""
    from tools import process_registry as registry

    if not registry._IS_LINUX or not (required or required_workload_scope()):
        return argv, ""
    if not registry._systemd_run_user_scope_available():
        raise WorkloadScopeUnavailable("local terminal requires a reachable systemd user scope; command not started")
    _require_bounded_slice()
    suffix = unit_suffix or f"foreground-{uuid.uuid4().hex[:12]}"
    scoped = registry._build_systemd_scope_argv(argv, unit_suffix=suffix)
    if scoped == argv:
        raise WorkloadScopeUnavailable("systemd-run disappeared; local command not started")
    maximum = registry._worker_memory_max_bytes()
    configured = _local_config().get("memory_max_mb")
    if configured is not None:
        if isinstance(configured, bool) or not isinstance(configured, int) or configured < 64:
            raise WorkloadScopeUnavailable("terminal.local.memory_max_mb must be an integer >= 64")
        maximum = min(maximum, configured * 1024**2)
    memory_index = next(i for i, value in enumerate(scoped) if value.startswith("MemoryMax="))
    scoped[memory_index] = f"MemoryMax={maximum}"
    index = scoped.index("--")
    scoped[index:index] = ["--slice", WORKLOAD_SLICE, "--property", "MemorySwapMax=0",
                           "--property", "TasksMax=256"]
    unit = f"hermes-worker-{suffix}.scope"
    logger.debug("Local terminal isolated in %s under %s (MemoryMax=%d)", unit, WORKLOAD_SLICE, maximum)
    return scoped, unit


def stop_foreground_scope(proc=None, *, unit: str = "", force: bool = False) -> bool:
    """Stop even daemonized descendants after the caller signals the parent/group."""
    from tools import process_registry as registry

    unit = unit or getattr(proc, "_hermes_systemd_unit", "")
    if not unit:
        return True
    if not force:
        return registry._stop_systemd_unit(unit)
    binary = shutil.which("systemctl")
    if binary is None:
        return False
    try:
        result = subprocess.run(
            [binary, "--user", "kill", "--signal=KILL", "--kill-whom=all", unit],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=0.25,
            env=registry.systemd_user_bus_env(), check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
