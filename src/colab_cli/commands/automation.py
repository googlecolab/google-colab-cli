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


def _parse_propagation_response(resp) -> dict:
    """Extract the JSON payload from a credentials-propagation response.

    Successful bodies are ``<prefix>\\n{json}``. Failures can return HTML, a
    plain-text reason, or an empty body — none of those should raise, because
    the exception would escape the websocket hook and leave the kernel waiting.
    """
    text = getattr(resp, "text", "") or ""
    for candidate in (text, text.split("\n", 1)[-1], text.rsplit("\n", 1)[-1]):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def drive_propagation_failure_message(status: Optional[int], data: dict) -> str:
    """Actionable text for a failed Drive credential propagation.

    The usual cause is a browser that approved Drive access for a different
    Google account than the one the CLI authenticated as. The raw response
    body (often HTML) is never echoed; it goes to the history log instead.
    """
    headline = "[colab] Drive credentials could not be propagated"
    if status is not None and status != 200:
        headline += f" (HTTP {status})"
    lines = [headline + "."]
    detail = data.get("error") or data.get("message")
    if isinstance(detail, str) and detail.strip():
        lines.append(f"[colab] Server said: {detail.strip()[:300]}")
    lines.append("[colab] If the browser showed an account chooser, re-run the command")
    lines.append("[colab] and select the account reported by `colab whoami`. Approving")
    lines.append(
        "[colab] Drive access as a different Google account makes propagation fail"
    )
    lines.append("[colab] even though the consent itself succeeded.")
    return "\n".join(lines)


def _report_propagation_failure(history, session_name, status, body, data) -> None:
    typer.echo(drive_propagation_failure_message(status, data))
    history.log_event(
        session_name,
        "drive_auth_failed",
        {"status": status, "body": (body or "")[:2000]},
    )


def _send_colab_reply(wsclient, deserialize_msg, msg_id) -> None:
    """Answer the intercepted request so the kernel stops waiting."""
    reply = wsclient.session.msg(
        "input_reply",
        {"value": {"type": "colab_reply", "colab_msg_id": msg_id}},
    )
    if "header" in deserialize_msg:
        reply["parent_header"] = deserialize_msg["header"]
    wsclient.stdin_channel.send(reply)


def run_automation(
    name: str,
    op: str,
    code: str,
    allow_stdin: bool = False,
    path: str = None,
    timeout: Optional[float] = None,
):
    from colab_cli.common import state

    s = state.get_session(name)
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
            data = _parse_propagation_response(resp)
            token = data.get("token") if get_status_code(resp) == 200 else None
            if not token:
                _report_propagation_failure(
                    state.history, s.name, get_status_code(resp), resp.text, data
                )
                _send_colab_reply(wsclient, deserialize_msg, msg_id)
                return True

            headers = {"x-goog-colab-token": token}
            resp = creds.request(
                "POST",
                url,
                params=params,
                headers=headers,
                files={"file_id": (None, "empty.ipynb")},
            )
            data = _parse_propagation_response(resp)

            if not data.get("success"):
                uri = data.get("unauthorized_redirect_uri")
                if not uri:
                    # A failure that is not an auth challenge cannot be fixed
                    # by approving again; report it instead of looping.
                    _report_propagation_failure(
                        state.history,
                        s.name,
                        get_status_code(resp),
                        resp.text,
                        data,
                    )
                    _send_colab_reply(wsclient, deserialize_msg, msg_id)
                    return True
                typer.echo(
                    f"\n[colab] REQUIRED: Google Drive Authorization needed.\nPlease visit:\n\n{uri}\n"
                )
                state.history.log_event(s.name, "drive_auth_needed", {"uri": uri})
                sys.stdout.write("Press Enter after you have granted access... ")
                sys.stdout.flush()
                try:
                    with open("/dev/tty") as tty:
                        tty.readline()
                except (FileNotFoundError, OSError, IOError):
                    # Windows has no /dev/tty; fall back to stdin.
                    try:
                        sys.stdin.readline()
                    except Exception:
                        try:
                            input()
                        except Exception:
                            pass

            typer.echo("[colab] Authorizing VM...")
            params["dryrun"] = "false"
            resp = creds.request(
                "POST",
                url,
                params=params,
                headers=headers,
                files={"file_id": (None, "empty.ipynb")},
            )
            status = get_status_code(resp)
            if status == 200:
                typer.echo("[colab] Credentials propagated. Resuming mount...")
                state.history.log_event(s.name, "drive_auth_success", {})
            else:
                _report_propagation_failure(
                    state.history,
                    s.name,
                    status,
                    resp.text,
                    _parse_propagation_response(resp),
                )
            _send_colab_reply(wsclient, deserialize_msg, msg_id)
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
        contents = ContentsClient(state.get_session(name))
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
