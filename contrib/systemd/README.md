# Bound native workloads and record memory growth

This native installation patch requires a bounded `hermes-workloads.slice` before Linux gateway or managed-worker terminal commands or language servers start. A missing or unbounded slice refuses execution. Each language server starts in its own scope before executing, outside the gateway's cgroup.

The example allocates at most 16 GiB of RAM and 2 GiB of swap across terminal jobs and language servers on a 32 GiB host. Adjust these aggregate limits for the host before installing. Each terminal command is bounded by the verified aggregate RAM limit and half of physical RAM, with zero swap. Language servers retain a 4 GiB per-process limit. `terminal.local.memory_max_mb` and a tighter enclosing limit can reduce a local command's memory limit.

Install the slice as the gateway's operating-system user:

```bash
install -Dm644 contrib/systemd/hermes-workloads.slice "$HOME/.config/systemd/user/hermes-workloads.slice"
systemctl --user daemon-reload
systemctl --user start hermes-workloads.slice
systemctl --user show hermes-workloads.slice -p MemoryMax -p MemorySwapMax
```

Set `terminal.local.require_workload_scope` to `true` in each native profile's configuration to cover manual workers and CLI commands as well. Use `hermes config set terminal.local.require_workload_scope true` for the default profile and `hermes -p PROFILE config set terminal.local.require_workload_scope true` for a named profile. Back up profile configuration before changing it.

Foreground commands, background commands, PTY commands, and language servers share the aggregate slice. A foreground command handed to the background retains its scope identity. Cancellation stops the owned scope, including daemonized descendants. Deliberately detached terminal descendants retain their bounded scope after normal foreground completion. Language-server shutdown also closes its owned scope after the launcher has exited.

The gateway's own process and provider workers remain outside this slice. This patch does not impose a memory limit on every gateway component. A general upstream release also needs installer provisioning for the slice before enabling mandatory enforcement. Non-Linux callers retain their existing process-group behavior.

## Retain process memory history

The standalone recorder samples every two seconds. It runs in a separate service so a kill of the gateway cgroup does not kill the recorder. The example service uses a 256 MiB RAM limit, zero swap, and 20 percent of one CPU.

The service example targets this native installation layout. Check its Python executable, source directory, and output path before installing it on another host. The recorder uses the Python standard library and needs readable host procfs and cgroup v2 counters. Run it as the gateway's operating-system user.

```bash
install -Dm644 contrib/systemd/hermes-memory-recorder.service "$HOME/.config/systemd/user/hermes-memory-recorder.service"
systemctl --user daemon-reload
systemctl --user enable --now hermes-memory-recorder.service
systemctl --user show hermes-memory-recorder.service -p ActiveState -p ControlGroup -p MemoryMax -p MemorySwapMax
```

The recorder writes private JSONL under the example's `logs/memory-recorder` directory. It refuses RAM-backed output, unsafe permissions, and a second writer to the same directory. History occupies at most five 16 MiB files. Up to four 16 MiB incident files preserve the preceding twenty seconds and following sixty seconds of a pressure event. The total data budget is 144 MiB plus the lock file. Incident files survive history rotation.

An incident starts when available RAM falls below 2 GiB, or host swap, a process's combined RSS and swap, or a cgroup's combined memory and swap grows by at least 1 GiB per two seconds. Process growth uses the process incarnation, so a reused PID does not inherit another process's history. Repeated events have a 120-second cooldown.

Each sample includes host memory, pressure, swap devices, physical zram backing, tmpfs filesystem usage, cgroup charges, and process RSS, anonymous, file, shared, and swap counters. The eight largest resident processes also receive a timestamped PSS reading when permitted. Process I/O counters help identify a writer when tmpfs usage rises. Boot ID, PID, start ticks, parent PID, executable basename, and cgroup label identify the process without recording command arguments, environment values, or file contents.

For a bounded foreground check, select a new private disk-backed output directory:

```bash
python3 -m gateway.memory_recorder --output-dir /path/to/private/disk-output --samples 3
```

The records identify process and cgroup growth. They do not provide heap-object or allocation-stack traces. Short-lived processes between samples, inaccessible counters, and scan or record limits remain observable gaps. Missing readings stay null and truncation is explicit. RSS, PSS, cgroup charges, tmpfs allocations, logical swap, and physical zram overlap and must not be added as independent consumers.
