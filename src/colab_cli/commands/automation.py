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

import datetime
import os
import sys
import json
from typing import Optional, List
import typer
from rich.console import Console
from typing_extensions import Annotated

from colab_cli.runtime import ColabRuntime
from colab_cli.contents import ContentsClient
from colab_cli.auth import get_credentials
from colab_cli.utils import get_status_code, render_display_data

_console = Console()


# Default execute() timeout for human-in-the-loop automations (auth /
# drivemount). The kernel goes silent while the user completes a browser
# OAuth flow, which can routinely take 30s+; the upstream 10s default
# raises ``TimeoutError`` mid-flow even though the mount actually succeeds.
# 10 minutes is long enough for any realistic interactive auth ceremony
# without leaving CI hangs unbounded.
INTERACTIVE_AUTOMATION_TIMEOUT_SEC = 600



def run_automation(
    name: str,
    op: str,
    code: str,
    allow_stdin: bool = False,
    path: str = None,
    timeout: Optional[float] = None,
):
    from colab_cli.common import state

    s = state.store.get(name)
    runtime = ColabRuntime(s.url, s.token, session_name=s.name, history=state.history)

    def drivefs_hook(deserialize_msg, wsclient):
        content = deserialize_msg.get("content", {})
        if content.get("request", {}).get("authType") == "dfs_ephemeral":
            msg_id = deserialize_msg.get("metadata", {}).get("colab_msg_id")
            state.history.log_event(
                s.name,
                "colab_request",
                {"type": "dfs_ephemeral", "colab_msg_id": msg_id},
            )
            url = f"{state.client.colab_domain}/tun/m/credentials-propagation/{s.endpoint}"
            params = {
                "authuser": "0",
                "authtype": "dfs_ephemeral",
                "version": "2",
                "dryrun": "true",
                "propagate": "true",
                "record": "false",
            }
            typer.echo(
                f"\n[colab] Intercepted Drive Auth Request. Connecting to {state.client.colab_domain}..."
            )

            creds = get_credentials(
                state.client_oauth_config, provider=state.auth_provider
            )
            resp = creds.request("GET", url, params=params)
            token = (
                json.loads(resp.text.split("\n", 1)[-1]).get("token")
                if get_status_code(resp) == 200
                else None
            )

            headers = {"x-goog-colab-token": token}
            resp = creds.request(
                "POST",
                url,
                params=params,
                headers=headers,
                files={"file_id": (None, "empty.ipynb")},
            )
            data = json.loads(resp.text.split("\n", 1)[-1])

            if not data.get("success"):
                uri = data.get("unauthorized_redirect_uri")
                typer.echo(
                    f"\n[colab] REQUIRED: Google Drive Authorization needed.\nPlease visit:\n\n{uri}\n"
                )
                state.history.log_event(s.name, "drive_auth_needed", {"uri": uri})
                sys.stdout.write("Press Enter after you have granted access... ")
                sys.stdout.flush()
                tty_path = "CON" if os.name == "nt" else "/dev/tty"
                with open(tty_path) as tty:
                    tty.readline()

            typer.echo("[colab] Authorizing VM...")
            params["dryrun"] = "false"
            resp = creds.request(
                "POST",
                url,
                params=params,
                headers=headers,
                files={"file_id": (None, "empty.ipynb")},
            )
            if get_status_code(resp) == 200:
                typer.echo("[colab] Credentials propagated. Resuming mount...")
                state.history.log_event(s.name, "drive_auth_success", {})
                reply = wsclient.session.msg(
                    "input_reply",
                    {"value": {"type": "colab_reply", "colab_msg_id": msg_id}},
                )
                if "header" in deserialize_msg:
                    reply["parent_header"] = deserialize_msg["header"]
                wsclient.stdin_channel.send(reply)
            else:
                typer.echo(
                    f"[colab] Error propagating: {get_status_code(resp)} {resp.text}"
                )
            return True
        return False

    runtime.colab_request_hook = drivefs_hook
    try:
        s.running = f"automation({op})"
        s.last_execution = (
            f"automation:{op}",
            None,
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        state.store.add(s)

        if op == "drivemount":
            state.history.log_event(
                name, "automation", {"op": "drivemount", "path": path, "code": code}
            )
        else:
            state.history.log_event(name, "automation", {"op": op, "code": code})

        outputs = runtime.execute_code(code, allow_stdin=allow_stdin, timeout=timeout)
        state.history.log_event(
            name, "automation_result", {"op": op, "outputs": outputs}
        )

        for out in outputs:
            if "text" in out:
                sys.stdout.write(out["text"])
            elif "data" in out:
                text = render_display_data(out["data"])
                if text is not None:
                    _console.print(text)
            elif out.get("output_type") == "error":
                ename = out.get("ename", "Error")
                evalue = out.get("evalue", "")
                tb = out.get("traceback", [])
                if tb:
                    sys.stderr.write("".join(tb) + "\n")
                else:
                    sys.stderr.write(f"{ename}: {evalue}\n")
    finally:
        s.running = None
        state.store.add(s)
        runtime.stop()


def auth(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
):
    """Authenticate with Google on the VM"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    code = "import os\nos.environ['USE_AUTH_EPHEM'] = '0'\nfrom google.colab import auth\nauth.authenticate_user()"
    typer.echo(f"[colab] Starting Google Auth flow on {name}...")
    run_automation(
        name,
        "auth",
        code,
        allow_stdin=True,
        timeout=INTERACTIVE_AUTOMATION_TIMEOUT_SEC,
    )


def drivemount(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    path: Annotated[str, typer.Argument(help="Mount path")] = "/content/drive",
):
    """Mount Google Drive at path"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    code = f"from google.colab import drive\ndrive.mount('{path}')"
    typer.echo(f"[colab] Mounting Google Drive to '{path}' on {name}...")
    run_automation(
        name,
        "drivemount",
        code,
        allow_stdin=True,
        path=path,
        timeout=INTERACTIVE_AUTOMATION_TIMEOUT_SEC,
    )


def install(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    packages: Annotated[
        Optional[List[str]], typer.Argument(help="Packages to install")
    ] = None,
    requirement: Annotated[
        Optional[str], typer.Option("-r", "--requirement", help="Requirements file")
    ] = None,
):
    """Install python packages on the VM"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    if not packages and not requirement:
        typer.echo("[colab] No packages or requirements specified.")
        raise typer.Exit(1)

    commands = []
    if requirement:
        if not os.path.isfile(requirement):
            typer.echo(f"[colab] Requirements file '{requirement}' not found locally.")
            raise typer.Exit(1)
        contents = ContentsClient(state.store.get(name))
        remote_path = f"content/{os.path.basename(requirement)}"
        contents.upload(requirement, remote_path)
        commands.extend(["-r", f"/{remote_path}"])
    if packages:
        commands.extend(packages)

    cmd_str = ", ".join(f"'{c}'" for c in commands)
    code = f"""
import subprocess, sys
def install():
    packages = [{cmd_str}]
    try:
        subprocess.check_call(['uv', 'pip', 'install', '--system'] + packages)
        print('Installation Complete (via uv)!')
    except:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install'] + packages)
        print('Installation Complete (via pip)!')
install()
"""
    typer.echo(f"[colab] Installing packages on {name} (preferring uv)...")
    run_automation(name, "install", code)


def register(app: typer.Typer):
    app.command(hidden=True)(auth)
    app.command()(drivemount)
    app.command()(install)
