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
            "pids_denied_events": 0,
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
    (group / "memory.events.local").write_text("oom 4\noom_kill 1\n")
    (group / "pids.events.local").write_text("max 2\n")
    (group / "pids.current").write_text("3\n")
    (group / "pids.max").write_text("64\n")

    sample = recorder.collect_sample(proc_root, sys_root, now_monotonic=7)

    row = next(row for row in sample["cgroups"] if row["name"] == recorder._safe_label("/user.slice"))
    assert row["pids_current"] == 3
    assert row["pids_max"] == 64
    assert row["identity"] == {"boot_id": "boot-cgroup", "path": "/user.slice", "inode": group.stat().st_ino}
    assert row["oom_events"] == 4
    assert row["pids_denied_events"] == 2


def test_short_lived_scope_oom_is_visible_through_workload_ancestor(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    sys_root = tmp_path / "sys"
    (proc_root / "sys/kernel/random").mkdir(parents=True)
    (proc_root / "sys/kernel/random/boot_id").write_text("boot-cgroup\n")
    (proc_root / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 900 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    (proc_root / "swaps").write_text("Filename Type Size Used Priority\n")
    (proc_root / "pressure").mkdir()
    (proc_root / "pressure/memory").write_text("")
    workload = Path(f"/user.slice/user-{Path.home().stat().st_uid}.slice/user@{Path.home().stat().st_uid}.service/hermes.slice/hermes-workloads.slice")
    base = sys_root / "fs/cgroup" / str(workload).lstrip("/")
    base.mkdir(parents=True)
    (base / "memory.events").write_text("oom 1\noom_kill 0\n")
    (base / "pids.events").write_text("max 0\n")

    before = recorder.collect_sample(proc_root, sys_root, now_monotonic=1)
    (base / "memory.events").write_text("oom 2\noom_kill 1\n")
    after = recorder.collect_sample(proc_root, sys_root, now_monotonic=3)

    assert any(row["identity"]["path"] == str(workload) and row["event_counter_scope"] == "hierarchical" for row in before["cgroups"])
    assert "cgroup_oom_event" in recorder.classify_pressure(after, before)
    assert "cgroup_oom_kill_event" in recorder.classify_pressure(after, before)


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


def test_pid_denials_trigger_and_missing_counter_reappearance_is_a_new_baseline() -> None:
    previous = _sample(1)
    previous["cgroups"][0]["pids_denied_events"] = 4
    denied = _sample(3)
    denied["cgroups"][0]["pids_denied_events"] = 5
    missing = _sample(5)
    missing["cgroups"][0]["pids_denied_events"] = None
    reappeared = _sample(7)
    reappeared["cgroups"][0]["pids_denied_events"] = 8

    assert "cgroup_pids_denied_event" in recorder.classify_pressure(denied, previous)
    assert recorder.classify_pressure(missing, denied) == []
    assert recorder.classify_pressure(reappeared, missing) == []


def test_workload_aggregate_and_child_counters_are_attributed_once() -> None:
    previous = _sample(1)
    current = _sample(3)
    workload = "/user.slice/user-1000.slice/user@1000.service/hermes.slice/hermes-workloads.slice"
    aggregate = {"boot_id": "boot-a", "path": workload, "inode": 10}
    child = {"boot_id": "boot-a", "path": f"{workload}/worker.scope", "inode": 11}
    for sample, event_count in ((previous, 4), (current, 5)):
        sample["cgroups"] = [
            {"name": "workload", "identity": aggregate, "event_counter_scope": "hierarchical", "oom_events": event_count, "oom_kill_events": 0, "pids_denied_events": 0, "memory_current_bytes": 0, "swap_current_bytes": 0},
            {"name": "worker", "identity": child, "event_counter_scope": "local", "oom_events": event_count - 4, "oom_kill_events": 0, "pids_denied_events": 0, "memory_current_bytes": 0, "swap_current_bytes": 0},
        ]

    event = store_module.RecorderStore._event_record(current, previous, ["cgroup_oom_event"], 3)

    assert event["cgroup_event_deltas"] == [{
        "identity": aggregate,
        "oom_delta": 1,
        "oom_kill_delta": 0,
        "pids_max_delta": 0,
    }]


@pytest.mark.parametrize("aggregate_count", [None, 0, 4])
def test_valid_child_counter_survives_missing_reset_or_unchanged_aggregate(aggregate_count):
    previous = _sample(1)
    current = _sample(3)
    root = "/user.slice/workloads.slice"
    for sample, count in ((previous, 4), (current, aggregate_count)):
        sample["cgroups"] = [
            {"identity": {"boot_id": "boot-a", "path": root, "inode": 1}, "event_counter_scope": "hierarchical", "oom_events": count},
            {"identity": {"boot_id": "boot-a", "path": root + "/child.scope", "inode": 2}, "oom_events": 8 if sample is previous else 9},
        ]
    delta = store_module._cgroup_event_deltas(current, previous)
    assert delta == [{"identity": {"boot_id": "boot-a", "path": root + "/child.scope", "inode": 2}, "oom_delta": 1, "oom_kill_delta": 0, "pids_max_delta": 0}]


def test_bounded_header_keeps_kill_evidence_and_counts_omitted_events():
    previous = _sample(1)
    current = _sample(3)
    for sample in (previous, current):
        template = sample["cgroups"][0]
        sample["cgroups"] = [{**template, "identity": {"boot_id": "boot-a", "path": f"/scope-{index}", "inode": index}, "pids_denied_events": int(sample is current)} for index in range(17)]
    current["cgroups"][-1]["oom_kill_events"] = 1
    header = store_module.RecorderStore._event_record(current, previous, ["cgroup_oom_kill_event", "cgroup_pids_denied_event"], 3)
    assert len(header["cgroup_event_deltas"]) == 16
    assert header["cgroup_event_deltas"][0]["oom_kill_delta"] == 1
    assert header["omitted_cgroup_event_deltas"] == 1
