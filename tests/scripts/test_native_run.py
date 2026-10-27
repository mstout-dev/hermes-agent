import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from tests.waiting import wait_until


REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "contrib/native/hermes_run.py"


def _write_wrapper(tmp_path, command, *, cancel_after_spawn=False, gate=None, daemon_pid=None):
    scratch = tmp_path / "scratch"
    scratch.mkdir(parents=True)
    ready = tmp_path / "ready"
    stopped = tmp_path / "stopped"
    entered = tmp_path / "stop-entered"
    wrapper = tmp_path / "launch.py"
    wrapper.write_text(
        "import importlib.util, os, signal, subprocess, sys\n"
        "from pathlib import Path\n"
        f"spec = importlib.util.spec_from_file_location('native_run', {str(RUNNER)!r})\n"
        "runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)\n"
        f"scratch = Path({str(scratch)!r})\n"
        "runner.require_disk = lambda path: None\n"
        "runner.scratch_root = lambda: scratch\n"
        "class Guard:\n"
        "    def native(self, path): pass\n"
        "    def processes(self, path): pass\n"
        "runner.authority = lambda: (Guard(), RuntimeError)\n"
        "from tools import process_registry, process_workload_scope\n"
        "process_registry.systemd_user_bus_env = lambda env: env\n"
        "process_workload_scope.local_workload_argv = lambda command, **kwargs: (command, 'owned-test.scope')\n"
        f"stopped = Path({str(stopped)!r})\n"
        f"entered = Path({str(entered)!r})\n"
        f"gate = {str(gate) if gate else None!r}\n"
        f"daemon_pid = {str(daemon_pid) if daemon_pid else None!r}\n"
        "def stop(proc=None, **kwargs):\n"
        "    stopped.touch()\n"
        "    if gate and not kwargs.get('force'):\n"
        "        entered.touch(); open(gate).read(1)\n"
        "    if daemon_pid and Path(daemon_pid).exists():\n"
        "        pid = int(Path(daemon_pid).read_text()); os.kill(pid, signal.SIGTERM)\n"
        "    try: os.killpg(proc.pid, signal.SIGKILL)\n"
        "    except ProcessLookupError: pass\n"
        "    return True\n"
        "process_workload_scope.stop_foreground_scope = stop\n"
        f"sys.argv = ['hermes_run.py', '--cwd', '.', '--', *{command!r}]\n"
        "if " + repr(cancel_after_spawn) + ":\n"
        "    original = runner.subprocess.Popen\n"
        "    def launch(*args, **kwargs):\n"
        "        proc = original(*args, **kwargs); os.kill(os.getpid(), signal.SIGTERM); return proc\n"
        "    runner.subprocess.Popen = launch\n"
        "raise SystemExit(runner.main())\n",
        encoding="utf-8",
    )
    env = {**os.environ, "HERMES_HOME": str(tmp_path / "hermes-home"), "PYTHONPATH": str(REPO)}
    process = subprocess.Popen([sys.executable, str(wrapper)], cwd=REPO, env=env)
    return process, ready, stopped, entered, scratch


def _stubborn_child(ready):
    return [sys.executable, "-c",
            f"import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"open({str(ready)!r}, 'w').close(); time.sleep(60)"]


@pytest.mark.platforms("posix")
def test_term_stops_owned_child_and_releases_run_directory(tmp_path):
    ready = tmp_path / "ready"
    process, ready, stopped, _entered, scratch = _write_wrapper(
        tmp_path, _stubborn_child(ready)
    )
    try:
        wait_until(ready.exists, timeout=10)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 128 + signal.SIGTERM
        assert stopped.exists()
        assert list(scratch.iterdir()) == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


@pytest.mark.platforms("posix")
def test_repeated_signal_does_not_interrupt_cleanup(tmp_path):
    ready = tmp_path / "ready"
    gate = tmp_path / "cleanup-gate"
    os.mkfifo(gate)
    process, ready, _stopped, entered, scratch = _write_wrapper(
        tmp_path, _stubborn_child(ready), gate=gate
    )
    try:
        wait_until(ready.exists, timeout=10)
        process.send_signal(signal.SIGTERM)
        wait_until(entered.exists, timeout=10)
        process.send_signal(signal.SIGINT)
        with gate.open("w", encoding="utf-8") as release:
            release.write("x")
        assert process.wait(timeout=10) == 128 + signal.SIGTERM
        assert list(scratch.iterdir()) == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


@pytest.mark.platforms("posix")
def test_cancel_immediately_after_spawn_closes_late_process(tmp_path):
    ready = tmp_path / "ready"
    process, _ready, stopped, _entered, scratch = _write_wrapper(
        tmp_path, _stubborn_child(ready), cancel_after_spawn=True
    )
    assert process.wait(timeout=10) == 128 + signal.SIGTERM
    assert stopped.exists()
    assert list(scratch.iterdir()) == []


@pytest.mark.platforms("posix")
def test_owned_scope_stops_daemonized_descendant(tmp_path):
    ready = tmp_path / "ready"
    daemon_pid = tmp_path / "daemon.pid"
    command = [
        sys.executable, "-c",
        f"import os, signal, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', \"import os, signal, time; "
        f"signal.signal(signal.SIGTERM, lambda *_: (open({str(tmp_path / 'daemon-exit')!r}, 'w').close(), sys.exit(0))); "
        f"open({str(ready)!r}, 'w').close(); time.sleep(60)\"], start_new_session=True); "
        f"open({str(daemon_pid)!r}, 'w').write(str(child.pid)); time.sleep(60)",
    ]
    process, _ready, stopped, _entered, scratch = _write_wrapper(
        tmp_path, command, daemon_pid=daemon_pid
    )
    try:
        wait_until(ready.exists, timeout=10)
        wait_until(daemon_pid.exists, timeout=10)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 128 + signal.SIGTERM
        assert (tmp_path / "daemon-exit").exists()
        assert stopped.exists()
        assert list(scratch.iterdir()) == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


@pytest.mark.platforms("posix")
def test_success_failure_and_literal_argv_are_preserved(tmp_path):
    output = tmp_path / "argv.json"
    args = ["two words", "", "$(literal)"]
    command = [sys.executable, "-c",
               f"import json, sys; open({str(output)!r}, 'w').write(json.dumps(sys.argv[1:]))",
               *args]
    process, _ready, _stopped, _entered, scratch = _write_wrapper(tmp_path, command)
    assert process.wait(timeout=10) == 0
    assert json.loads(output.read_text()) == args
    assert list(scratch.iterdir()) == []

    failed, _ready, _stopped, _entered, scratch = _write_wrapper(
        tmp_path / "failure", [sys.executable, "-c", "raise SystemExit(7)"]
    )
    assert failed.wait(timeout=10) == 7
    assert list(scratch.iterdir()) == []
