---
log:
2026-07-17: Initial design and implementation of `colab ssh` — client side of SSH-over-WebSocket runtime access. Adds three modes (interactive shell, `-s SESSION`, and `--proxy-mode` OpenSSH ProxyCommand bridge), `--identity/-i` key selection, and per-HTTP-status handshake error messages. Server side is out of scope for this repo; the subcommand is a no-op against runtimes that do not expose the `/colab/ssh` endpoint (surfaces an actionable HTTP 404 message).
---

# Design: `colab ssh` — SSH-over-WebSocket runtime access

## Motivation
Users want a real shell on their Colab runtime and, more importantly, IDE
remote-development (VS Code Remote-SSH, JetBrains Gateway, plain `ssh`). The
runtime exposes an SSH-over-WebSocket endpoint at `/colab/ssh`; `colab ssh`
is the client that speaks it, reusing the CLI's existing session resolution and
runtime-proxy token so no separate credential handling is needed.

## Modes
1. `colab ssh` — resolve the only active session (via `state.resolve_session`,
   the same helper the other commands use) and open an interactive shell.
2. `colab ssh -s SESSION` — same, targeting `SESSION` explicitly.
3. `colab ssh --proxy-mode -s SESSION` — act as an OpenSSH
   `ProxyCommand`-compatible WebSocket↔stdio bridge. Configure once:
   ```
   Host colab-runtime
     ProxyCommand colab ssh --proxy-mode -s SESSION
   ```
   and any SSH-based tool can reach the runtime.

The interactive mode spawns the system `ssh` binary with the CLI re-invoked as
its own `ProxyCommand` (`python -m colab_cli.cli ssh --proxy-mode`), so the
WebSocket bridge and the interactive shell share one code path.

## Key selection
`--identity/-i` overrides the default order (`~/.ssh/id_ed25519` → `id_ecdsa` →
`id_rsa`). The public key is derived from the private key via
`ssh-keygen -y -f` and sent verbatim in the `X-Colab-Ssh-Pubkey` header — no
transformation, so the bytes the user controls are exactly what the server
receives.

## Error handling
The WebSocket upgrade maps each common HTTP status to an actionable message:

| Status | Meaning surfaced to the user |
| --- | --- |
| 400 | Bad/unsupported/missing pubkey, with remediation (`ssh-keygen -t ed25519`) |
| 401 | Token invalid/expired — try `colab new` |
| 403 | Feature not enabled for this session/tier |
| 404 | Runtime does not expose the SSH endpoint |
| 429 | Another `colab ssh` is already connected — disconnect first |
| 502 | Runtime `sshd` unreachable — runtime may be unhealthy |
| other / none | Raw status or a network-check hint |

## Testing strategy
- **Unit** (`tests/test_ssh.py`, 24 cases): URL construction, pubkey resolution
  (both paths), the full status→message map, shell quoting, session resolution,
  and end-to-end dispatch (interactive vs `--proxy-mode`), including a
  verbatim-pubkey pass-through assertion.
- **Integration** (`integration/repro_ssh/`): a non-interactive help smoke test
  that always runs, plus a documented live end-to-end scenario (guarded behind
  `RUN_LIVE=1`) that requires a runtime exposing the SSH endpoint.
