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
import nbformat
import os
import re
import sys
import typer
import uuid
from nbformat.v4 import new_output
from rich.console import Console
from typing import List, Optional
from typing_extensions import Annotated

from colab_cli.runtime import ColabRuntime
from colab_cli.utils import handle_image, is_terminal_error, render_display_data
from colab_cli.console import connect_console

_console = Console()

TITLE_REGEX = re.compile(r"^\s*#\s*@title\s+(.*)", re.MULTILINE)
ENV_KEY_REGEX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_stdin_tty():
    return sys.stdin.isatty()


def _parse_env_vars(env: Optional[List[str]]) -> dict[str, str]:
    """Parse repeatable --env KEY=VALUE entries into an ordered mapping."""
    env_vars = {}
    for item in env or []:
        if "=" not in item:
            typer.echo(
                f"[colab] Invalid --env value {item!r}. Expected KEY=VALUE.",
                err=True,
            )
            raise typer.Exit(2)

        key, value = item.split("=", 1)
        if not ENV_KEY_REGEX.fullmatch(key):
            typer.echo(
                f"[colab] Invalid --env key {key!r}. Expected a valid "
                "environment variable name.",
                err=True,
            )
            raise typer.Exit(2)

        env_vars[key] = value
    return env_vars


def _build_env_prelude(env_vars: dict[str, str]) -> str:
    """Build Python source that sets environment variables in the remote kernel."""
    if not env_vars:
        return ""

    lines = ["import os"]
    lines.extend(f"os.environ[{key!r}] = {value!r}" for key, value in env_vars.items())
    return "\n".join(lines) + "\n"


def save_output(outputs, cell):
    if cell is None:
        return

    if not hasattr(cell, "outputs"):
        cell.outputs = []
    else:
        cell.outputs.clear()

    for out in outputs:
        if out.get("output_type") == "stream":
            cell.outputs.append(
                new_output(
                    output_type="stream",
                    name=out.get("name", "stdout"),
                    text=out.get("text", ""),
                )
            )
        elif "data" in out:
            output_type = out.get("output_type", "display_data")
            cell.outputs.append(
                new_output(
                    output_type=output_type,
                    data=out["data"],
                    metadata=out.get("metadata", {}),
                )
            )
        elif out.get("output_type") == "error":
            cell.outputs.append(
                new_output(
                    output_type="error",
                    ename=out.get("ename", "Error"),
                    evalue=out.get("evalue", ""),
                    traceback=out.get("traceback", []),
                )
            )


def display_output(out, output_image=None):
    if out.get("output_type") == "stream":
        stream = sys.stderr if out.get("name") == "stderr" else sys.stdout
        stream.write(out.get("text", ""))
        stream.flush()
    elif "data" in out:
        data = out["data"]
        text = render_display_data(data)
        if text is not None:
            _console.print(text)
        if png := data.get("image/png"):
            handle_image(png, "image/png", target_path=output_image)
        elif jpeg := data.get("image/jpeg"):
            handle_image(jpeg, "image/jpeg", target_path=output_image)
    elif out.get("output_type") == "error":
        tb = out.get("traceback", [])
        if tb:
            sys.stderr.write("".join(tb) + "\n")
        else:
            ename = out.get("ename", "Error")
            evalue = out.get("evalue", "")
            sys.stderr.write(f"{ename}: {evalue}\n")
    else:
        # Ignore silent outputs like metadata or clear_output for streaming
        pass


def exec_command(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    file: Annotated[
        Optional[str], typer.Option("-f", "--file", help="File to execute")
    ] = None,
    output_image: Annotated[
        Optional[str], typer.Option("--output-image", help="Path to save plot")
    ] = None,
    timeout: Annotated[
        Optional[float],
        typer.Option("--timeout", help="Timeout in seconds for code execution"),
    ] = 30.0,
    env: Annotated[
        Optional[List[str]],
        typer.Option(
            "--env",
            help=(
                "Set an environment variable in the remote kernel as KEY=VALUE. "
                "Repeat for multiple variables."
            ),
        ),
    ] = None,
):
    """Execute code in a session"""
    from colab_cli.common import state

    env_vars = _parse_env_vars(env)
    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)

    code_blocks = []
    if file:
        if file.endswith(".ipynb"):
            typer.echo(f"[colab] Parsing notebook '{file}'...")
            with open(file, "r", encoding="utf-8") as f:
                nb = nbformat.read(f, as_version=4)
                for cell in nb.cells:
                    # nbformat v4.5+ requires 'id' at the top level
                    if not hasattr(cell, "id") or not cell.id:
                        cell.id = str(uuid.uuid4())

                    if cell.cell_type == "code":
                        code_blocks.append(
                            {"code": cell.source, "id": cell.id, "cell": cell}
                        )
        else:
            with open(file, "r") as f:
                code_blocks.append({"code": f.read(), "id": None})
    else:
        if is_stdin_tty():
            typer.echo("[colab] Error: No input provided. Pipe code or provide a file.")
            raise typer.Exit(1)
        code_blocks.append({"code": sys.stdin.read(), "id": None})

    if not any(b["code"].strip() for b in code_blocks):
        raise typer.Exit(0)

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
        # Ensure we are in /content which is the standard Colab working directory
        runtime.execute_code(
            "import os; os.makedirs('/content', exist_ok=True); os.chdir('/content')"
        )
    except Exception as e:
        if is_terminal_error(e):
            typer.echo(
                f"[colab] Session '{name}' appears to be lost (404/401). Cleaning up."
            )
            state.prune_session(name)
            raise typer.Exit(1)
        raise e

    try:
        is_nb = file and file.endswith(".ipynb")
        s.running = f"exec({file or 'stdin'})"
        state.store.add(s)

        for i, block in enumerate(code_blocks):
            code = _build_env_prelude(env_vars) + block["code"]
            identifier = None
            if is_nb:
                title_match = TITLE_REGEX.search(code)
                if title_match:
                    identifier = title_match.group(1).strip()
                elif block.get("id"):
                    identifier = block["id"]
                else:
                    identifier = ""

                identifier_str = f" - {identifier}" if identifier else ""
                typer.echo(
                    f"[colab] Executing cell {i + 1}/{len(code_blocks)}{identifier_str}..."
                )

            s.last_execution = (
                file or "stdin",
                identifier,
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            state.store.add(s)

            outputs = runtime.execute_code(
                code,
                output_hook=lambda o: display_output(o, output_image),
                timeout=timeout,
            )
            if "cell" in block:
                save_output(outputs, block["cell"])
            state.history.log_event(
                name,
                "execution",
                {
                    "code": code,
                    "outputs": outputs,
                    "cell_index": i if len(code_blocks) > 1 else None,
                    "cell_id": block.get("id"),
                },
            )
    finally:
        s.running = None
        state.store.add(s)
        runtime.stop()
        if file and file.endswith(".ipynb"):
            output_file = os.path.splitext(file)[0] + "_output.ipynb"
            typer.echo(f"[colab] Saving notebook with outputs to '{output_file}'...")
            with open(output_file, "w", encoding="utf-8") as f:
                nbformat.write(nb, f)


def repl(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    output_image: Annotated[
        Optional[str], typer.Option("--output-image", help="Path to save plot")
    ] = None,
):
    """Start an interactive REPL"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)

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
        # Ensure we are in /content which is the standard Colab working directory
        runtime.execute_code(
            "import os; os.makedirs('/content', exist_ok=True); os.chdir('/content')"
        )
    except Exception as e:
        if is_terminal_error(e):
            typer.echo(
                f"[colab] Session '{name}' appears to be lost (404/401). Cleaning up."
            )
            state.prune_session(name)
            raise typer.Exit(1)
        raise e

    if not is_stdin_tty():
        code = sys.stdin.read()
        if not code.strip():
            raise typer.Exit(0)

        s.last_execution = (
            "stdin",
            None,
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        s.running = "repl(stdin)"
        state.store.add(s)
        try:
            outputs = runtime.execute_code(
                code, output_hook=lambda o: display_output(o, output_image)
            )
            state.history.log_event(
                name, "execution", {"code": code, "outputs": outputs, "source": "piped"}
            )
        finally:
            s.running = None
            state.store.add(s)
            runtime.stop()
    else:
        from colab_cli.repl import ColabREPL

        s.running = "repl"
        state.store.add(s)
        try:
            repl_inst = ColabREPL(
                runtime,
                session_name=s.name,
                history_logger=state.history,
                output_image=output_image,
            )
            state.history.log_event(name, "repl_started", {})
            repl_inst.run()
        finally:
            s.running = None
            state.store.add(s)


def console(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
):
    """Connect to raw TTY console"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)
    state.history.log_event(s.name, "console_started", {})
    s.running = "console"
    state.store.add(s)
    try:
        connect_console(s)
    except Exception as e:
        if is_terminal_error(e):
            typer.echo(
                f"[colab] Session '{name}' appears to be lost (404/401). Cleaning up."
            )
            state.prune_session(name)
            raise typer.Exit(1)
        raise e
    finally:
        s.running = None
        state.store.add(s)


def register(app: typer.Typer):
    app.command(name="exec")(exec_command)
    app.command()(repl)
    app.command()(console)
