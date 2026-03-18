# Colab CLI: Agent Guidelines

## Architecture Overview
- **CLI**: Modular `Typer` based entry point in `cli.py` with subcommands in `commands/`.
- **Common**: `common.py` centralizes shared `State` (lazy-loading) and session resolution.
- **Client**: `ColabClient` handles API interactions (assignment, unassignment).
- **Auth**: `GoogleAuth` manages local OAuth2 flow via `InstalledAppFlow`.
- **Runtime**: `ColabRuntime` wraps `jupyter-kernel-client` for execution.
- **State**:
  - `StateStore` persists session metadata in `~/.config/colab-cli/sessions.json`.
  - Persistent settings are in `~/.config/colab-cli/settings.json`.
- **History**: `HistoryLogger` records structured events in `~/.config/colab-cli/history/*.jsonl`.

## Core Mandates
- **Fidelity**: Mimic the robust patterns found in `colab-agent/src/colab_agent/`.
- **Minimalism**: Favor standard library where possible (e.g., `urllib`) while utilizing `Typer` for CLI ergonomics.
- **Piping**: Always consider piped input (`stdin`) vs. interactive TTY.
- **Trace Alignment**: Use `har/colab.sandbox.google.com.har` as the source of truth for API calls.
- **TDD (Test-Driven Development)**: Always implement tests first. Verify they fail before implementing the solution to make them pass. Every design must include a testing strategy and specific test cases.

- **Jupyter Protocol Deviations**: Google Colab uses custom extensions to the Jupyter protocol. Examples include `colab_request` messages over the `iopub` channel and `input_reply` wrapping `colab_reply` payloads on the `stdin` channel. These require monkey-patching or specialized handlers within `jupyter-kernel-client` (e.g., `wsclient.kernel_socket.on_message` interceptors).

- **Integration Testing**: Unit tests and mocks are not enough. Before declaring any feature complete, you MUST perform a real-world, end-to-end integration test against a live Colab environment using the CLI. Never rely solely on mocked unit tests to verify a feature's correctness.
    - Integration tests are located in `integration/` (e.g., `integration/repro_plot_redirection/test.sh`).
    - To run an integration test, use: `uv run bash integration/repro_<name>/test.sh`.
    - `uv run` ensures the `colab` command (entry point) is available in the shell environment.
- **Continuous Improvement**: Whenever the user provides feedback, workflow advice, or corrections, immediately encode that advice into this `AGENTS.md` file. The goal is to learn from review and never repeat the same errors.

## Tools & Workflow
- **Workflow**:
    1.  **Draft**: Plan and start the task. Create a new git branch before working on new features or changes.
    2.  **Refine**: Implement changes and verify with tests and linting. Run tests using `uv run pytest tests/` and resolve any lint errors using `uv run ruff check . --fix`.
    3.  **Finalize**: Ensure everything is complete and correct. **Whenever features are added or behaviors change, you MUST re-review the corresponding design document in `docs/` and update it to reflect the new state. You should also add a brief log entry to the frontmatter of the updated design document with the current date summarizing the change.** Finally, commit the finished changes to the git branch for review.

## Subcommand Workflows
- **Session Management**: `new`, `sessions`, `status`, `stop`.
- **Execution**: `repl`, `exec`, `console`.
- **Files**: `ls`, `rm`, `upload`, `download`, `edit`.
- **Automation**: `auth`, `drivemount`, `install`, `log`, `pay`, `version`, `update`.

## Implementation Principles
1.  **Direct Execution**: Code for `auth`, `drivemount`, etc., should be injected and executed on the VM kernel.
2.  **Contents API**: Use the Jupyter Contents API for file management as seen in the browser traces.
3.  **Transparent Storage**: Local state must be overridable via flags.
4.  **No netrc**: Avoid `netrc` for token persistence in this project.
5.  **Mocking Interactivity**: When testing commands that branch on `stdin.isatty()`, use the `is_stdin_tty` helper in `execution.py` and mock it via `mocker.patch("colab_cli.commands.execution.is_stdin_tty", return_value=...)`. This ensures tests don't hang in CI/agent environments.
6.  **State Isolation**: Always patch the `colab_cli.common.state` singleton in tests to control session persistence and client behavior. Refer to `tests/conftest.py` for the standard global fixture.
7.  **Branch Hygiene**: The `1p-auth` branch contains internal-only logic in `src/colab_cli/auth.py`. **NEVER** merge this implementation or its specialized `tests/test_auth_1p.py` into `main`. Use manual porting or selective checkouts (`git checkout main -- <file>`) to keep versions synchronized while keeping auth logic distinct.
8.  **Fire-and-Forget Architecture**: The Colab CLI is a "fire-and-forget" tool. Avoid using background threads for long-running tasks within the main command flows. For persistent needs such as keep-alive, utilize detached background daemon processes (with PID tracking in the session state).

## Agent Execution Limitations (What I Can vs Cannot Run)
As an AI agent operating via non-interactive shell tools (`run_shell_command`), there are strict limits on what I can test autonomously without human intervention:
- **I CAN Run:**
  - Automated tests (`pytest`), linting (`ruff`), and headless execution scripts.
  - Subcommands that don't pause for user input (e.g., `colab new`, `colab status`, `colab stop`, `colab ls`, `colab install`, `colab exec <file.py>`).
  - Specially crafted mock scripts that simulate timeouts or API calls.
- **I CANNOT Run (Requires User Assistance):**
  - **`colab auth`**: This command relies on the traditional Gcloud fallback `input_request` (via `USE_AUTH_EPHEM='0'`), which prompts the user via Python's `input()` to click a URL, sign in, and paste back an authorization code. My shell tool will hang indefinitely on this `input()`.
  - **`colab drivemount`**: This command prompts the user via `sys.stdin.readline()` (specifically querying `/dev/tty` to ensure input is captured) to press `Enter` after granting OAuth consent in the browser. My shell tool will timeout/hang waiting for `Enter`.
  - **`colab repl` / `colab console`**: These commands drop into interactive TTY modes that require real-time keystroke streaming, which my shell tools cannot support.

Whenever working on interactive commands, I must build the core logic, write mock tests, and explicitly ask the user to run the live test in their terminal to verify success.
