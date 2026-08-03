---
log:
2026-07-17: Initial design and implementation of `colab ssh` — client side of SSH-over-WebSocket runtime access. Adds three modes (interactive shell, `-s SESSION`, and `--proxy-mode` OpenSSH ProxyCommand bridge), `--identity/-i` key selection, and per-HTTP-status handshake error messages. Server side is out of scope for this repo; the subcommand is a no-op against runtimes that do not expose the `/colab/ssh` endpoint (surfaces an actionable HTTP 404 message).
2026-07-22: Bare `colab ssh` now auto-creates a runtime (via `colab new`) when you have no active session, with `--gpu/--tpu` passthrough and `--rm` to stop an auto-created runtime on exit. Fixed two client bugs: the dead 403 branch (feature-off returns 404, not 403) and the RSA guidance (all `ssh-rsa` keys are server-rejected, so `id_rsa` is no longer auto-scanned and the 400 message no longer advertises `rsa-sha2`). Added `tests/test_ssh_wire_contract.py` (real loopback-server wire assertions) and `tests/test_ssh_autocreate.py`.
2026-07-22: Interactive `colab ssh` now starts in `/content` (Colab's working dir) instead of `/root`, via a forced PTY (`-t`) plus a remote `cd /content 2>/dev/null; exec $SHELL -l`. A missing `/content` falls back to the login home. Added `tests/test_ssh_workdir.py`.
2026-07-22: `--proxy-mode` now honors every `colab ssh` flag: with `-s NAME` it creates the session if missing (creation output routed to stderr so stdout stays the clean ssh byte stream), `--gpu/--tpu` set the accelerator, and `--rm` stops the bridged session on disconnect — so `~/.ssh/config` hosts work on first connect and can be made ephemeral. Removed the `--drive` subfeature entirely (code + tests).
2026-07-22: Fixed `--proxy-mode --rm` not tearing down on disconnect. OpenSSH sends the ProxyCommand SIGHUP (verified empirically) when the session ends — not just stdin EOF — and Python's default SIGHUP action terminated the process before the teardown `finally` ran, leaking the runtime + keep-alive daemon. Now `--rm` installs SIGHUP/SIGTERM/SIGINT handlers that run the stop (idempotent with the `finally`).
2026-07-23: Applied go/pystyle readability to the ssh code (80-col reflow, Args/Returns/Raises docstrings) and upgraded the integration test from a `--help` smoke into a real end-to-end: it drives a live remote command over `colab ssh --proxy-mode` used as an OpenSSH ProxyCommand (handshake + pubkey-header auth + bridge + remote exec), asserts the RSA-key rejection, and verifies no orphan VM — plus an always-on offline check (help flags + unknown-session exit 2). The live part now auto-runs when auth is present instead of being `RUN_LIVE`-gated.
2026-07-24: Refactored `ssh()` into intent-named helpers (`_select_proxy_session`, `_select_interactive_session`, `_run_proxy_bridge`, `_run_interactive_shell`, `_install_rm_signal_handlers`, `_warn_accelerator_ignored`) — behavior-preserving — and unified the two `--gpu/--tpu ignored` messages into one. Added `tests/test_ssh_lifecycle.py` pinning lifecycle guarantees: `--rm` teardown survives an exception (try/finally), ssh/bridge exit-code propagation, `--proxy-mode` stdout cleanliness (create/`--rm` chatter stays on stderr), auto-create failure aborts before connect, `--gpu`+`--tpu` both forwarded to `colab new`, `--rm` idempotency across the signal + finally paths, and reused-session `--rm` teardown. Trimmed prose in this doc + the integration README.
---

# Design: `colab ssh` — SSH-over-WebSocket Runtime Access

## Motivation
Users want a real shell on their Colab runtime and, more importantly, IDE
remote-development (VS Code Remote-SSH, JetBrains Gateway, plain `ssh`). `colab ssh` is
the client that allows sshing into Colab, reusing the CLI's existing session resolution and
runtime-proxy token so no separate credential handling is needed.

## User Surface

```
colab ssh [OPTIONS]
```

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `-s`, `--session` | str | auto | Session to connect to. If omitted, uses your only active session, auto-creates one when you have none, or errors when you have several. |
| `--proxy-mode` | bool | False | Act as an OpenSSH `ProxyCommand`-compatible WebSocket↔stdio bridge (reads stdin, writes stdout) for `~/.ssh/config`. Every other flag still applies. |
| `-i`, `--identity` | str | auto | Private key for the public key sent to Colab. Default: first of `~/.ssh/id_ed25519`, `id_ecdsa`. |
| `--gpu` | str | None | GPU accelerator for a runtime this command creates (T4, L4, G4, H100, A100). |
| `--tpu` | str | None | TPU accelerator for a runtime this command creates (v5e1, v6e1). |
| `--rm` | bool | False | Stop the runtime when the session ends. Interactive: only a runtime `colab ssh` auto-created (a reused session is never removed). `--proxy-mode`: the bridged session, on disconnect. |

### `~/.ssh/config` usage
`--proxy-mode` turns `colab ssh` into a transport any SSH-based tool can drive:

```
Host <alias>
  ProxyCommand <abs-path-to>/colab ssh --proxy-mode -s <name> [--gpu T4] [--rm]
  User root
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
```

Because every flag applies in `--proxy-mode`, `-s <name>` creates the session on
first connect, `--gpu/--tpu` size it, and `--rm` makes the host ephemeral. Use an
**absolute** `colab` path: `ssh` runs the `ProxyCommand` in a non-login shell
where a bare `colab` may not be on `PATH`. External SSH tools run their own
remote command, so to also land in `/content` add `RequestTTY yes` and
`RemoteCommand cd /content 2>/dev/null; exec bash -l`.

## Behavior

1. **Session resolution / auto-create**: With `-s NAME`, resolves that session
   (via `state.resolve_session`, the same helper the other commands use). Bare
   `colab ssh` uses your only active session; with **no** session it auto-creates
   one (mirrors `colab new` end-to-end: assign → keep-alive pre-flight → spawn
   keep-alive daemon → persist `SessionState`); with **multiple** it errors and
   asks you to pick one with `-s`.
2. **Connect**: Opens the WebSocket to `wss://<netloc>/colab/ssh?colab-runtime-proxy-token=<token>`
   and sends the resolved public key verbatim in the `X-Colab-Ssh-Pubkey` header
   (no transformation -- the bytes the user controls are exactly what the server
   receives). Only `ssh-ed25519` / `ecdsa-sha2-nistp{256,384,521}` keys are
   accepted.
3. **Interactive shell**: Spawns the system `ssh` binary with the CLI re-invoked
   as its own `ProxyCommand` (`python -m colab_cli.cli ssh --proxy-mode`), so the
   WebSocket bridge and the interactive shell share one code path. It forces a
   PTY (`-t`) and runs `cd /content 2>/dev/null; exec $SHELL -l` so you land in
   `/content` (Colab's working dir) rather than root's home; a missing `/content`
   falls back to the login home.
4. **`--proxy-mode` bridge**: Bridges the WebSocket ↔ stdin/stdout for use as an
   OpenSSH `ProxyCommand`. Honors every flag: `-s NAME` creates the session if
   missing (creation output routed to stderr so stdout stays the clean ssh byte
   stream); bare `--proxy-mode` with no `-s` just resolves an existing session.
5. **`--rm` teardown**: Stops the runtime when the session ends. In `--proxy-mode`
   this must survive how OpenSSH ends a `ProxyCommand`: on disconnect it sends
   **SIGHUP** (verified), not just stdin EOF, and Python's default SIGHUP action
   would terminate the process before the teardown `finally` ran — leaking the
   runtime and its keep-alive daemon. `--rm` therefore installs
   SIGHUP/SIGTERM/SIGINT handlers that run the stop, idempotent with the
   `finally`. `SIGKILL` cannot be intercepted, so a `kill -9`/hard crash can
   still leak; a normal disconnect is SIGHUP and is handled.
6. **Error handling**: The WebSocket upgrade maps each common HTTP status to an
   actionable message:

   | Status | Meaning surfaced to the user |
   | --- | --- |
   | 400 | Bad/unsupported/missing pubkey, with remediation (`ssh-keygen -t ed25519`) |
   | 401 | Token invalid/expired — try `colab new` |
   | 403 | Forbidden — token lacks permission for this action (feature-off returns 404, not 403) |
   | 404 | SSH not exposed on this runtime — SSH is baked in at creation, so run `colab new` |
   | 429 | Another `colab ssh` is already connected — disconnect first |
   | 502 | Runtime `sshd` unreachable — runtime may be unhealthy |
   | other / none | Raw status or a network-check hint |


## Testing Strategy (TDD)

### Unit tests (`tests/test_ssh.py`)
1. WebSocket URL construction (`wss` for https, `ws` for http; token query param).
2. Pubkey resolution — `--identity` (via `ssh-keygen -y -f`) and the `~/.ssh`
   default scan; missing-key and missing-identity exit paths.
3. The full status→message map (400/401/403/404/429/502/other/none).
4. Shell quoting for the `ProxyCommand` string.
5. Session resolution (existing vs missing).
6. End-to-end dispatch: interactive vs `--proxy-mode`, including a
   verbatim-pubkey pass-through assertion and the actionable-400 message.

### Wire-contract tests (`tests/test_ssh_wire_contract.py`)
Stands up a loopback WebSocket server and drives the real connect path (no mock)
to assert the request path, the `colab-runtime-proxy-token` query param, and the
`X-Colab-Ssh-Pubkey` header reach the wire verbatim. Includes mutation tests that
fail if `_SSH_PATH`/`_PUBKEY_HEADER` drift, plus real HTTP 400/429 mapping via a
genuine `WebSocketBadStatusException`.

### Auto-create & proxy-mode tests (`tests/test_ssh_autocreate.py`)
Bare `colab ssh` create vs reuse vs ambiguous; `--gpu/--tpu` passthrough; `--rm`
stop-on-exit; and the `--proxy-mode` matrix — create-if-missing with `-s NAME`,
reuse of an existing session, `--gpu` passthrough, `--rm` teardown, and the
SIGHUP cleanup handler being installed only under `--rm`.

### Working-directory tests (`tests/test_ssh_workdir.py`)
Interactive `ssh` forces a PTY (`-t`) and runs a `cd /content` remote command
(host before the command, `2>/dev/null` tolerance for a missing directory).

### Integration test (`integration/repro_ssh/`)
Two parts. An offline smoke that always runs (no VM): ``--help`` advertises the
documented flags, and an unknown session exits 2 with an actionable message. A
live end-to-end that runs when auth is present (allocates a CPU VM): it uses
``colab ssh --proxy-mode`` as an OpenSSH ProxyCommand to run a real remote
command over the WebSocket bridge -- exercising the same connect ->
pubkey-header auth -> handshake -> bridge -> remote-exec path as the interactive
shell, minus the TTY -- asserts the RSA-key rejection, and verifies ``colab
stop`` leaves no orphan VM.
