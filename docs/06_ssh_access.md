---
log:
2026-07-17: Initial design and implementation of `colab ssh` — client side of SSH-over-WebSocket runtime access. Adds three modes (interactive shell, `-s SESSION`, and `--proxy-mode` OpenSSH ProxyCommand bridge), `--identity/-i` key selection, and per-HTTP-status handshake error messages. Server side is out of scope for this repo; the subcommand is a no-op against runtimes that do not expose the `/colab/ssh` endpoint (surfaces an actionable HTTP 404 message).
2026-07-22: Bare `colab ssh` now auto-creates a runtime (via `colab new`) when you have no active session, with `--gpu/--tpu` passthrough and `--rm` to stop an auto-created runtime on exit. Fixed two client bugs: the dead 403 branch (feature-off returns 404, not 403) and the RSA guidance (all `ssh-rsa` keys are server-rejected, so `id_rsa` is no longer auto-scanned and the 400 message no longer advertises `rsa-sha2`). Added `tests/test_ssh_wire_contract.py` (real loopback-server wire assertions) and `tests/test_ssh_autocreate.py`.
2026-07-22: Interactive `colab ssh` now starts in `/content` (Colab's working dir) instead of `/root`, via a forced PTY (`-t`) plus a remote `cd /content 2>/dev/null; exec $SHELL -l`. A missing `/content` falls back to the login home. Added `tests/test_ssh_workdir.py`.
2026-07-22: `--proxy-mode` now honors every `colab ssh` flag: with `-s NAME` it creates the session if missing (creation output routed to stderr so stdout stays the clean ssh byte stream), `--gpu/--tpu` set the accelerator, and `--rm` stops the bridged session on disconnect — so `~/.ssh/config` hosts work on first connect and can be made ephemeral. Removed the `--drive` subfeature entirely (code + tests).
2026-07-22: Fixed `--proxy-mode --rm` not tearing down on disconnect. OpenSSH sends the ProxyCommand SIGHUP (verified empirically) when the session ends — not just stdin EOF — and Python's default SIGHUP action terminated the process before the teardown `finally` ran, leaking the runtime + keep-alive daemon. Now `--rm` installs SIGHUP/SIGTERM/SIGINT handlers that run the stop (idempotent with the `finally`).
---

# Design: `colab ssh` — SSH-over-WebSocket runtime access

## Motivation
Users want a real shell on their Colab runtime and, more importantly, IDE
remote-development (VS Code Remote-SSH, JetBrains Gateway, plain `ssh`). The
runtime exposes an SSH-over-WebSocket endpoint at `/colab/ssh`; `colab ssh`
is the client that speaks it, reusing the CLI's existing session resolution and
runtime-proxy token so no separate credential handling is needed.

## Modes
1. `colab ssh` — use the only active session (via `state.resolve_session`, the
   same helper the other commands use) and open an interactive shell. If you
   have **no** active session it auto-creates one (like `colab new`) and sshes
   in; if you have **multiple** it errors and asks you to pick one with `-s`.
2. `colab ssh -s SESSION` — same, targeting `SESSION` explicitly.
3. `colab ssh --proxy-mode -s SESSION` — act as an OpenSSH
   `ProxyCommand`-compatible WebSocket↔stdio bridge for `~/.ssh/config`, so any
   SSH-based tool can reach the runtime. **Every flag below also applies here**:
   with `-s NAME` the session is created if missing (so a config host works on
   first connect), `--gpu/--tpu` set its accelerator, and `--rm` stops it on
   disconnect.

The interactive mode spawns the system `ssh` binary with the CLI re-invoked as
its own `ProxyCommand` (`python -m colab_cli.cli ssh --proxy-mode`), so the
WebSocket bridge and the interactive shell share one code path.

## Working directory
Interactive `colab ssh` lands you in `/content` (Colab's standard working
directory, where notebooks and uploads live) rather than root's home. It forces
a PTY (`-t`) and runs `cd /content 2>/dev/null; exec $SHELL -l` on the runtime;
a missing `/content` falls back to the login home. `--proxy-mode` and external
SSH tools run their own remote commands, so to also land in `/content` from a
`~/.ssh/config` host add `RequestTTY yes` and
`RemoteCommand cd /content && exec bash -l`.

## Auto-create options
When bare `colab ssh` creates a runtime for you (no active session):
- `--gpu T4|L4|G4|H100|A100` / `--tpu v5e1|v6e1` — request an accelerator for the
  new runtime (defaults to CPU). Ignored when an existing session is reused.
- `--rm` — stop the runtime when the session ends. In interactive mode this
  applies only to a runtime `colab ssh` auto-created (a reused session is never
  removed); in `--proxy-mode` it stops the bridged session on disconnect.

In `--proxy-mode`, `-s NAME` creates the session if missing (creation output is
routed to stderr so stdout stays the clean ssh byte stream); bare `--proxy-mode`
with no `-s` still just resolves an existing session.

## Key selection
`--identity/-i` overrides the default order (`~/.ssh/id_ed25519` → `id_ecdsa`).
RSA keys are rejected by the server (a `.pub` type token is always `ssh-rsa`,
which is not accepted), so `id_rsa` is not auto-selected. The public key is
derived from the private key via `ssh-keygen -y -f` and sent verbatim in the
`X-Colab-Ssh-Pubkey` header — no transformation, so the bytes the user controls
are exactly what the server receives.

## Error handling
The WebSocket upgrade maps each common HTTP status to an actionable message:

| Status | Meaning surfaced to the user |
| --- | --- |
| 400 | Bad/unsupported/missing pubkey, with remediation (`ssh-keygen -t ed25519`) |
| 401 | Token invalid/expired — try `colab new` |
| 403 | Forbidden — token lacks permission for this action (feature-off returns 404, not 403) |
| 404 | SSH not exposed on this runtime — SSH is baked in at creation, so run `colab new` |
| 429 | Another `colab ssh` is already connected — disconnect first |
| 502 | Runtime `sshd` unreachable — runtime may be unhealthy |
| other / none | Raw status or a network-check hint |

## Testing strategy
- **Unit** (`tests/test_ssh.py`): URL construction, pubkey resolution (both
  paths), the full status→message map, shell quoting, session resolution, and
  end-to-end dispatch (interactive vs `--proxy-mode`), including a
  verbatim-pubkey pass-through assertion.
- **Wire contract** (`tests/test_ssh_wire_contract.py`): stands up a loopback
  WebSocket server and drives the real connect path (no mock) to assert the
  request path, the `colab-runtime-proxy-token` query param, and the
  `X-Colab-Ssh-Pubkey` header reach the wire verbatim; includes mutation tests
  that fail if `_SSH_PATH`/`_PUBKEY_HEADER` drift, plus real HTTP 400/429 mapping.
- **Auto-create** (`tests/test_ssh_autocreate.py`): bare-`colab ssh` create vs
  reuse vs ambiguous, `--gpu/--tpu` passthrough, and `--rm` stop-on-exit.
- **Integration** (`integration/repro_ssh/`): a non-interactive help smoke test
  that always runs, plus a documented live end-to-end scenario (guarded behind
  `RUN_LIVE=1`) that requires a runtime exposing the SSH endpoint.
