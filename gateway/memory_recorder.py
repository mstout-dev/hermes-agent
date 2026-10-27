"""Standalone Linux memory recorder. Run with ``python -m gateway.memory_recorder``."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gateway.memory_recorder_store import RecorderStore, validate_output_dir

SCHEMA_VERSION = 1
SAMPLE_INTERVAL_SECONDS = 2.0
MAX_INTERVAL_SECONDS = 30.0
MAX_PROCESSES = 2048
MAX_PROC_READS = 8192
MAX_CGROUPS = 256
MAX_RECORD_BYTES = 512 * 1024
MAX_SAMPLE_BYTES = MAX_RECORD_BYTES - 1024
MAX_PSS_PROCESSES = 8
PRE_EVENT_SECONDS = 20.0
INCIDENT_POST_SECONDS = 60.0
COOLDOWN_SECONDS = 120.0
PRESSURE_AVAILABLE_BYTES = 2 * 1024**3
PRESSURE_RISE_PER_TWO_SECONDS = 1024**3


def _read_text(path: Path, limit: int = 65536) -> str | None:
    try:
        with path.open("rb") as stream:
            return stream.read(limit).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def _integer(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text.strip().split()[0])
    except (ValueError, IndexError):
        return None


def _proc_stat(path: Path) -> tuple[str, int, int] | None:
    raw = _read_text(path / "stat", 8192)
    if raw is None:
        return None
    close = raw.rfind(")")
    open_at = raw.find("(")
    if open_at < 0 or close < open_at:
        return None
    fields = raw[close + 2 :].split()
    try:
        return raw[open_at + 1 : close][:64], int(fields[1]), int(fields[19])
    except (ValueError, IndexError):
        return None


def _status(path: Path) -> dict[str, int | None]:
    wanted = {"VmRSS": "rss_kib", "RssAnon": "rss_anon_kib", "RssFile": "rss_file_kib", "RssShmem": "rss_shmem_kib", "VmSwap": "swap_kib"}
    result: dict[str, int | None] = {key: None for key in wanted.values()}
    raw = _read_text(path / "status", 16384)
    if raw is None:
        return result
    for line in raw.splitlines():
        key, separator, value = line.partition(":")
        if separator and key in wanted:
            result[wanted[key]] = _integer(value)
    return result


def _proc_io(path: Path) -> dict[str, int | None]:
    result: dict[str, int | None] = {"read_bytes": None, "write_bytes": None, "wchar_bytes": None}
    raw = _read_text(path / "io", 4096)
    if raw is None:
        return result
    fields = dict(line.split(":", 1) for line in raw.splitlines() if ":" in line)
    for source, target in (("read_bytes", "read_bytes"), ("write_bytes", "write_bytes"), ("wchar", "wchar_bytes")):
        result[target] = _integer(fields.get(source))
    return result


def _cgroup_path(path: Path) -> str | None:
    raw = _read_text(path / "cgroup", 4096)
    if raw is None:
        return None
    for line in raw.splitlines():
        _, _, group = line.partition("::")
        if group:
            return group.strip()[:512]
    return None


def _safe_label(value: str | None) -> str | None:
    if value is None:
        return None
    unit = re.sub(r"[^A-Za-z0-9_.@-]", "_", Path(value).name or "root")[:96]
    return unit + "-" + hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


def _process(path: Path, boot_id: str) -> dict[str, Any] | None:
    before = _proc_stat(path)
    if before is None:
        return None
    name, ppid, start_ticks = before
    fields = _status(path)
    io = _proc_io(path)
    raw_exe = None
    try:
        raw_exe = os.path.basename(os.readlink(path / "exe"))[:128]
    except OSError:
        pass
    cgroup = _cgroup_path(path)
    after = _proc_stat(path)
    if after is None or after[2] != start_ticks:
        return None
    return {
        "identity": {"boot_id": boot_id, "pid": int(path.name), "start_ticks": start_ticks},
        "ppid": ppid,
        "comm": name,
        "exe_basename": raw_exe,
        "cgroup": _safe_label(cgroup),
        **fields,
        "io": io,
        "pss_kib": None,
        "pss_observed_at": None,
    }


def _pss(path: Path, identity: dict[str, Any], boot_id: str) -> tuple[int | None, str | None]:
    if _proc_stat(path) is None:
        return None, None
    before = _proc_stat(path)
    if before is None or before[2] != identity["start_ticks"]:
        return None, None
    raw = _read_text(path / "smaps_rollup", 32768)
    after = _proc_stat(path)
    if raw is None or after is None or after[2] != before[2]:
        return None, None
    pss = next((_integer(line.partition(":")[2]) for line in raw.splitlines() if line.startswith("Pss:")), None)
    if pss is None:
        return None, None
    return pss, datetime.now(UTC).isoformat()


def _meminfo(path: Path) -> dict[str, int | None]:
    raw = _read_text(path / "meminfo", 16384)
    wanted = ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")
    values = {key: None for key in wanted}
    if raw:
        for line in raw.splitlines():
            key, sep, rest = line.partition(":")
            if sep and key in values:
                values[key] = _integer(rest)
    total, free = values["SwapTotal"], values["SwapFree"]
    return {"total_kib": values["MemTotal"], "available_kib": values["MemAvailable"], "swap_used_kib": total - free if total is not None and free is not None else None}


def _pressure(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"some_avg10": None, "some_total_us": None, "full_avg10": None, "full_total_us": None}
    raw = _read_text(path / "pressure" / "memory", 2048)
    if raw:
        for line in raw.splitlines():
            kind, _, rest = line.partition(" ")
            if kind not in ("some", "full"):
                continue
            fields = dict(part.split("=", 1) for part in rest.split() if "=" in part)
            result[f"{kind}_avg10"] = _float(fields.get("avg10"))
            result[f"{kind}_total_us"] = _integer(fields.get("total"))
    return result


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _swaps(path: Path) -> dict[str, Any]:
    rows = []
    raw = _read_text(path / "swaps", 16384)
    if raw:
        for line in raw.splitlines()[1:33]:
            fields = line.split()
            if len(fields) >= 5:
                rows.append({"device": os.path.basename(fields[0])[:64], "type": fields[1][:16], "size_kib": _integer(fields[2]), "used_kib": _integer(fields[3]), "priority": _integer(fields[4])})
    return {"entries": rows, "used_kib": sum(row["used_kib"] or 0 for row in rows) if raw is not None else None}


def _zram(sys_root: Path) -> list[dict[str, Any]]:
    result = []
    for node in sorted((sys_root / "block").glob("zram[0-9]*"))[:8]:
        values = (_read_text(node / "mm_stat", 1024) or "").split()
        result.append({"device": node.name[:32], "logical_bytes": _int_at(values, 0), "compressed_bytes": _int_at(values, 1), "physical_bytes": _int_at(values, 2)})
    return result


def _int_at(values: list[str], index: int) -> int | None:
    try:
        return int(values[index])
    except (ValueError, IndexError):
        return None


def _mount_usage(target: Path) -> dict[str, Any]:
    try:
        stat = os.statvfs(target)
        return {"target": str(target), "total_bytes": stat.f_blocks * stat.f_frsize, "free_bytes": stat.f_bavail * stat.f_frsize, "allocated_bytes": (stat.f_blocks - stat.f_bfree) * stat.f_frsize}
    except OSError:
        return {"target": str(target), "total_bytes": None, "free_bytes": None, "allocated_bytes": None}


def _cgroup_rows(paths: set[str], sys_root: Path) -> tuple[list[dict[str, Any]], int]:
    rows = []
    ordered = sorted(paths)
    for rel in ordered[:MAX_CGROUPS]:
        base = sys_root / "fs" / "cgroup" / rel.lstrip("/")
        stat = _read_text(base / "memory.stat", 8192)
        stats = dict(line.split()[:2] for line in (stat or "").splitlines() if len(line.split()) >= 2)
        rows.append({"name": _safe_label(rel), "memory_current_bytes": _integer(_read_text(base / "memory.current", 64)), "swap_current_bytes": _integer(_read_text(base / "memory.swap.current", 64)), "anon_bytes": _integer(stats.get("anon")), "file_bytes": _integer(stats.get("file")), "shmem_bytes": _integer(stats.get("shmem")), "oom_events": _event_value(base / "memory.events", "oom"), "oom_kill_events": _event_value(base / "memory.events", "oom_kill")})
    return rows, max(0, len(ordered) - len(rows))


def _event_value(path: Path, key: str) -> int | None:
    raw = _read_text(path, 4096)
    if raw is None:
        return None
    values = dict(line.split()[:2] for line in raw.splitlines() if len(line.split()) >= 2)
    return _integer(values.get(key))


def _encode_bounded(record: dict[str, Any]) -> tuple[bytes, int]:
    omitted = record.get("omitted_processes", 0)
    while True:
        record["truncated"] = omitted > 0 or record.get("omitted_cgroups", 0) > 0
        record["omitted_processes"] = omitted
        raw = (json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        if len(raw) <= MAX_SAMPLE_BYTES:
            return raw, omitted
        processes = record["processes"]
        cgroups = record["cgroups"]
        if processes:
            keep = max(0, min(len(processes) - 1, int(len(processes) * MAX_SAMPLE_BYTES / len(raw)) - 1))
            omitted += len(processes) - keep
            del processes[keep:]
        elif cgroups:
            cgroups.pop()
            record["omitted_cgroups"] = record.get("omitted_cgroups", 0) + 1
        else:
            minimal = {key: record.get(key) for key in ("schema_version", "sampled_at", "monotonic_seconds", "boot_id")}
            minimal.update({"processes": [], "cgroups": [], "truncated": True, "omitted_processes": omitted, "omitted_cgroups": record.get("omitted_cgroups", 0)})
            return (json.dumps(minimal, separators=(",", ":")) + "\n").encode(), omitted


def collect_sample(proc_root: Path = Path("/proc"), sys_root: Path = Path("/sys"), *, now_monotonic: float | None = None) -> dict[str, Any]:
    scan_started = time.monotonic()
    boot_id = (_read_text(proc_root / "sys" / "kernel" / "random" / "boot_id", 128) or "unknown").strip()[:64]
    captured_mono = time.monotonic() if now_monotonic is None else now_monotonic
    processes = []
    unreadable = 0
    pids = sorted(int(entry.name) for entry in proc_root.iterdir() if entry.name.isdecimal())
    sampled_pids = pids
    if len(pids) > MAX_PROC_READS:
        sampled_pids = [pids[round(index * (len(pids) - 1) / (MAX_PROC_READS - 1))] for index in range(MAX_PROC_READS)]
    for pid in sampled_pids:
        entry = proc_root / str(pid)
        sample = _process(entry, boot_id)
        if sample is None:
            unreadable += 1
        else:
            processes.append(sample)
    processes.sort(key=lambda row: (row["rss_kib"] or 0) + (row["swap_kib"] or 0), reverse=True)
    omitted_processes = max(0, len(pids) - min(len(processes), MAX_PROCESSES))
    processes = processes[:MAX_PROCESSES]
    for row in sorted(processes, key=lambda row: row["rss_kib"] or 0, reverse=True)[:MAX_PSS_PROCESSES]:
        row["pss_kib"], row["pss_observed_at"] = _pss(proc_root / str(row["identity"]["pid"]), row["identity"], boot_id)
    cgroup_paths = {path for entry in processes if (path := _cgroup_path(proc_root / str(entry["identity"]["pid"]))) is not None}
    for group in tuple(cgroup_paths):
        cgroup_paths.update(str(parent) for parent in Path(group).parents)
    cgroups, omitted_cgroups = _cgroup_rows(cgroup_paths, sys_root)
    uid_path = Path("/run/user") / str(Path.home().stat().st_uid)
    record = {
        "schema_version": SCHEMA_VERSION,
        "sampled_at": datetime.now(UTC).isoformat(),
        "monotonic_seconds": captured_mono,
        "boot_id": boot_id,
        "host": {"memory": _meminfo(proc_root), "pressure": _pressure(proc_root), "swaps": _swaps(proc_root), "zram": _zram(sys_root)},
        "tmpfs": [_mount_usage(Path("/tmp")), _mount_usage(uid_path), _mount_usage(Path("/dev/shm"))],  # no-tmp: ok — record host tmpfs allocation as required
        "processes": processes,
        "cgroups": cgroups,
        "unreadable_processes": unreadable,
        "sampled_processes": len(sampled_pids),
        "unscanned_processes": len(pids) - len(sampled_pids),
        "omitted_processes": omitted_processes,
        "omitted_cgroups": omitted_cgroups,
        "truncated": False,
        "scan_duration_seconds": time.monotonic() - scan_started,
    }
    raw, _ = _encode_bounded(record)
    return json.loads(raw)


def _process_totals(sample: dict[str, Any]) -> dict[tuple[str, int, int], int]:
    totals = {}
    for process in sample["processes"]:
        identity = process["identity"]
        key = identity["boot_id"], identity["pid"], identity["start_ticks"]
        totals[key] = (process.get("rss_kib") or 0) + (process.get("swap_kib") or 0)
    return totals


def classify_pressure(current: dict[str, Any], previous: dict[str, Any] | None) -> list[str]:
    reasons = []
    available = current["host"]["memory"].get("available_kib")
    if available is not None and available * 1024 < PRESSURE_AVAILABLE_BYTES:
        reasons.append("available_below_2gib")
    if previous is None:
        return reasons
    elapsed = max(0.1, current["monotonic_seconds"] - previous["monotonic_seconds"])
    threshold_kib = PRESSURE_RISE_PER_TWO_SECONDS * elapsed / SAMPLE_INTERVAL_SECONDS / 1024
    swap_now = current["host"]["swaps"].get("used_kib")
    swap_before = previous["host"]["swaps"].get("used_kib")
    if swap_now is not None and swap_before is not None and swap_now - swap_before >= threshold_kib:
        reasons.append("global_swap_rise")
    before = _process_totals(previous)
    after = _process_totals(current)
    growth = max((value - before.get(identity, 0) for identity, value in after.items()), default=0)
    if growth >= threshold_kib:
        reasons.append("process_rss_swap_rise")
    cg_before = {row["name"]: _cgroup_total(row) for row in previous["cgroups"]}
    cg_growth = max((total - (cg_before.get(row["name"]) or 0) for row in current["cgroups"] if (total := _cgroup_total(row)) is not None), default=0)
    if cg_growth >= threshold_kib * 1024:
        reasons.append("cgroup_memory_swap_rise")
    return reasons


def _cgroup_total(row: dict[str, Any]) -> int | None:
    memory, swap = row.get("memory_current_bytes"), row.get("swap_current_bytes")
    return memory + swap if memory is not None and swap is not None else None


@dataclass
class IncidentState:
    started_at: float | None
    rearm_at: float

    def observe(self, now: float, triggered: bool) -> str | None:
        if self.started_at is not None:
            if now < self.started_at + INCIDENT_POST_SECONDS:
                return "capture"
            self.started_at = None
        if triggered and now >= self.rearm_at:
            self.started_at = now
            self.rearm_at = now + COOLDOWN_SECONDS
            return "start"
        return None


def run(output_dir: Path, *, interval: float = SAMPLE_INTERVAL_SECONDS, samples: int | None = None) -> int:
    validate_output_dir(output_dir)
    store = RecorderStore(output_dir)
    previous = None
    ring: list[dict[str, Any]] = []
    current_boot_id = (_read_text(Path("/proc/sys/kernel/random/boot_id"), 128) or "unknown").strip()[:64]
    restored_start = store.recover_event_start(current_boot_id)
    incident = IncidentState(restored_start, restored_start + COOLDOWN_SECONDS if restored_start is not None else 0.0)
    stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    old_term = signal.signal(signal.SIGTERM, request_stop)
    old_int = signal.signal(signal.SIGINT, request_stop)
    try:
        count = 0
        next_tick = time.monotonic()
        while not stop and (samples is None or count < samples):
            now = time.monotonic()
            if now < next_tick:
                time.sleep(next_tick - now)
            sample = collect_sample(now_monotonic=time.monotonic())
            sample["scheduled_interval_seconds"] = interval
            sample["sampling_gap_seconds"] = max(0.0, sample["monotonic_seconds"] - previous["monotonic_seconds"] - interval) if previous else None
            store.append_history(sample)
            ring.append(sample)
            ring = [row for row in ring[-21:] if row["monotonic_seconds"] >= sample["monotonic_seconds"] - PRE_EVENT_SECONDS]
            reasons = classify_pressure(sample, previous)
            action = incident.observe(sample["monotonic_seconds"], bool(reasons))
            if action == "capture":
                store.append_incident(sample, event_started=incident.started_at or 0.0)
            elif action == "start":
                store.start_incident(sample, ring, reasons, previous)
            previous = sample
            count += 1
            next_tick += interval
            if next_tick < time.monotonic() - interval:
                next_tick = time.monotonic() + interval
        return 0
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record bounded Linux process and memory samples")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--samples", type=int)
    parser.add_argument("--interval", type=float, default=SAMPLE_INTERVAL_SECONDS)
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        parser.error("memory recorder requires Linux")
    if args.once and args.samples is not None:
        parser.error("--once and --samples cannot be combined")
    if args.samples is not None and args.samples < 1:
        parser.error("--samples must be positive")
    if not 1 <= args.interval <= MAX_INTERVAL_SECONDS:
        parser.error(f"--interval must be between 1 and {MAX_INTERVAL_SECONDS:g} seconds")
    return run(args.output_dir, interval=args.interval, samples=1 if args.once else args.samples)


if __name__ == "__main__":
    raise SystemExit(main())
