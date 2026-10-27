from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from gateway import memory_recorder as recorder
from gateway import memory_recorder_store as store_module


def _proc_tree(root: Path, *, pid: int = 123, start: int = 7, rss: int = 10, swap: int = 0, available: int = 8_000_000, swap_used: int = 0) -> tuple[Path, Path]:
    proc = root / "proc"
    sys_root = root / "sys"
    process = proc / str(pid)
    process.mkdir(parents=True)
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("boot-test\n")
    (proc / "meminfo").write_text(f"MemTotal: 16000000 kB\nMemAvailable: {available} kB\nSwapTotal: 4000000 kB\nSwapFree: {4000000 - swap_used} kB\n")
    (proc / "swaps").write_text(f"Filename Type Size Used Priority\n/dev/zram0 partition 4000000 {swap_used} 100\n")
    (proc / "pressure").mkdir()
    (proc / "pressure/memory").write_text("some avg10=0.10 avg60=0.20 avg300=0.30 total=44\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    stat_fields = ["S", "1", *(["0"] * 18)]
    stat_fields[19] = str(start)
    (process / "stat").write_text(f"{pid} (worker with spaces) {' '.join(stat_fields)}\n")
    (process / "status").write_text(f"Name:\tworker\nPPid:\t1\nVmRSS:\t{rss} kB\nRssAnon:\t{rss // 2} kB\nRssFile:\t{rss // 3} kB\nRssShmem:\t{rss // 6} kB\nVmSwap:\t{swap} kB\n")
    (process / "io").write_text("read_bytes: 11\nwrite_bytes: 22\nwchar: 33\n")
    (process / "cgroup").write_text("0::/user.slice/test.scope\n")
    (process / "smaps_rollup").write_text("Pss: 9 kB\n")
    group = sys_root / "fs/cgroup/user.slice/test.scope"
    group.mkdir(parents=True)
    (group / "memory.current").write_text("1000\n")
    (group / "memory.swap.current").write_text("50\n")
    (group / "memory.stat").write_text("anon 800\nfile 200\nshmem 20\n")
    (group / "memory.events").write_text("oom 0\noom_kill 0\n")
    (sys_root / "block/zram0").mkdir(parents=True)
    (sys_root / "block/zram0/mm_stat").write_text("4096 2048 1024 0 0 0 0\n")
    return proc, sys_root


def test_collect_sample_keeps_safe_fields_and_rechecks_incarnation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc, sys_root = _proc_tree(tmp_path)
    monkeypatch.setattr(os, "readlink", lambda path: "/private/home/worker" if str(path).endswith("/exe") else "")
    sample = recorder.collect_sample(proc, sys_root, now_monotonic=12.5)
    process = sample["processes"][0]
    assert process["identity"] == {"boot_id": "boot-test", "pid": 123, "start_ticks": 7}
    assert process["comm"] == "worker with spaces"
    assert process["exe_basename"] == "worker"
    assert process["rss_anon_kib"] == 5
    assert process["io"] == {"read_bytes": 11, "write_bytes": 22, "wchar_bytes": 33}
    assert process["pss_kib"] == 9
    assert any(row["memory_current_bytes"] == 1000 for row in sample["cgroups"])
    assert sample["host"]["swaps"]["used_kib"] == 0
    assert sample["host"]["zram"][0]["physical_bytes"] == 1024
    encoded = json.dumps(sample)
    assert "/private/home" not in encoded
    assert "cmdline" not in encoded and "environ" not in encoded


def test_process_reuse_changes_identity_and_growth_uses_incarnation(tmp_path: Path) -> None:
    proc, sys_root = _proc_tree(tmp_path, start=7, rss=100)
    previous = recorder.collect_sample(proc, sys_root, now_monotonic=1)
    next_proc, _ = _proc_tree(tmp_path / "next", start=8, rss=600000, swap=500000)
    current = recorder.collect_sample(next_proc, sys_root, now_monotonic=3)
    assert previous["processes"][0]["identity"]["start_ticks"] != current["processes"][0]["identity"]["start_ticks"]
    assert "process_rss_swap_rise" in recorder.classify_pressure(current, previous)


def test_process_cap_keeps_heaviest_and_reports_omissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc, sys_root = _proc_tree(tmp_path, pid=123, rss=10)
    second = proc / "456"
    shutil.copytree(proc / "123", second)
    (second / "stat").write_text("456 (large) S 1 " + "0 " * 18 + "9\n")
    (second / "status").write_text("PPid:\t1\nVmRSS:\t200 kB\nVmSwap:\t400 kB\n")
    monkeypatch.setattr(recorder, "MAX_PROCESSES", 1)
    sample = recorder.collect_sample(proc, sys_root)
    assert [row["identity"]["pid"] for row in sample["processes"]] == [456]
    assert sample["omitted_processes"] == 1
    assert sample["truncated"] is True


def test_pressure_threshold_scales_with_sample_interval(tmp_path: Path) -> None:
    proc, sys_root = _proc_tree(tmp_path, available=1_000_000)
    previous = recorder.collect_sample(proc, sys_root, now_monotonic=0)
    current_proc, _ = _proc_tree(tmp_path / "current", available=1_000_000, swap_used=500_000)
    current = recorder.collect_sample(current_proc, sys_root, now_monotonic=4)
    reasons = recorder.classify_pressure(current, previous)
    assert "available_below_2gib" in reasons
    assert "global_swap_rise" not in reasons


def test_individual_process_growth_is_not_masked_by_another_exit(tmp_path: Path) -> None:
    proc, sys_root = _proc_tree(tmp_path, pid=123, rss=2_000_000)
    previous = recorder.collect_sample(proc, sys_root, now_monotonic=1)
    current_proc, _ = _proc_tree(tmp_path / "next", pid=456, rss=2_000_000)
    current = recorder.collect_sample(current_proc, sys_root, now_monotonic=3)
    assert "process_rss_swap_rise" in recorder.classify_pressure(current, previous)


def test_individual_cgroup_growth_and_missing_counters(tmp_path: Path) -> None:
    proc, sys_root = _proc_tree(tmp_path)
    previous = recorder.collect_sample(proc, sys_root, now_monotonic=1)
    current = recorder.collect_sample(proc, sys_root, now_monotonic=3)
    previous["cgroups"] = [
        {"name": "large", "memory_current_bytes": 2 * 1024**3, "swap_current_bytes": 0},
        {"name": "missing", "memory_current_bytes": None, "swap_current_bytes": None},
    ]
    current["cgroups"] = [
        {"name": "growing", "memory_current_bytes": 2 * 1024**3, "swap_current_bytes": 0},
        {"name": "missing", "memory_current_bytes": None, "swap_current_bytes": None},
    ]
    assert "cgroup_memory_swap_rise" in recorder.classify_pressure(current, previous)


def test_incident_state_keeps_a_fixed_tail_and_rearm_cooldown() -> None:
    state = recorder.IncidentState(None, 0)
    assert state.observe(1, True) == "start"
    assert state.observe(10, True) == "capture"
    assert state.started_at == 1
    assert state.observe(62, True) is None
    assert state.observe(120, True) is None
    assert state.observe(121, True) == "start"


def test_output_validation_rejects_tmpfs_and_unsafe_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_module, "_filesystem_type", lambda _path: "tmpfs")
    with pytest.raises(RuntimeError, match="disk-backed"):
        store_module.validate_output_dir(tmp_path / "output")
    monkeypatch.setattr(store_module, "_filesystem_type", lambda _path: "ext4")
    root = tmp_path / "unsafe"
    root.mkdir(mode=0o700)
    root.chmod(0o755)
    with pytest.raises(RuntimeError, match="mode 0700"):
        store_module.validate_output_dir(root)


@pytest.mark.platforms("linux")
def test_store_locks_rotates_and_recovers_bounded_incident(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_module, "_filesystem_type", lambda _path: "ext4")
    monkeypatch.setattr(store_module, "HISTORY_FILE_BYTES", 500)
    monkeypatch.setattr(store_module, "HISTORY_FILE_COUNT", 3)
    monkeypatch.setattr(store_module, "INCIDENT_FILE_BYTES", 2048)
    root = tmp_path / "store"
    store = store_module.RecorderStore(root)
    with pytest.raises(RuntimeError, match="another memory recorder"):
        store_module.RecorderStore(root)
    sample = {"schema_version": 1, "boot_id": "b", "sampled_at": "now", "monotonic_seconds": 10.0, "host": {"memory": {"available_kib": 99}, "swaps": {"used_kib": 20}}, "processes": [{"identity": {"boot_id": "b", "pid": 4, "start_ticks": 8}, "rss_kib": 99, "swap_kib": 1}], "cgroups": []}
    for _ in range(5):
        store.append_history({**sample, "payload": "x" * 100})
    assert len(list(root.glob("history*.jsonl"))) <= 3
    store.start_incident(sample, [sample, sample], ["available_below_2gib"], None)
    incident = next(root.glob("incident-*.jsonl"))
    store.close()
    resumed = store_module.RecorderStore(root)
    assert resumed.recover_event_start("b") == 10.0
    resumed.close()
    assert incident.stat().st_size <= store_module.INCIDENT_FILE_BYTES
    assert json.loads(incident.read_text().splitlines()[0])["kind"] == "event"


@pytest.mark.parametrize("header", [[], None, {"kind": "event", "event_started_monotonic": float("nan")}, {"kind": "event", "event_started_monotonic": float("inf")}, {"kind": "event", "event_started_monotonic": 10**1000}])
def test_malformed_incident_header_does_not_prevent_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header) -> None:
    monkeypatch.setattr(store_module, "_filesystem_type", lambda _path: "ext4")
    root = tmp_path / "private-output"
    root.mkdir(mode=0o700)
    incident = root / "incident-malformed.jsonl"
    incident.write_text(json.dumps(header) + "\n")
    incident.chmod(0o600)
    store = store_module.RecorderStore(root)
    try:
        assert store.recover_event_start() is None
    finally:
        store.close()
