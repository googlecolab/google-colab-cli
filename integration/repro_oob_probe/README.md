# repro_oob_probe

Probes Google's OAuth2 server to determine which "no-localhost" copy-paste
flows it accepts for the colab-cli OAuth client(s).

## `probe.py` (autonomous, GET-only)

Builds the remote copy-paste authorization URL exactly as `auth.py:_run_remote_flow`
does (registered HTTPS landing page `sdk.cloud.google.com/...` +
`token_usage=remote`) and issues a single HTTP GET to the authorize endpoint,
then reports whether Google reached sign-in/consent or rejected the request.
Allocates no resources.

```
uv run python integration/repro_oob_probe/probe.py
```

Findings (2026-06-11):
- Bundled cloud-SDK client (`764086051850-...`) + `sdk.cloud.google.com`
  redirect + `token_usage=remote` -> **ACCEPTED** (reaches sign-in).
- OOB redirect (`urn:ietf:wg:oauth:2.0:oob`) -> **REJECTED**
  ("The out-of-band (OOB) flow has been blocked").
- A non-bundled client (`366568267421-...`) reusing the `sdk.cloud.google.com`
  redirect -> **REJECTED** (`redirect_uri_mismatch`).

## `verify_remote_exchange.py` (interactive, requires browser + paste)

Completes the full `code -> token` exchange to prove the flow works end to end.
Cannot run unattended (`input()` prompt).

```
uv run python integration/repro_oob_probe/verify_remote_exchange.py
```
