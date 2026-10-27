"""Bounded, private JSONL storage for the standalone memory recorder."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

HISTORY_FILE_BYTES = 16 * 1024 * 1024
HISTORY_FILE_COUNT = 5
INCIDENT_FILE_BYTES = 16 * 1024 * 1024
INCIDENT_FILE_COUNT = 4
MAX_RECORD_BYTES = 512 * 1024


def _decode_mount_field(value: str) -> str:
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")


def _filesystem_type(path: Path) -> str:
    resolved = path.resolve()
    best: tuple[int, str] = (-1, "")
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError("cannot verify output filesystem") from exc
    for line in lines:
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        fields = left.split()
        after = right.split()
        if len(fields) < 5 or not after:
            continue
        mountpoint = Path(_decode_mount_field(fields[4]))
        try:
            matches = resolved == mountpoint or mountpoint in resolved.parents
        except RuntimeError:
            matches = False
        if matches and len(str(mountpoint)) > best[0]:
            best = len(str(mountpoint)), after[0]
    if not best[1]:
        raise RuntimeError("cannot identify output filesystem")
    return best[1]


def _current_uid() -> int:
    return Path.home().stat().st_uid


def validate_output_dir(root: Path) -> Path:
    root = root.expanduser().absolute()
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        if current.exists() and current.is_symlink():
            raise RuntimeError(f"output path contains a symlink: {current}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != _current_uid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("output directory must be owned by the current user with mode 0700")
    if _filesystem_type(root) in {"tmpfs", "ramfs"}:
        raise RuntimeError("output directory must be on a disk-backed filesystem")
    return root


class RecorderStore:
    def __init__(self, root: Path) -> None:
        import fcntl

        self.root = validate_output_dir(root)
        self._validate_existing_files()
        lock_fd = os.open(self.root / ".memory-recorder.lock", os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        lock_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != _current_uid() or stat.S_IMODE(lock_stat.st_mode) & 0o077:
            os.close(lock_fd)
            raise RuntimeError("lock file must be private and owned by the current user")
        self._lock = os.fdopen(lock_fd, "a", encoding="ascii")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock.close()
            raise RuntimeError("another memory recorder owns this output directory") from exc
        self._incident_path: Path | None = None
        self._incident_truncated = False
        self._flock = fcntl.flock
        self._lock_un = fcntl.LOCK_UN
        self._prune_incidents()

    def _validate_existing_files(self) -> None:
        allowed = {".memory-recorder.lock", "history.jsonl"} | {f"history.{i}.jsonl" for i in range(1, HISTORY_FILE_COUNT)}
        allowed.update(path.name for path in self.root.glob("incident-*.jsonl"))
        for path in self.root.iterdir():
            if path.name not in allowed:
                raise RuntimeError(f"unexpected entry in recorder output directory: {path.name[:80]}")
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != _current_uid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise RuntimeError("recorder files must be private regular files owned by the current user")
            limit = INCIDENT_FILE_BYTES if path.name.startswith("incident-") else HISTORY_FILE_BYTES
            if path.name != ".memory-recorder.lock" and info.st_size > limit:
                raise RuntimeError("existing recorder file exceeds its retention limit")

    def close(self) -> None:
        if not self._lock.closed:
            self._flock(self._lock.fileno(), self._lock_un)
            self._lock.close()

    def append_history(self, sample: dict[str, Any]) -> None:
        self._append_bounded(self.root / "history.jsonl", sample, HISTORY_FILE_BYTES, rotate=True)
        fd = os.open(self.root / "history.jsonl", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def start_incident(self, sample: dict[str, Any], ring: list[dict[str, Any]], reasons: list[str], previous: dict[str, Any] | None) -> None:
        started = sample["monotonic_seconds"]
        self._prune_incidents(reserve=1)
        self._incident_path = self.root / f"incident-{time.time_ns()}.jsonl"
        self._incident_truncated = False
        event = self._event_record(sample, previous, reasons, started)
        self._append_bounded(self._incident_path, event, INCIDENT_FILE_BYTES)
        for pre_event in ring[:-1]:
            self._append_bounded(self._incident_path, {"kind": "sample", "sample": pre_event}, INCIDENT_FILE_BYTES)
        self.append_incident(sample, event_started=started)
        self._prune_incidents()
        fd = os.open(self._incident_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def append_incident(self, sample: dict[str, Any], *, event_started: float) -> None:
        if self._incident_path is None:
            self._incident_path = self._find_active_incident(event_started)
        if self._incident_path is not None:
            self._append_bounded(self._incident_path, {"kind": "sample", "sample": sample}, INCIDENT_FILE_BYTES)

    def recover_event_start(self, boot_id: str | None = None) -> float | None:
        candidates = sorted(self.root.glob("incident-*.jsonl"), key=lambda item: item.stat().st_mtime_ns, reverse=True)
        for candidate in candidates:
            try:
                with candidate.open("rb") as stream:
                    raw = stream.readline(MAX_RECORD_BYTES + 1)
                event = json.loads(raw)
                started = _event_start(event, boot_id)
                if started is not None:
                    self._incident_path = candidate
                    return float(started)
            except (OSError, ValueError, TypeError):
                continue
        return None

    def _find_active_incident(self, started: float) -> Path | None:
        for candidate in sorted(self.root.glob("incident-*.jsonl"), key=lambda item: item.stat().st_mtime_ns, reverse=True):
            try:
                with candidate.open("rb") as stream:
                    event = json.loads(stream.readline(MAX_RECORD_BYTES + 1))
                if _event_start(event) == started:
                    return candidate
            except (OSError, ValueError, TypeError):
                continue
        return None

    @staticmethod
    def _event_record(sample: dict[str, Any], previous: dict[str, Any] | None, reasons: list[str], started: float) -> dict[str, Any]:
        old_processes = _identity_rows(previous)
        deltas = []
        for process in sample["processes"]:
            identity = process["identity"]
            key = (identity["boot_id"], identity["pid"], identity["start_ticks"])
            old = old_processes.get(key)
            current = (process.get("rss_kib") or 0) + (process.get("swap_kib") or 0)
            before = 0 if old is None else (old.get("rss_kib") or 0) + (old.get("swap_kib") or 0)
            delta = current - before
            if delta > 0:
                deltas.append({"identity": identity, "rss_swap_delta_kib": delta, "new_process": old is None})
        deltas.sort(key=lambda item: item["rss_swap_delta_kib"], reverse=True)
        cgroup_deltas = _cgroup_deltas(sample, previous)
        return {"schema_version": 1, "kind": "event", "boot_id": sample.get("boot_id"), "event_started_monotonic": started, "sampled_at": sample["sampled_at"], "reasons": reasons, "identities_and_deltas": deltas[:16], "cgroup_deltas": cgroup_deltas[:16], "global_swap_delta_kib": _swap_delta(sample, previous), "available_kib": sample["host"]["memory"].get("available_kib")}

    def _append_bounded(self, path: Path, record: dict[str, Any], limit: int, *, rotate: bool = False) -> None:
        raw = (json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        if len(raw) > MAX_RECORD_BYTES:
            raw = b'{"schema_version":1,"kind":"truncated","reason":"record_over_limit"}\n'
        if path.exists() and path.stat().st_size + len(raw) > limit:
            if rotate:
                self._rotate_history(path)
            else:
                self._mark_incident_truncated(path, limit)
                return
        if path.exists() and path.stat().st_size + len(raw) > limit:
            return
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            view = memoryview(raw)
            while view:
                view = view[os.write(fd, view) :]
        finally:
            os.close(fd)

    def _rotate_history(self, current: Path) -> None:
        oldest = self.root / f"history.{HISTORY_FILE_COUNT - 1}.jsonl"
        oldest.unlink(missing_ok=True)
        for index in range(HISTORY_FILE_COUNT - 2, 0, -1):
            source = self.root / f"history.{index}.jsonl"
            if source.exists():
                source.replace(self.root / f"history.{index + 1}.jsonl")
        if current.exists():
            current.replace(self.root / "history.1.jsonl")

    def _prune_incidents(self, *, reserve: int = 0) -> None:
        paths = sorted(self.root.glob("incident-*.jsonl"), key=lambda item: item.stat().st_mtime_ns)
        for path in paths[:max(0, len(paths) - (INCIDENT_FILE_COUNT - reserve))]:
            path.unlink(missing_ok=True)

    def _mark_incident_truncated(self, path: Path, limit: int) -> None:
        if path != self._incident_path or self._incident_truncated:
            return
        row = {"schema_version": 1, "kind": "truncated", "reason": "incident_byte_limit"}
        raw = (json.dumps(row, separators=(",", ":")) + "\n").encode()
        try:
            if path.stat().st_size + len(raw) <= limit:
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
                try:
                    os.write(fd, raw)
                finally:
                    os.close(fd)
        except OSError:
            pass
        self._incident_truncated = True


def _event_start(event: Any, boot_id: str | None = None) -> float | None:
    if not isinstance(event, dict) or event.get("kind") != "event":
        return None
    if boot_id is not None and event.get("boot_id") != boot_id:
        return None
    value = event.get("event_started_monotonic")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= value <= time.monotonic() else None


def _identity_rows(sample: dict[str, Any] | None) -> dict[tuple[str, int, int], dict[str, Any]]:
    if sample is None:
        return {}
    return {(row["identity"]["boot_id"], row["identity"]["pid"], row["identity"]["start_ticks"]): row for row in sample["processes"]}


def _swap_delta(sample: dict[str, Any], previous: dict[str, Any] | None) -> int | None:
    now = sample["host"]["swaps"].get("used_kib")
    before = previous["host"]["swaps"].get("used_kib") if previous else None
    return now - before if now is not None and before is not None else None


def _cgroup_deltas(sample: dict[str, Any], previous: dict[str, Any] | None) -> list[dict[str, Any]]:
    old = {row["name"]: row for row in previous["cgroups"]} if previous else {}
    changes = []
    for row in sample["cgroups"]:
        before = old.get(row["name"], {})
        current = (row.get("memory_current_bytes") or 0) + (row.get("swap_current_bytes") or 0)
        old_total = (before.get("memory_current_bytes") or 0) + (before.get("swap_current_bytes") or 0)
        if current > old_total:
            changes.append({"cgroup": row["name"], "memory_swap_delta_bytes": current - old_total})
    return sorted(changes, key=lambda row: row["memory_swap_delta_bytes"], reverse=True)
