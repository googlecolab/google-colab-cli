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

import uuid
from typing import Optional
import typer
from typing_extensions import Annotated

from colab_cli.client import (
    Accelerator,
    ColabRequestError,
    HIGH_MEM_ONLY_ACCELERATORS,
    PostAssignmentResponse,
    Shape,
    TooManyAssignmentsError,
    Variant,
    resolve_assign_shape,
    shape_display_label,
)
from colab_cli.utils import get_status_code
from colab_cli.state import SessionState
from colab_cli.runtime import ColabRuntime


def _hardware_label(accelerator: str) -> str:
    """`NONE` -> `CPU`; everything else passes through."""
    return "CPU" if accelerator == "NONE" else accelerator


def _format_session_line(
    name: str,
    endpoint: str,
    accelerator: str,
    variant: str,
    status: Optional[str] = None,
    machine_shape: Optional[str] = None,
) -> str:
    """Single source of truth for session display lines.

    Format: ``[name] endpoint | Hardware: X | Shape: Y | Variant: Z[ | Status: W]``.
    Use ``"?"`` as the name for orphaned server-side assignments with no local
    state.
    """
    parts = [
        f"[{name}] {endpoint}",
        f"Hardware: {_hardware_label(accelerator)}",
        f"Shape: {shape_display_label(machine_shape)}",
        f"Variant: {variant}",
    ]
    if status is not None:
        parts.append(f"Status: {status}")
    return " | ".join(parts)


def resolve_runtime_options(
    gpu: Optional[str] = None,
    tpu: Optional[str] = None,
    *,
    high_mem: bool = False,
) -> tuple[Variant, Accelerator, Optional[Shape]]:
    """Map CLI flags to backend variant, accelerator, and optional shape."""
    if tpu:
        variant = Variant.TPU
        accelerator = Accelerator.V5E1 if tpu.lower() == "v5e1" else Accelerator.V6E1
    elif gpu:
        variant = Variant.GPU
        mapping = {
            "a100": Accelerator.A100,
            "h100": Accelerator.H100,
            "l4": Accelerator.L4,
            "t4": Accelerator.T4,
            "g4": Accelerator.G4,
        }
        accelerator = mapping.get(gpu.lower(), Accelerator.A100)
    else:
        variant = Variant.DEFAULT
        accelerator = Accelerator.NONE

    shape = resolve_assign_shape(accelerator, high_mem=high_mem)
    return variant, accelerator, shape


def new(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    tpu: Annotated[
        Optional[str],
        typer.Option(
            help="TPU accelerator variant. Supported: v5e1, v6e1.",
        ),
    ] = None,
    gpu: Annotated[
        Optional[str],
        typer.Option(
            help=(
                "GPU accelerator variant. Supported: T4, L4, G4, H100, A100."
                "\n\nIf omitted (along with --tpu), a CPU runtime is created."
                "\n\nAvailability varies by Colab subscription tier."
            ),
        ),
    ] = None,
    high_mem: Annotated[
        bool,
        typer.Option(
            "--high-mem",
            help=(
                "Request a high-RAM machine shape (CPU, T4, A100, etc.). "
                "Requires Colab Pro or Pro+ entitlement. Ignored for "
                "accelerators that only offer a single shape (L4, v5e1, v6e1)."
            ),
        ),
    ] = False,
):
    """Create a new session"""
    from colab_cli.common import state

    name = session or uuid.uuid4().hex[:6]
    variant, accelerator, shape = resolve_runtime_options(gpu, tpu, high_mem=high_mem)

    if high_mem and accelerator in HIGH_MEM_ONLY_ACCELERATORS:
        typer.echo(
            "[colab] --high-mem ignored: this accelerator only offers one "
            "machine shape.",
            err=True,
        )

    typer.echo(f"[colab] Creating session '{name}'...")
    try:
        res = state.client.assign(
            uuid.uuid4(), variant=variant, accelerator=accelerator, shape=shape
        )
    except TooManyAssignmentsError:
        # The Colab backend returns 412 when it refuses the assignment. This
        # usually means too many active sessions, or a temporary usage or
        # capacity limit for the requested runtime. Translate that to a
        # friendly, actionable message instead of a raw traceback.
        typer.echo(
            "[colab] Allocation refused (precondition failed). This can mean "
            "too many active sessions, or a temporary usage or capacity "
            "limit for the requested runtime. Run `colab stop` to free up a "
            "session, wait and retry, or try a different accelerator.",
            err=True,
        )
        raise typer.Exit(code=1)
    except ColabRequestError as e:
        # The Colab backend returns 400 when the caller is not entitled to the
        # requested accelerator (e.g. no A100 quota). Translate that to a
        # friendly, actionable message instead of a raw traceback. We only
        # interpret it this way when an accelerator was actually requested;
        # otherwise we re-raise so the user sees the real cause.
        if get_status_code(e) == 400 and accelerator != Accelerator.NONE:
            typer.echo(
                f"[colab] Backend rejected accelerator '{accelerator.value}'. "
                "You may not have quota or entitlement for this accelerator on "
                "your account. Try a different one (e.g. --gpu T4) or omit "
                "--gpu/--tpu for a CPU runtime.",
                err=True,
            )
            raise typer.Exit(code=1)
        raise

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

    # Importing locally to avoid a top-level circular import via auth.

    s = SessionState(
        name=name,
        token=token,
        url=url,
        endpoint=endpoint,
        variant=variant.value,
        accelerator=accelerator.value,
        machine_shape=(
            Shape.HIGH_RAM.name if shape == Shape.HIGH_RAM else Shape.STANDARD.name
        ),
    )

    state.store.add(s)
    state.history.log_event(
        name,
        "session_created",
        {
            "endpoint": endpoint,
            "variant": variant.value,
            "accelerator": accelerator.value,
            "machine_shape": s.machine_shape,
        },
    )
    typer.echo("[colab] Session READY.")


def restart_kernel(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
):
    """Restart a session's kernel"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)

    def on_started(kid):
        s.kernel_id = kid
        state.store.add(s)

    def on_sess_started(sid):
        s.session_id = sid
        state.store.add(s)

    runtime = ColabRuntime(
        s.url,
        s.token,
        kernel_id=s.kernel_id,
        session_id=s.session_id,
        on_kernel_started=on_started,
        on_session_started=on_sess_started,
    )

    try:
        runtime.restart()
    finally:
        runtime.stop()


def sessions_command():
    """List all active sessions"""
    from colab_cli.common import state

    sessions, assignments = state.sync_sessions()
    if not assignments:
        typer.echo("[colab] No active sessions found on server.")
        return

    # Build endpoint -> local-name lookup so we can lead with the friendly name.
    name_by_endpoint = {s.endpoint: s.name for s in sessions.values()}
    for a in assignments:
        name = name_by_endpoint.get(a.endpoint, "?")
        # `a.variant` is an int-valued AssignmentVariant (DEFAULT=0/GPU=1/TPU=2);
        # its `.name` matches the user-facing string Variant enum, which is what
        # `status` shows for locally-tracked sessions.
        typer.echo(
            _format_session_line(
                name=name,
                endpoint=a.endpoint,
                accelerator=a.accelerator.value,
                variant=a.variant.name,
                machine_shape=a.machine_shape.name,
            )
        )


def _print_status_for(s: SessionState) -> None:
    """Print one session's status line plus optional last-execution detail."""
    status = f"BUSY ({s.running})" if s.running else "IDLE"
    typer.echo(
        _format_session_line(
            name=s.name,
            endpoint=s.endpoint,
            accelerator=s.accelerator,
            variant=s.variant,
            status=status,
            machine_shape=s.machine_shape,
        )
    )
    if s.last_execution:
        exec_file, exec_cell, exec_time = s.last_execution
        cell_str = f" | Cell: {exec_cell}" if exec_cell else ""
        typer.echo(f"  Last Execution: {exec_file}{cell_str} at {exec_time}")


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
            _print_status_for(s)
        else:
            typer.echo(f"[colab] Session '{session}' not found.")
        return

    if not local_sessions:
        typer.echo("[colab] No active sessions.")
        return
    for s in local_sessions.values():
        _print_status_for(s)


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
    try:
        runtime = ColabRuntime(s.url, s.token, kernel_id=s.kernel_id)
        runtime.stop(shutdown_kernel=True)
    except Exception:
        pass

    state.client.unassign(s.endpoint)
    state.store.remove(name)
    state.history.log_event(name, "session_terminated", {"reason": "user_requested"})
    typer.echo("[colab] Session terminated.")


def register(app: typer.Typer):
    app.command()(new)
    app.command(name="sessions")(sessions_command)
    app.command(name="restart-kernel")(restart_kernel)
    app.command()(status)
    app.command()(stop)

