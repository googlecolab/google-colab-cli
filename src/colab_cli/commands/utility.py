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

import json
import subprocess
import urllib.request
from datetime import datetime, timezone
from importlib.metadata import version, PackageNotFoundError
from typing import Optional

import typer
from typing_extensions import Annotated

from colab_cli.common import state


def pay():
    """Open the Colab signup page to manage compute units"""
    import webbrowser

    url = "https://colab.research.google.com/signup"
    typer.echo(f"[colab] Opening {url}...")
    webbrowser.open(url)


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
            else:
                typer.echo(f"[{ts}] EVENT: {etype}")


def get_app_version():
    package_name = "colab"

    # 1. Try to get version from installed metadata (.whl)
    try:
        return version(package_name)
    except PackageNotFoundError:
        pass

    # 2. Fallback: Get Git hash if running from source
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
        ).strip()
    except Exception:
        return "unknown"


def version_command():
    """Show the version of the Colab CLI"""
    typer.echo(f"Version: {get_app_version()}")


def check_for_updates(quiet: bool = False):
    """Check for updates and print a message if a new version is available."""
    settings = state.settings_store.load()
    current_version = get_app_version()

    try:
        data = None
        try:
            with urllib.request.urlopen(settings.update_url, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as e:
            if not quiet:
                typer.echo(f"[colab] Warning: Failed to fetch update info: {e}")

        latest_version = data.get("info", {}).get("version") if data else None

        is_newer = False
        if latest_version:
            from packaging.version import Version, InvalidVersion

            try:
                latest_parsed = Version(latest_version)
                current_parsed = Version(current_version)
                if latest_parsed > current_parsed:
                    is_newer = True
            except InvalidVersion:
                if latest_version != current_version:
                    is_newer = True

        if is_newer:
            typer.echo(
                f"\n[colab] A new version of Colab CLI is available: {latest_version} (current: {current_version})"
            )
            typer.echo("[colab] Run 'pip install --upgrade colab' to update.\n")
        elif not quiet:
            typer.echo(f"[colab] Colab CLI is up to date (version: {current_version}).")

        settings.last_check = datetime.now(timezone.utc)
        state.settings_store.save(settings)

    except Exception as e:
        if not quiet:
            typer.echo(f"[colab] Failed to check for updates: {e}")


def update_command(
    force: Annotated[
        bool, typer.Option("--force", help="Force a version check")
    ] = True,
):
    """Check for latest version and print if an update is available"""
    check_for_updates(quiet=False)


def register(app: typer.Typer):
    app.command()(pay)
    app.command()(log)
    app.command(name="version")(version_command)
    app.command(name="update")(update_command)
