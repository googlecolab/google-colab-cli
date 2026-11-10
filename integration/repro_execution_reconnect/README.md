# Execution recovery live tests

Run from the repository root in Bash or WSL, with Colab CLI credentials available
in that environment:

```bash
uv run colab --auth=oauth2 sessions
uv run bash integration/repro_execution_reconnect/test.sh
```

The suite creates CPU runtimes with isolated local state. It releases only its
own endpoints in `finally`, then checks the server assignments. Existing user
runtimes are left alone.

The output hook closes the actual WebSocket TCP connection after an output marker.
Tests cover repeated interruptions, success and exceptions during disconnection,
stdout/stderr and rich output, unchanged kernel/session IDs, and an independent
receipt recording one execution. Additional faults replace the recovery reader's
proxy token or route with invalid values and stop the socket worker while a task
runs for longer than the recovery timeout. These injected faults exercise refresh
and HTTP fallback; they do not establish the cause of every reported disconnect
or reproduce natural credential expiry.

A separate CLI process is killed after its user code starts. A new `resume`
process must retrieve the original transcript and result, and the receipt must
still show one execution. The suite also checks stale startup credentials,
preservation after an idle timeout, and normal `run` cleanup. Finally it deletes
an owned kernel during an incomplete execution and checks that resume reports
the recorded shutdown error or an unknown result, without allocating another
assignment or creating a replacement kernel.

To rerun one disconnect scenario while retaining lifecycle checks:

```bash
uv run python integration/repro_execution_reconnect/test.py --scenario stale_token
```

Other selections include `stale_route`, `stopped_socket_thread`, `two_drops`,
`offline_success`, `offline_error`, and `offline_no_limit`. Use `--scenario
missing_kernel` to check only idle-timeout recovery and the destroyed-kernel
boundary. A resumed transcript starts at the beginning; output displayed by the
old process can repeat, while user code must never run twice.
