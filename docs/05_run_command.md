---
log:
2026-05-12: Initial design and implementation of `colab run <script.py> [args...]`. Combines `colab new` + `colab exec` + `colab stop` into a single fire-and-forget invocation so a Python file can use `#!/usr/bin/env -S colab run` as a shebang line and execute on a freshly-allocated Colab VM. Adds `--keep` (skip auto-stop), `--gpu` / `--tpu` (passthrough to session creation), `-s/--session` (name the ephemeral session), and propagates the script's exit status (non-zero on any uncaught exception in the kernel). The script's `sys.argv` is re-set inside the kernel to mirror native `python script.py arg1 arg2` semantics, and `__name__` is set to `"__main__"`.
2026-05-12: Native CPython exit-code semantics for `sys.exit()` / `raise SystemExit(...)` from the script body. The Colab kernel reports a `SystemExit` as `output_type=='error'`, which under the previous logic would have (a) printed the IPython traceback (`An exception has occurred, use %tb...`) and (b) flagged the run as a failure regardless of the integer exit code. Now: `sys.exit()` / `sys.exit(0)` exit 0 silently; `sys.exit(N)` exits N; `sys.exit('msg')` exits 1 (matching CPython). The IPython "To exit: use 'exit', 'quit', or Ctrl-D." UserWarning is filtered via the prelude. Encoded after running `examples/gpu_hello.py` end-to-end and seeing the noisy `SystemExit: 0` traceback at the end of an otherwise-successful GPU run.
---

# Design: `colab run` — Shebang-Compatible One-Shot Execution

## Motivation
Inspired by the `llm` shebang pattern (https://til.simonwillison.net/llms/llm-shebang), users should be able to write a single self-contained Python file with a shebang line that:

1. Allocates a Colab VM according to user-supplied flags (CPU / GPU / TPU).
2. Executes the body of the file on that VM.
3. Tears the VM down when execution finishes — UNLESS told otherwise.

This is the natural ergonomic top-end of `colab-cli`: no boilerplate, no stale sessions, a single file is the unit of work.

## User Surface

```
colab run [OPTIONS] SCRIPT [SCRIPT_ARGS]...
```

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `SCRIPT` | positional | — | Local path to a `.py` file. Required. |
| `SCRIPT_ARGS` | variadic | — | Extra args forwarded to the script as `sys.argv[1:]`. |
| `-s`, `--session` | str | auto | Name the ephemeral session (helpful with `--keep`). Auto-generated as `run-<6 hex>` if omitted. |
| `--gpu` | str | None | Same set as `colab new --gpu` (T4, L4, G4, H100, A100). |
| `--tpu` | str | None | Same set as `colab new --tpu` (v5e1, v6e1). |
| `--keep` | bool | False | Do **not** stop the session after the script finishes. |
| `--timeout` | float | 10.0 | Timeout in seconds for code execution to prevent hanging on silent tasks. |

### Shebang usage
With `--keep` and `--gpu` baked into the shebang line, an entire one-file workload becomes:

```python
#!/usr/bin/env -S colab run --gpu T4
import torch
print(torch.cuda.get_device_name(0))
```

`chmod +x` and `./script.py` is then a single-step "rent a GPU, run, return".

> The `-S` flag of `env` is necessary on Linux/macOS to allow multiple words after `colab run` in a shebang line; without it the kernel passes the whole tail as one argument.

## Behavior

1. **Allocate**: Creates a fresh session (mirrors `colab new` end-to-end: `assign` → keep-alive pre-flight → spawn keep-alive daemon → persist `SessionState`). Session name defaults to `run-<6 hex>`.
2. **Execute**: Reads the script file. Prepends a deterministic prelude that re-sets `sys.argv` and `__name__` so the script body sees the same execution context as `python script.py arg1 arg2`:
   ```python
   import sys
   sys.argv = ['<basename>', 'arg1', 'arg2', ...]
   __name__ = '__main__'
   ```
   Then executes the script body in the same kernel cell so any `if __name__ == "__main__":` guard fires.
3. **Detect failure**: If the kernel returns any output of `output_type == "error"` (uncaught exception, syntax error, etc.) the CLI exits non-zero.
4. **Tear down**: In a `finally` block, unless `--keep` was passed, the CLI:
   - Sends `runtime.stop(shutdown_kernel=True)` (best-effort).
   - Calls `state.client.unassign(endpoint)` to free the billable VM.
   - Removes the session from `StateStore`.
   - Kills the keep-alive daemon (`kill_process(s.keep_alive_pid)`).
   - Logs `session_terminated` with `reason="run_completed"` (or `"run_failed"`).

If `--keep` is set, the session remains visible in `colab sessions` and `colab status` and can be reused with `colab exec -s <name>`, `colab repl -s <name>`, etc., until the user runs `colab stop` (or the keep-alive daemon hits its 24h cap).

## AGENTS.md Constraints Honoured
- **Item 7 (no background threads)**: The keep-alive daemon is the existing detached process from `colab new`; this command introduces no new threads.
- **Item 10 (live probes allocate real resources)**: The teardown is in a `try/finally` so an exception during execution still releases the VM. Tests assert `unassign` is called even when the script errors.
- **Item 16 (daemon flag propagation)**: Reuses `spawn_keep_alive(...)` which already propagates `--auth` and `--config`.
- **Item 17 (persist-before-spawn)**: Uses the same persist-before-spawn pattern as `colab new`.

## Testing Strategy (TDD)

### Unit tests (`tests/test_run.py`)
1. **`test_run_basic_flow`** — Happy path: create session, execute script, unassign on exit. Mocks `client.assign`, `client.unassign`, `ColabRuntime`. Asserts unassign is called.
2. **`test_run_keep_skips_unassign`** — With `--keep`, `unassign` is NOT called and the session remains in the store.
3. **`test_run_passes_argv`** — `colab run script.py a b c` results in a kernel `execute_code` call whose payload contains `sys.argv = ['script.py', 'a', 'b', 'c']`.
4. **`test_run_sets_dunder_main`** — The execute payload contains `__name__ = '__main__'`.
5. **`test_run_propagates_error_exit_code`** — When `runtime.execute_code` returns an output of `output_type == "error"`, the CLI exits non-zero AND still calls `unassign`.
6. **`test_run_with_gpu_flag`** — `colab run --gpu T4 script.py` calls `client.assign(..., variant=GPU, accelerator=T4)`.
7. **`test_run_missing_script_errors`** — `colab run` with no script path errors out (Typer-level).
8. **`test_run_nonexistent_script_errors_before_assign`** — `colab run does-not-exist.py` MUST exit non-zero **without** calling `client.assign` so users don't burn a VM on a typo.
9. **`test_run_unassign_called_on_exception_during_execute`** — If `runtime.execute_code` raises, unassign is still called (try/finally guarantee).

### Integration test (`integration/repro_run_command/test.sh`)
- Write a tiny script that prints its argv and exits 0.
- Run `colab run /tmp/script.py hello world`.
- Assert stdout contains `argv=['script.py', 'hello', 'world']`.
- Assert `colab sessions` returns "No active sessions" afterward (cleanup happened).
- Repeat with `--keep`: assert the session shows up in `colab sessions`, then call `colab stop` to clean up.
