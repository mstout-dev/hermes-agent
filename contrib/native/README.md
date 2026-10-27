# Native administration runner

`hermes_run.py` is the source artifact for the native administration helper. Install this file at
`NATIVE_ROOT/scripts/hermes_run.py`; its `INSTALLATION` path is derived from
that installed location. Keep the source copy byte-for-byte aligned with the
installed helper when preparing a deployment.

The runner gives one foreground command a private disk-backed temporary
directory, records its existing process scope, and removes the directory only
after the owned processes have stopped. TERM, HUP, and INT request bounded
cleanup and return `128 + signal`; SIGKILL cannot run cleanup.

`hermes-company-env` loads the saved company binding and Node 24. Install it in
your local executable directory. It selects bundled libvips for Sharp so a
host libvips installation does not force nested builds to compile Sharp from source.
