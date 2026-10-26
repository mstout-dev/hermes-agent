# Bound native terminal workloads

This native installation patch requires a bounded `hermes-workloads.slice` before Linux gateway or managed-worker terminal commands start. A missing or unbounded slice refuses execution. It does not silently run commands inside the gateway's cgroup.

The example allocates at most 16 GiB of RAM and 2 GiB of swap across terminal jobs on a 32 GiB host. Adjust these aggregate limits for the host before installing. Each terminal scope also has a finite memory limit, zero swap, and at most 256 tasks. `terminal.local.memory_max_mb` can tighten its memory limit.

Install the slice as the gateway's operating-system user:

```bash
install -Dm644 contrib/systemd/hermes-workloads.slice "$HOME/.config/systemd/user/hermes-workloads.slice"
systemctl --user daemon-reload
systemctl --user start hermes-workloads.slice
systemctl --user show hermes-workloads.slice -p MemoryMax -p MemorySwapMax -p TasksMax
```

Set `terminal.local.require_workload_scope` to `true` in each native profile's configuration to cover manual workers and CLI commands as well. Use `hermes config set terminal.local.require_workload_scope true` for the default profile and `hermes -p PROFILE config set terminal.local.require_workload_scope true` for a named profile. Back up profile configuration before changing it.

Foreground commands, background commands, and PTY commands share the aggregate slice. A foreground command handed to the background retains its scope identity. Cancellation stops the owned scope, including daemonized descendants. Deliberately detached descendants retain their bounded scope after normal foreground completion. This preserves the existing detached-command behavior.

The gateway's own process and provider workers remain outside this terminal slice. This patch bounds terminal workloads; it does not impose a memory limit on every gateway component. A general upstream release also needs installer provisioning for the slice before enabling mandatory enforcement.
