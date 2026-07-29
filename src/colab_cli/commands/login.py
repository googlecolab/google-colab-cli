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
import os
from urllib.parse import parse_qs, urlparse

import typer
from typing_extensions import Annotated

from colab_cli.auth import (
    LOGIN_LOCK_PATH,
    TOKEN_CONFIG_PATH,
    _is_lock_expired,
    start_remote_flow,
    complete_remote_flow,
)

login_app = typer.Typer(
    help="2-step OAuth2 login flow for Colab CLI.",
    no_args_is_help=True,
)


@login_app.command(name="start")
def login_start(
    client_oauth_config: Annotated[
        str,
        typer.Option(
            "-c", "--client-oauth-config", help="Path to client OAuth config JSON file"
        ),
    ] = os.path.expanduser("~/.colab-cli-oauth-config.json"),
):
    """Start the 2-step login flow.

    Generates an authorization URL and persists intermediate state to a lock
    file. Visit the printed URL in any browser, approve access, and then run
    ``colab login verify <code>`` with the authorization code shown on the
    landing page.
    """
    try:
        auth_url, lock_data = start_remote_flow(client_oauth_config)
    except FileNotFoundError as e:
        typer.echo(f"[colab] {e}", err=True)
        raise typer.Exit(code=1)

    typer.echo("\n[colab] Step 1 of 2: authorize this CLI", err=True)
    typer.echo("")
    typer.echo("Visit this URL in any browser:\n", err=True)
    typer.echo(f"  {auth_url}\n", err=True)
    typer.echo("After approving, Google will display an authorization code.", err=True)
    typer.echo("Then run: colab login verify <code> OR colab login verify \"<full-url>\"\n", err=True)


@login_app.command(name="verify")
def login_verify(
    code_or_url: Annotated[
        str,
        typer.Argument(
            help="Authorization code from Google, or the full redirect URL after approval"
        ),
    ],
    client_oauth_config: Annotated[
        str,
        typer.Option(
            "-c", "--client-oauth-config", help="Path to client OAuth config JSON file"
        ),
    ] = os.path.expanduser("~/.colab-cli-oauth-config.json"),
):
    """Verify the authorization code and complete login.

    Accepts either the raw authorization code shown on Google's landing page,
    or the full redirect URL from the browser address bar after approval.
    Reads the lock file written by ``colab login start``, exchanges the code
    for credentials, and writes them to the token store. Any existing cached
    token is overwritten.
    """
    if code_or_url.startswith("http"):
        parsed = urlparse(code_or_url)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        if not code:
            typer.echo(
                "[colab] Could not extract authorization code from URL.",
                err=True,
            )
            raise typer.Exit(code=1)

        returned_state = params.get("state", [None])[0]
        if not returned_state:
            typer.echo(
                "[colab] Could not extract state from URL.",
                err=True,
            )
            raise typer.Exit(code=1)
    else:
        code = code_or_url.strip()
        returned_state = None

    lock_data = None
    try:
        with open(LOGIN_LOCK_PATH, "r") as f:
            lock_data = json.load(f)
    except FileNotFoundError:
        typer.echo(
            "[colab] No pending login session found. Run `colab login start` first.",
            err=True,
        )
        raise typer.Exit(code=1)
    except json.JSONDecodeError:
        try:
            os.remove(LOGIN_LOCK_PATH)
        except OSError:
            pass
        typer.echo(
            "[colab] Lock file is corrupted. Run `colab login start` to begin a new login session.",
            err=True,
        )
        raise typer.Exit(code=1)

    if _is_lock_expired(lock_data):
        try:
            os.remove(LOGIN_LOCK_PATH)
        except OSError:
            pass
        typer.echo(
            "[colab] Login session has expired. Run `colab login start` to begin a new session.",
            err=True,
        )
        raise typer.Exit(code=1)

    stored_state = lock_data.get("state")
    if returned_state is not None and stored_state and returned_state != stored_state:
        try:
            os.remove(LOGIN_LOCK_PATH)
        except OSError:
            pass
        typer.echo(
            "[colab] State mismatch: the authorization response does not match the pending login session.",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        complete_remote_flow(LOGIN_LOCK_PATH, code, client_oauth_config, lock_data=lock_data, state=returned_state)
    except FileNotFoundError as e:
        typer.echo(f"[colab] {e}", err=True)
        typer.echo("Run `colab login start` to begin a new login session.", err=True)
        raise typer.Exit(code=1)
    except Exception as e:
        typer.echo(f"[colab] Login verification failed ({type(e).__name__}): {e}", err=True)
        try:
            if os.path.exists(LOGIN_LOCK_PATH):
                os.remove(LOGIN_LOCK_PATH)
        except OSError:
            pass
        raise typer.Exit(code=1)

    typer.echo("[colab] Login complete.")
    typer.echo(f"[colab] Credentials saved to {TOKEN_CONFIG_PATH}")


def register(app: typer.Typer):
    app.add_typer(login_app, name="login")
