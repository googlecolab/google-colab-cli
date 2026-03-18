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

import os
from datetime import datetime, timezone
from typing import Optional

import click
import typer
from typing_extensions import Annotated

from colab_cli.common import state, setup_logging
from colab_cli.commands import session, execution, files, automation, utility

app = typer.Typer(
    help="Colab CLI",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def callback(
    ctx: typer.Context,
    client_oauth_config: Annotated[
        str,
        typer.Option(
            "-c", "--client-oauth-config", help="Path to client OAuth config JSON file"
        ),
    ] = os.path.expanduser("~/.colab-cli-oauth-config.json"),
    config: Annotated[
        Optional[str],
        typer.Option(
            "--config",
            help="Path to session state file (~/.config/colab-cli/sessions.json)",
        ),
    ] = None,
    logtostderr: Annotated[
        bool, typer.Option("--logtostderr", help="Log all output to stderr")
    ] = False,
    auth_loas2: Annotated[
        bool,
        typer.Option(
            "--auth-loas2/--auth-oauth2", help="Authentication strategy to use"
        ),
    ] = True,
):
    """
    Colab CLI global configuration.
    """
    state.client_oauth_config = client_oauth_config
    state.config_path = config
    state.logtostderr = logtostderr
    state.auth_loas2 = auth_loas2
    setup_logging(logtostderr)

    # Check for updates once a day, unless we are already running the update command
    if ctx.invoked_subcommand != "update":
        settings = state.settings_store.load()
        if settings.enable_auto_update:
            now = datetime.now(timezone.utc)
            if settings.last_check is None or (now - settings.last_check).days >= 1:
                from colab_cli.commands.utility import check_for_updates

                check_for_updates(quiet=True)


@app.command(name="help")
def help_command(
    ctx: typer.Context,
    command: Annotated[
        Optional[str], typer.Argument(help="Command to show help for")
    ] = None,
):
    """
    Show help for a command.
    """
    if not command:
        typer.echo(ctx.parent.get_help())
        return

    group = ctx.parent.command
    cmd = group.get_command(ctx, command)
    if cmd is None:
        typer.echo(f"No such command '{command}'.", err=True)
        raise typer.Exit(code=2)

    with click.Context(cmd, info_name=command, parent=ctx.parent) as cmd_ctx:
        typer.echo(cmd.get_help(cmd_ctx))


# Register subcommands
session.register(app)
execution.register(app)
files.register(app)
automation.register(app)
utility.register(app)


def main():
    app()


if __name__ == "__main__":
    main()
