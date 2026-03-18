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

import subprocess
import sys
import time
import uuid
from typing import Optional
import typer
from typing_extensions import Annotated

from colab_cli.client import (
    Accelerator,
    PostAssignmentResponse,
    Variant,
)
from colab_cli.utils import get_status_code
from colab_cli.state import SessionState
from colab_cli.runtime import ColabRuntime


def new(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    tpu: Annotated[Optional[str], typer.Option(help="TPU variant")] = None,
    gpu: Annotated[Optional[str], typer.Option(help="GPU variant")] = None,
):
    """Create a new session"""
    from colab_cli.common import state

    name = session or uuid.uuid4().hex[:6]
    variant = Variant.DEFAULT
    accelerator = Accelerator.NONE

    if tpu:
        variant = Variant.TPU
        accelerator = Accelerator.V5E1 if tpu == "v5e1" else Accelerator.V6E1
    elif gpu:
        variant = Variant.GPU
        mapping = {
            "A100": Accelerator.A100,
            "H100": Accelerator.H100,
            "l4": Accelerator.L4,
            "t4": Accelerator.T4,
            "g4": Accelerator.T4,
        }
        accelerator = mapping.get(gpu, Accelerator.A100)

    typer.echo(f"[colab] Creating session '{name}'...")
    res = state.client.assign(uuid.uuid4(), variant=variant, accelerator=accelerator)

    if isinstance(res, PostAssignmentResponse):
        token = res.runtime_proxy_info.token
        url = res.runtime_proxy_info.url
        endpoint = res.endpoint
    else:
        token = (
            res.runtime_proxy_info.token
            if hasattr(res, "runtime_proxy_info")
            else getattr(res, "runtime_proxy_token", "")
        )
        url = res.runtime_proxy_info.url if hasattr(res, "runtime_proxy_info") else ""
        endpoint = res.endpoint

    s = SessionState(
        name=name,
        token=token,
        url=url,
        endpoint=endpoint,
        variant=variant.value,
        accelerator=accelerator.value,
    )
    s.keep_alive_pid = spawn_keep_alive(endpoint, name)
    state.store.add(s)
    state.history.log_event(
        name,
        "session_created",
        {
            "endpoint": endpoint,
            "variant": variant.value,
            "accelerator": accelerator.value,
        },
    )
    typer.echo("[colab] Session READY.")


def sessions_command():
    """List all active sessions"""
    from colab_cli.common import state

    sessions, assignments = state.sync_sessions()
    if not assignments:
        typer.echo("[colab] No active sessions found on server.")
    else:
        for i, a in enumerate(assignments):
            hw = "CPU" if a.accelerator.value == "NONE" else a.accelerator.value
            typer.echo(
                f"[{i}] Endpoint: {a.endpoint} | Variant: {a.variant.value} | Hardware: {hw}"
            )

    if sessions:
        typer.echo("\n[colab] Local session names:")
        for s in sessions.values():
            typer.echo(f"  {s.name} -> {s.endpoint}")


def status(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
):
    """Show session status"""
    from colab_cli.common import state

    local_sessions, _ = state.sync_sessions()
    if session:
        s = state.store.get(session)
        if s:
            hw = "CPU" if s.accelerator == "NONE" else s.accelerator
            typer.echo(f"[colab] Session: {s.name} | Status: RUNNING | Hardware: {hw}")
            if s.last_execution:
                exec_file, exec_cell, exec_time = s.last_execution
                cell_str = f" | Cell: {exec_cell}" if exec_cell else ""
                typer.echo(f"  Last Execution: {exec_file}{cell_str} at {exec_time}")
        else:
            typer.echo(f"[colab] Session '{session}' not found.")
    else:
        if not local_sessions:
            typer.echo("[colab] No active sessions.")
        for s in local_sessions.values():
            hw = "CPU" if s.accelerator == "NONE" else s.accelerator
            typer.echo(f"[{s.name}] Hardware: {hw} | Variant: {s.variant}")
            if s.last_execution:
                exec_file, exec_cell, exec_time = s.last_execution
                cell_str = f" | Cell: {exec_cell}" if exec_cell else ""
                typer.echo(f"  Last Execution: {exec_file}{cell_str} at {exec_time}")


def stop(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
):
    """Stop a session"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.")
        return

    typer.echo(f"[colab] Stopping session '{name}'...")
    if s.keep_alive_pid:
        from colab_cli.common import kill_process

        kill_process(s.keep_alive_pid)

    try:
        runtime = ColabRuntime(s.url, s.token, kernel_id=s.kernel_id)
        runtime.stop(shutdown_kernel=True)
    except Exception:
        pass

    state.client.unassign(s.endpoint)
    state.store.remove(name)
    state.history.log_event(name, "session_terminated", {"reason": "user_requested"})
    typer.echo("[colab] Session terminated.")


def spawn_keep_alive(endpoint: str, session_name: str):
    """Spawns a detached keep-alive process."""
    # We call 'colab keep-alive' using the current interpreter
    cmd = [sys.executable, "-m", "colab_cli.cli", "keep-alive", endpoint, session_name]
    # Detach process
    kwargs = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    else:
        # https://stackoverflow.com/questions/1356540/how-can-i-make-a-python-script-run-in-the-background-as-a-service-on-windows
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    p = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        **kwargs,
    )
    return p.pid


def keep_alive(
    endpoint: Annotated[str, typer.Argument(help="Endpoint ID")],
    session_name: Annotated[str, typer.Argument(help="Session name")],
):
    """Hidden command to run keep-alive loop. Terminate after 24h."""
    from colab_cli.common import state

    state.history.log_event(session_name, "keep_alive_started", {"endpoint": endpoint})

    start_time = time.time()
    # 24 hours limit
    max_duration = 24 * 3600
    consecutive_4xx = 0

    reason = "time_limit_reached"
    while time.time() - start_time < max_duration:
        # Check if session still exists in local state
        s = state.store.get(session_name)
        if not s:
            reason = "session_not_found"
            break
        if s.endpoint != endpoint:
            reason = "endpoint_mismatch"
            break

        try:
            state.client.keep_alive_assignment(endpoint)
            consecutive_4xx = 0
        except Exception as e:
            code = get_status_code(e)
            if code is not None and 400 <= code < 500:
                consecutive_4xx += 1
                if consecutive_4xx >= 2:
                    reason = "consecutive_4xx_errors"
                    break
            else:
                # For other errors (network), we retry and don't count as 4xx
                pass

        time.sleep(60)

    state.history.log_event(session_name, "keep_alive_stopped", {"reason": reason})


def register(app: typer.Typer):
    app.command()(new)
    app.command(name="sessions")(sessions_command)
    app.command()(status)
    app.command()(stop)
    app.command(hidden=True)(keep_alive)
