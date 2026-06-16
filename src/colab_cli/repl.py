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
from typing import Any, List, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.styles import Style
from pygments.lexers.python import PythonLexer
from rich.console import Console
from rich.text import Text

from colab_cli.runtime import ColabRuntime
from colab_cli.utils import handle_image, render_display_data



class ColabREPL:
    def __init__(
        self,
        runtime: ColabRuntime,
        session_name: Optional[str] = None,
        history_logger: Optional[Any] = None,
        output_image: Optional[str] = None,
    ):
        self.runtime = runtime
        self.session_name = session_name
        self.history_logger = history_logger
        self.output_image = output_image
        self.kb = KeyBindings()
        self.console = Console()
        self.repl_history: List[dict] = []

        @self.kb.add("enter")
        def _(event):
            event.current_buffer.validate_and_handle()

        @self.kb.add("escape", "enter")
        @self.kb.add("c-j")
        def _(event):
            event.current_buffer.insert_text("\n")

        self.session = PromptSession(
            history=InMemoryHistory(),
            lexer=PygmentsLexer(PythonLexer),
            include_default_pygments_style=False,
            key_bindings=self.kb,
            multiline=True,
        )
        self.style = Style.from_dict(
            {
                "prompt": "bold blue",
                "continuation": "#888888",
            }
        )

    def print_info(self, message: str):
        self.console.print(f"[bold blue][*][/bold blue] {message}")

    def print_error(self, message: str):
        self.console.print(f"[bold red][!][/bold red] {message}")

    def display_output(self, output: dict):
        if "text" in output:
            self.console.print(Text.from_ansi(output["text"]), end="")
        elif "data" in output:
            data = output["data"]

            # Check for images first
            image_displayed = False
            for mime_type in ["image/png", "image/jpeg"]:
                if mime_type in data:
                    handle_image(
                        data[mime_type], mime_type, target_path=self.output_image
                    )
                    image_displayed = True
                    break

            text = render_display_data(data)
            if text is not None:
                # Skip generic IPython object reprs if we already showed an image
                if isinstance(text, Text) and image_displayed:
                    if any(
                        x in text.plain
                        for x in ["<IPython.core.display.Image", "<Figure size"]
                    ):
                        return
                self.console.print(text)
        elif output.get("output_type") == "error":
            ename = output.get("ename", "Error")
            evalue = output.get("evalue", "")
            traceback = output.get("traceback", [])
            if traceback:
                self.console.print(Text.from_ansi("".join(traceback)))
            else:
                self.print_error(f"{ename}: {evalue}")

    def execute(self, code: str):
        if self.session_name:
            from colab_cli.common import state

            s = state.store.get(self.session_name)
            if s:
                s.last_execution = (
                    "REPL",
                    None,
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
                state.store.add(s)

        try:
            outputs = self.runtime.execute_code(
                code, output_hook=lambda o: self.display_output(o)
            )
            # Ensure next prompt starts on a newline after streaming
            print()

            self.repl_history.append({"input": code, "outputs": outputs or []})
            if self.history_logger and self.session_name:
                self.history_logger.log_event(
                    self.session_name,
                    "execution",
                    {"code": code, "outputs": outputs or []},
                )
        except Exception as e:
            self.print_error(f"Execution failed: {e}")

    def run(self):
        self.console.print("Python 3 (Google Colab Runtime)\nType /quit to exit.")

        while True:
            try:
                result = self.session.prompt(
                    ">>> ",
                    style=self.style,
                )

                if result is None:
                    continue

                code = result.strip()

                if not code:
                    continue

                if code.lower() in ("/quit", "quit()", "exit()"):
                    break

                self.execute(code)

            except EOFError:
                break
            except KeyboardInterrupt:
                print()
                continue
            except Exception as e:
                self.print_error(f"REPL Error: {e}")

        self.print_info("Goodbye!")
        self.runtime.stop()
