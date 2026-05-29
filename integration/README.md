# Integration tests

End-to-end tests that run against a **live Colab backend** (unlike the mocked unit tests under `tests/`).

## Prerequisites
- Google account with Colab access.
- `uv` installed.
- Working auth — verify with `colab sessions`.

## Scenarios

| Directory | What it covers |
| --- | --- |
| `repro_plot_redirection/` | `colab exec` of a matplotlib script with `--output-image` redirection. |
| `repro_keep_alive/` | Fast smoke test (~10s): keep-alive daemon spawns, persists its PID, no errors during the pre-flight ping, `colab stop` reaps it. |
| `repro_keep_alive_scope/` | Slow soak test (~95s): runs the daemon long enough for one ping past the pre-flight, asserts no `keep_alive_error` events. |
| `repro_variable_persistence/` | Variables persist across `colab exec` calls in the same session. |
| `repro_piped_console/` | Fast smoke test (~5s including session creation): `echo cmd \| colab console -s s` runs the command and exits within 30s. Regression test for the 2026-05-07 EOF-handler fix. |
| `repro_bundled_oauth/` | Fast smoke test (~5s): verifies that the fallback OAuth configuration is loaded and starts the OAuth flow with the default client ID when local config is missing. |


## Running
```bash
uv run bash integration/repro_keep_alive/test.sh
```
`uv run` ensures the local `colab` entry point is on `PATH`.

## Adding a scenario
1. Create `repro_<short_description>/`.
2. Add a script (`.sh` or `.py`) that demonstrates or verifies the issue.
3. Add a row to the table above noting whether it's fast (smoke) or slow (soak).
