from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch


def _task() -> kb.Task:
    return kb.Task(
        id="t_import_isolation",
        title="import isolation",
        body=None,
        assignee="coder",
        status="ready",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )


def _run_worker_from_workspace(
    workspace: Path, hermes_home: Path
) -> subprocess.CompletedProcess[str]:
    repo_root = Path(dispatch.__file__).resolve().parents[1]
    env = {
        "HOME": str(hermes_home.parent),
        "HERMES_HOME": str(hermes_home),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(repo_root),
    }
    return subprocess.run(
        dispatch._worker_argv(_task(), "coder", str(hermes_home)),
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _assert_real_worker_bootstrapped(
    result: subprocess.CompletedProcess[str], marker: Path
) -> None:
    assert not marker.exists(), (
        f"task helper ran during worker bootstrap: {marker.read_text()}"
    )
    assert "[kanban-worker-exit] rc=1" in result.stderr, result.stderr
    assert result.returncode == 1


def test_worker_bootstrap_does_not_import_task_inspect(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hermes_home = tmp_path / "home" / ".hermes"
    hermes_home.mkdir(parents=True)
    marker = tmp_path / "task_helper_imported"

    (workspace / "inspect.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('inspect')\n"
        "raise RuntimeError('task inspect.py imported')\n",
        encoding="utf-8",
    )
    result = _run_worker_from_workspace(workspace, hermes_home)
    _assert_real_worker_bootstrapped(result, marker)


def test_worker_bootstrap_does_not_import_task_hermes_cli_package(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hermes_home = tmp_path / "home" / ".hermes"
    hermes_home.mkdir(parents=True)
    marker = tmp_path / "task_helper_imported"
    local_package = workspace / "hermes_cli"
    local_package.mkdir()
    (local_package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('hermes_cli')\n",
        encoding="utf-8",
    )
    (local_package / "kanban_worker_client.py").write_text(
        "raise SystemExit('task lookalike worker ran')\n", encoding="utf-8"
    )

    result = _run_worker_from_workspace(workspace, hermes_home)

    _assert_real_worker_bootstrapped(result, marker)


def test_normal_task_python_still_imports_workspace_helpers(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    helper = workspace / "task_helper.py"
    helper.write_text("print('task helper imported')\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-c", "import task_helper"],
        cwd=workspace,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": ""},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "task helper imported"
