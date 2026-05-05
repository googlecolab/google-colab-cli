# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import platform
from typing import Optional

import typer
from typing_extensions import Annotated

from colab_cli import auto_update
from colab_cli.auto_update import get_app_version
from colab_cli.common import state


def pay():
    """Open the Colab signup page to manage compute units"""
    import webbrowser

    url = "https://colab.research.google.com/signup"
    typer.echo(f"[colab] Opening {url}...")
    webbrowser.open(url)


def url(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help=(
                "Colab frontend host (origin) to use for the URL. The Colab "
                "frontend resolves `dbu` against `window.location.origin` "
                "(see google3 traits.ts:359), so this only changes the page "
                "origin, not the embedded backend path."
            ),
        ),
    ] = "https://colab.research.google.com",
    open_browser: Annotated[
        bool,
        typer.Option(
            "--open",
            help=(
                "After printing the URL, also open it in the system browser. "
                "Off by default so the command remains pipeable "
                "(e.g. `colab url -s s1 | xclip`)."
            ),
        ),
    ] = False,
):
    """Print a browser URL that connects to an existing session.

    Format: ``https://<host>/notebooks/empty.ipynb?dbu=<urlencoded path>``,
    where the path is ``/tun/m/<endpoint>``. When opened, the Colab frontend
    skips ``/tun/m/assign`` and attaches the kernel to our existing VM.

    The ``dbu`` query parameter is the mendel development flag
    ``datalab_backend_url`` (see
    google3/googledata/experiments/colab/features/development_flags.gcl:589
    and research/colab/frontend/common/model/traits.ts:359). Because it's a
    development flag, URL-overriding it is gated to sandbox users and
    Googlers; external users would need a different (hash-based) form.
    Tracked in b/509662345.
    """
    # Imported here (not at module top) to mirror the lazy-state pattern used
    # elsewhere in this module and avoid a circular import via colab_cli.common.
    from urllib.parse import quote

    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.", err=True)
        raise typer.Exit(code=1)

    # Strip a trailing slash so we don't produce `https://host//notebooks/...`.
    host_clean = host.rstrip("/")
    # `dbu` value is the path `/tun/m/<endpoint>`. URL-encode it (incl. the
    # slashes via `safe=""`) so the value survives any downstream non-strict
    # query-string re-parsing — this is also the form shown in real Colab
    # connect URLs in the wild.
    dbu_value = quote(f"/tun/m/{s.endpoint}", safe="")
    connect_url = f"{host_clean}/notebooks/empty.ipynb?dbu={dbu_value}"

    # Print the URL on its own line with no `[colab]` prefix so the output
    # is pipeable (`colab url -s s1 | xclip`, etc.).
    typer.echo(connect_url)

    if open_browser:
        import webbrowser

        webbrowser.open(connect_url)


def log(
    session: Annotated[
        Optional[str],
        typer.Option(
            "-s",
            "--session",
            help="Session name (if omitted, lists all sessions with logs)",
        ),
    ] = None,
    lines: Annotated[
        Optional[int],
        typer.Option(
            "-n", "--lines", help="Number of lines to show/export (default: all)"
        ),
    ] = None,
    type: Annotated[
        Optional[str],
        typer.Option(
            "-t",
            "--type",
            help="Filter by event type (e.g., execution, file_operation)",
        ),
    ] = None,
    output: Annotated[
        Optional[str],
        typer.Option(
            "-o",
            "--output",
            help="Output file path (suffix determines format: .ipynb, .md, .txt, .jsonl)",
        ),
    ] = None,
):
    """Manage and view session history logs"""
    if not session:
        sessions_with_logs = state.history.list_sessions()
        if not sessions_with_logs:
            typer.echo("[colab] No session history found.")
        else:
            typer.echo("[colab] Sessions with history logs:")
            for n in sorted(sessions_with_logs):
                typer.echo(f"  {n}")
        return

    events = state.history.get_history(session)
    if not events:
        typer.echo(f"[colab] No history found for session '{session}'.")
        return

    if type:
        events = [e for e in events if e.get("event_type") == type]

    if lines:
        events = events[-lines:]

    if output:
        from colab_cli.converter import export_history

        export_history(events, session, output)
    else:
        for event in events:
            ts = event.get("timestamp", "").split(".")[0].replace("T", " ")
            etype = event.get("event_type", "unknown")

            if etype == "execution":
                preview = event.get("code", "").strip().split("\n")[0][:60]
                typer.echo(f"[{ts}] EXEC: {preview}...")
            elif etype == "file_operation":
                typer.echo(
                    f"[{ts}] FILE: {event.get('op')} {event.get('path', event.get('remote', ''))}"
                )
            elif etype == "automation":
                typer.echo(f"[{ts}] AUTO: {event.get('op')}")
            elif etype == "stdin_request":
                typer.echo(f"[{ts}] INPT: {event.get('prompt', '').strip()}")
            elif etype == "input_reply":
                typer.echo(f"[{ts}] RPLY: {event.get('value', '').strip()}")
            elif etype == "keep_alive_started":
                typer.echo(
                    f"[{ts}] KEEP: started endpoint={event.get('endpoint')} pid={event.get('pid')}"
                )
            elif etype == "keep_alive_error":
                msg = (
                    f"[{ts}] KEEP: error iter={event.get('iteration')} "
                    f"status={event.get('status_code')} "
                    f"type={event.get('error_type')} "
                    f"msg={event.get('error', '')[:120]}"
                )
                body = event.get("response_body")
                if body:
                    msg += f" body={body[:300]}"
                typer.echo(msg)
            elif etype == "keep_alive_stopped":
                msg = (
                    f"[{ts}] KEEP: stopped reason={event.get('reason')} "
                    f"iters={event.get('iterations')} "
                    f"duration={event.get('duration_seconds')}s"
                )
                last_err = event.get("last_error")
                if last_err:
                    msg += (
                        f" last_error=[status={last_err.get('status_code')} "
                        f"type={last_err.get('error_type')} "
                        f"msg={str(last_err.get('error', ''))[:120]}]"
                    )
                if event.get("expected_endpoint") or event.get("actual_endpoint"):
                    msg += (
                        f" expected={event.get('expected_endpoint')} "
                        f"actual={event.get('actual_endpoint')}"
                    )
                typer.echo(msg)
            else:
                typer.echo(f"[{ts}] EVENT: {etype}")


def version_command():
    """Show the version of the Colab CLI"""
    typer.echo(f"Version: {get_app_version()}")


def update_command(
    install: Annotated[
        bool,
        typer.Option(
            "--install",
            help=(
                "After checking, run 'uv tool install <URL>' if a newer "
                "release with a download URL is available from the local "
                "update file. Linux only."
            ),
        ),
    ] = False,
):
    """Check for latest version and print if an update is available"""
    release_url = auto_update.check_for_updates(quiet=False)
    if not install:
        return

    if platform.system() != "Linux":
        typer.echo(
            "[colab] '--install' self-install is only supported on Linux.", err=True
        )
        raise typer.Exit(code=1)

    if not release_url:
        # No newer local-file release with a URL → nothing to install.
        return

    auto_update.self_install(release_url)


def register(app: typer.Typer):
    app.command()(pay)
    app.command()(log)
    app.command(name="url")(url)
    app.command(name="version")(version_command)
    app.command(name="update")(update_command)
