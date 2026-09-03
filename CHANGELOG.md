# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The package version is derived from the git tag via `hatch-vcs`; each release
below corresponds to a tag of the same name.

## [0.7.0] - 2026-09-03

### Changed

- **deps:** Bump cryptography from 49.0.0 to 50.0.0 (#98), and pyasn1 from 0.6.3 to 0.6.4 (#87).
- **lockfile:** Upgrade lockfile dependencies via `uv lock --upgrade`. (#67)
- **skill:** Move the CLI help skill details into a dedicated `SKILL.md`. (#55)
- **docs:** Update `AGENTS.md` with instructions for the release tagging workflow. (#66)

### Added

- **ssh:** Add `colab ssh` command to provide secure SSH-over-WebSocket direct access to the Colab runtime VM. (#88)
- **execution:** Add `--env KEY=VALUE` flag to `colab run` and `colab exec` commands to allow injecting custom environment variables into the running kernel. (#65)
- **session:** Add `--high-mem` flag to machine shape selection when creating a new session to support high-RAM resources. (#105)

### Fixed

- **session:** Surface a friendly error message on 412 GPU/TPU allocation failure instead of printing a raw traceback. (#112)
- **runtime:** Support both `ColabKernelClient` and standard `KernelClient` in the runtime layer to improve compatibility across environments. (#95)

## [0.6.0] - 2026-06-16

### Changed

- **auth:** OAuth2 login now uses a remote copy-paste flow instead of a
  localhost callback server. The CLI prints an authorization URL with
  `redirect_uri=https://sdk.cloud.google.com/applicationdefaultauthcode.html`
  and `token_usage=remote`, then reads the pasted code from stdin. This works
  in headless/remote environments where a browser cannot reach a local
  callback port. (#54)

### Added

- **display output:** Rich rendering for `display_data` output via a shared
  `render_display_data()` helper. HTML is converted with `html2text` and
  rendered as Markdown, following a `text/markdown > text/html > text/plain`
  priority; `text/plain` is wrapped with `Text.from_ansi` to preserve embedded
  ANSI escapes. Applied consistently across `exec`, `console`/`repl`, and
  automation call sites. (#58)

### Fixed

- **keep-alive:** Replace the `RuntimeService/KeepAliveAssignment` RPC on
  `colab.pa.googleapis.com` with a Tunnel Frontend (TFE) HTTP ping
  (`GET /tun/m/<endpoint>/keep-alive/` with `X-Colab-Tunnel: Google`) on
  `colab.research.google.com`, authenticated by the user's own bearer token.
  The old RPC required `serviceusage` consumer access to Colab's internal
  project and returned HTTP 403 `USER_PROJECT_DENIED` for every external user,
  causing their sessions to be idle-pruned within minutes. The TFE ping needs
  no project entitlement; because the VM often does not answer on this path, a
  `ReadTimeout` is treated as success while genuine HTTP errors propagate.
  (#14, #61)

### Removed

- Dead grpc-web client-registry / API-key code path and the now-irrelevant
  `colaboratory`-scope / `pa.googleapis.com` pre-flight remediation messaging,
  superseded by the TFE keep-alive ping. (#61)

[0.7.0]: https://github.com/googlecolab/google-colab-cli/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/googlecolab/google-colab-cli/compare/v0.5.11...v0.6.0
