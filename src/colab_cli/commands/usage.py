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

import typer

from colab_cli.consumption import format_consumption_status


def usage():
    """Show account compute-unit usage rate and balance"""
    from colab_cli.common import state

    try:
        result = state.client.get_consumption_user_info()
    except Exception as e:
        typer.echo(
            f"[colab] Warning: failed to fetch compute-unit info: {e}",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(format_consumption_status(result))


def register(app: typer.Typer):
    app.command()(usage)
