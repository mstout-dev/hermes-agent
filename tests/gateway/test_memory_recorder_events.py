from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway import memory_recorder as recorder
from gateway import memory_recorder_store as store_module


def _sample(monotonic: float, *, boot_id: str = "boot-a", events: int | None = 0, kill_events: int | None = 0, group_identity: dict | None = None) -> dict:
    return {
        "schema_version": 1,
        "boot_id": boot_id,
        "sampled_at": f"sample-{monotonic}",
        "monotonic_seconds": monotonic,
        "host": {"memory": {"available_kib": 8_000_000}, "swaps": {"used_kib": 0}},
        "processes": [],
        "cgroups": [{
            "name": "worker-scope",
            "identity": group_identity or {"boot_id": boot_id, "path": "/user.slice/worker.scope", "inode": 41},
            "oom_events": events,
            "oom_kill_events": kill_events,
            "pids_current": 0,
            "pids_max": None,
            "memory_current_bytes": 0,
            "swap_current_bytes": 0,
        }],
    }


def test_oom_counter_increase_on_healthy_host_creates_durable_incident(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_module, "_filesystem_type", lambda _path: "ext4")
    samples = iter([
        _sample(1, events=2, kill_events=1),
        _sample(3, events=3, kill_events=2),
        _sample(5, events=3, kill_events=2),
    ])
    monkeypatch.setattr(recorder, "collect_sample", lambda **_kwargs: next(samples))

    assert recorder.run(tmp_path / "recorder", interval=2, samples=3) == 0

    incidents = list((tmp_path / "recorder").glob("incident-*.jsonl"))
    assert len(incidents) == 1
    event = json.loads(incidents[0].read_text().splitlines()[0])
    assert set(event["reasons"]) >= {"cgroup_oom_event", "cgroup_oom_kill_event"}
    assert event["cgroup_event_deltas"] == [{
        "identity": {"boot_id": "boot-a", "path": "/user.slice/worker.scope", "inode": 41},
        "oom_delta": 1,
        "oom_kill_delta": 1,
        "pids_max_delta": 0,
    }]


def test_cgroup_sample_captures_task_limits_and_identity_even_without_processes(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    sys_root = tmp_path / "sys"
    (proc_root / "sys/kernel/random").mkdir(parents=True)
    (proc_root / "sys/kernel/random/boot_id").write_text("boot-cgroup\n")
    (proc_root / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 900 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    (proc_root / "swaps").write_text("Filename Type Size Used Priority\n")
    (proc_root / "pressure").mkdir()
    (proc_root / "pressure/memory").write_text("")
    group = sys_root / "fs/cgroup/user.slice"
    group.mkdir(parents=True)
    (group / "memory.current").write_text("10\n")
    (group / "memory.swap.current").write_text("0\n")
    (group / "memory.stat").write_text("anon 10\n")
    (group / "memory.events").write_text("oom 4\noom_kill 1\n")
    (group / "pids.current").write_text("3\n")
    (group / "pids.max").write_text("64\n")

    sample = recorder.collect_sample(proc_root, sys_root, now_monotonic=7)

    row = next(row for row in sample["cgroups"] if row["name"] == recorder._safe_label("/user.slice"))
    assert row["pids_current"] == 3
    assert row["pids_max"] == 64
    assert row["identity"] == {"boot_id": "boot-cgroup", "path": "/user.slice", "inode": group.stat().st_ino}
    assert row["oom_events"] == 4


def test_missing_counter_is_null_and_identity_change_establishes_new_baseline() -> None:
    previous = _sample(1, events=2, kill_events=None)
    missing = _sample(3, events=None, kill_events=None)
    recreated = _sample(5, events=9, kill_events=4, group_identity={"boot_id": "boot-a", "path": "/user.slice/worker.scope", "inode": 99})

    assert recorder.classify_pressure(missing, previous) == []
    assert recorder.classify_pressure(recreated, previous) == []


def test_counter_reset_or_reboot_does_not_create_a_new_event() -> None:
    previous = _sample(1, boot_id="boot-a", events=8, kill_events=2)
    reset = _sample(3, boot_id="boot-a", events=0, kill_events=0)
    reboot = _sample(3, boot_id="boot-b", events=9, kill_events=3)

    assert recorder.classify_pressure(reset, previous) == []
    assert recorder.classify_pressure(reboot, previous) == []
