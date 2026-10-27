#!/usr/bin/env python3
"""Run foreground administration commands with owned, disk-backed native temp space."""

import argparse
import contextlib
import importlib.util
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time

INSTALLATION = Path(__file__).resolve().parent.parent
_POLL_INTERVAL = 0.1
_STOP_GRACE = 2.0


def require_disk(path):
    existing = Path(path).absolute()
    while not existing.exists():
        existing = existing.parent
    result = subprocess.run(
        ["findmnt", "--noheadings", "--output", "FSTYPE", "--target", str(existing)],
        check=True, capture_output=True, text=True, timeout=3,
    )
    filesystem = result.stdout.strip()
    if not filesystem or filesystem in {"tmpfs", "ramfs", "devtmpfs"}:
        raise ValueError("large run output requires a verified disk-backed filesystem")


def scratch_root():
    sys.path.insert(0, str(INSTALLATION / "hermes-agent"))
    __import__("hermes_bootstrap")
    from hermes_constants import get_scratch_dir

    root = get_scratch_dir(prune=False)
    require_disk(root)
    return root


def authority():
    sys.path.insert(0, str(INSTALLATION / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "hermes_workspace_authority", INSTALLATION / "scripts/hermes-workspaces.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Authority(), module.Hold


@contextlib.contextmanager
def temporary_run(prefix="admin-", *, state=None):
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=scratch_root()))
    identity = (path.stat().st_dev, path.stat().st_ino)
    try:
        yield path
    finally:
        if state and state.get("retain"):
            print(
                f"Retained temporary output at {path}: owned workload cleanup was not confirmed; "
                "check ownership and live use before cleanup.", file=sys.stderr,
            )
        else:
            hold_error = RuntimeError
            try:
                info = path.lstat()
                if path.is_symlink() or (info.st_dev, info.st_ino) != identity:
                    raise ValueError("temporary directory identity changed")
                guard, hold_error = authority()
                guard.native(path)
                guard.processes(path)
                shutil.rmtree(path)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, hold_error) as exc:
                print(
                    f"Retained temporary output at {path}: {exc}; "
                    "check ownership and live use before cleanup.", file=sys.stderr,
                )


def run_environment(path):
    return {**os.environ, **{key: str(path) for key in ("TMPDIR", "TMP", "TEMP")}}


def _signal_process_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def _finish_process(process, unit, stop_foreground_scope, state):
    _signal_process_group(process, signal.SIGTERM)
    scoped = stop_foreground_scope(process)
    force_stopped = True
    if not scoped and unit:
        force_stopped = stop_foreground_scope(process, force=True)
    deadline = time.monotonic() + _STOP_GRACE
    while process.poll() is None and time.monotonic() < deadline:
        try:
            process.wait(timeout=_POLL_INTERVAL)
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is None:
        if unit:
            force_stopped = stop_foreground_scope(process, force=True) and force_stopped
        else:
            _signal_process_group(process, signal.SIGKILL)
    if not force_stopped:
        state["retain"] = True
        raise RuntimeError(f"Could not stop owned workload scope {unit}")
    try:
        process.wait(timeout=_STOP_GRACE)
    except subprocess.TimeoutExpired as exc:
        state["retain"] = True
        raise RuntimeError(f"Could not reap owned process {process.pid}") from exc
    return scoped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("provide a foreground command after --")
    require_disk(args.cwd)
    pending_signal = None
    cleanup_state = {"retain": False}
    previous_handlers = {}

    def request_cancel(signum, _frame):
        nonlocal pending_signal
        if pending_signal is None:
            pending_signal = signum

    watched_signals = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    for signum in watched_signals:
        previous_handlers[signum] = signal.signal(signum, request_cancel)
    try:
        with temporary_run(state=cleanup_state) as temporary:
            from tools.process_registry import systemd_user_bus_env
            from tools.process_workload_scope import local_workload_argv, stop_foreground_scope

            argv, unit = local_workload_argv(command, required=True)
            if pending_signal is not None:
                return 128 + pending_signal
            process = subprocess.Popen(
                argv, cwd=args.cwd, env=systemd_user_bus_env(run_environment(temporary)),
                start_new_session=True,
            )
            process._hermes_systemd_unit = unit
            try:
                if pending_signal is None:
                    while pending_signal is None:
                        try:
                            result = process.wait(timeout=_POLL_INTERVAL)
                            break
                        except subprocess.TimeoutExpired:
                            continue
            except BaseException:
                _finish_process(process, unit, stop_foreground_scope, cleanup_state)
                raise
            if pending_signal is not None:
                _finish_process(process, unit, stop_foreground_scope, cleanup_state)
                return 128 + pending_signal
            if not stop_foreground_scope(process):
                _finish_process(process, unit, stop_foreground_scope, cleanup_state)
                raise RuntimeError(f"Could not close owned workload scope {unit}")
            return result
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
