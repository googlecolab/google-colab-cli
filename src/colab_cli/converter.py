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

import nbformat
from typing import Any, Dict, List
import json
import os
import uuid


def export_history(events: List[Dict[str, Any]], session_name: str, output_path: str):
    """
    Exports history based on file extension.
    """
    ext = os.path.splitext(output_path)[1].lower()

    if ext == ".ipynb":
        nb = convert_history_to_ipynb(events, session_name)
        with open(output_path, "w", encoding="utf-8") as f:
            nbformat.write(nb, f)

    elif ext == ".jsonl":
        with open(output_path, "w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

    elif ext == ".md":
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"# Colab Session: {session_name}\n\n")
            for event in events:
                ts = event.get("timestamp", "").split(".")[0].replace("T", " ")
                etype = event.get("event_type")
                if etype == "execution":
                    code = event.get("code", "")
                    f.write(f"### Execution ({ts})\n```python\n{code}\n```\n\n")
                    for o in event.get("outputs", []):
                        if "text" in o:
                            f.write(f"**Output**:\n```\n{o['text']}```\n\n")
                elif etype == "session_created":
                    f.write(
                        f"## Session Created: {ts}\n- Endpoint: `{event.get('endpoint')}`\n\n"
                    )
                elif etype == "file_operation":
                    f.write(
                        f"*File Operation*: `{event.get('op')}` on `{event.get('path', event.get('remote', ''))}`\n\n"
                    )

    elif ext == ".txt":
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"Colab Session: {session_name}\n" + "=" * 20 + "\n\n")
            for event in events:
                ts = event.get("timestamp", "").split(".")[0].replace("T", " ")
                etype = event.get("event_type", "unknown")
                f.write(f"[{ts}] {etype.upper()}: ")
                if etype == "execution":
                    f.write(event.get("code", "").strip() + "\n")
                else:
                    f.write(str(event) + "\n")

    else:
        print(f"[colab] Unsupported export format: {ext}")
        return

    print(f"[colab] Exported history to '{output_path}'.")


def convert_history_to_ipynb(
    events: List[Dict[str, Any]], session_name: str
) -> nbformat.NotebookNode:
    """
    Converts a list of session events to a Jupyter Notebook (v4).
    """
    nb = nbformat.v4.new_notebook()
    nb.metadata.kernelspec = {
        "display_name": "Python 3 (Google Colab)",
        "language": "python",
        "name": "python3",
    }

    title = f"# Colab Session: {session_name}\nGenerated from colab-cli history log."
    cell = nbformat.v4.new_markdown_cell(title)
    cell.id = str(uuid.uuid4())
    nb.cells.append(cell)

    for event in events:
        etype = event.get("event_type")
        ts = event.get("timestamp", "").split(".")[0].replace("T", " ")

        if etype == "session_created":
            meta = f"**Session Created**: {ts}\n- Endpoint: `{event.get('endpoint')}`\n- Hardware: `{event.get('accelerator')}`"
            cell = nbformat.v4.new_markdown_cell(meta)
            cell.id = str(uuid.uuid4())
            nb.cells.append(cell)

        elif etype == "execution":
            code = event.get("code", "")
            # Check for shell commands (starting with ! or from piped console)
            # If it's a raw shell command from a console pipe, wrap it in %%bash if it doesn't have !
            if event.get("source") == "piped" and not code.startswith("!"):
                code = "%%bash\n" + code

            outputs = _map_outputs(event.get("outputs", []))
            cell = nbformat.v4.new_code_cell(code, outputs=outputs)
            cell.id = str(uuid.uuid4())
            nb.cells.append(cell)

        elif etype == "automation":
            op = event.get("op")
            code = event.get("code", "")
            cell = nbformat.v4.new_markdown_cell(f"### Automation: {op} ({ts})")
            cell.id = str(uuid.uuid4())
            nb.cells.append(cell)
            if code:
                # Get the result from the next event if it's automation_result
                cell = nbformat.v4.new_code_cell(code)
                cell.id = str(uuid.uuid4())
                nb.cells.append(cell)

        elif etype == "automation_result":
            # We can attach these outputs to the previous automation cell if we were more clever,
            # but for now let's just ensure we capture the output.
            if event.get("outputs"):
                cell = nbformat.v4.new_code_cell(
                    "# Result of previous automation",
                    outputs=_map_outputs(event.get("outputs")),
                )
                cell.id = str(uuid.uuid4())
                nb.cells.append(cell)

        elif etype == "file_operation":
            cell = nbformat.v4.new_markdown_cell(
                f"*File Operation*: `{event.get('op')}` on `{event.get('path', event.get('remote', ''))}`"
            )
            cell.id = str(uuid.uuid4())
            nb.cells.append(cell)

        elif etype == "stdin_request":
            cell = nbformat.v4.new_markdown_cell(
                f"> **Input Requested**: {event.get('prompt')}"
            )
            cell.id = str(uuid.uuid4())
            nb.cells.append(cell)

        elif etype == "input_reply":
            cell = nbformat.v4.new_markdown_cell(
                f"> **User Input**: `{event.get('value')}`"
            )
            cell.id = str(uuid.uuid4())
            nb.cells.append(cell)

    return nb


def _map_outputs(outputs: List[Dict[str, Any]]) -> List[nbformat.NotebookNode]:
    nb_outputs = []
    for o in outputs:
        otype = o.get("output_type")
        if "text" in o:
            nb_outputs.append(
                nbformat.v4.new_output("stream", name="stdout", text=o["text"])
            )
        elif "data" in o:
            nb_outputs.append(nbformat.v4.new_output("display_data", data=o["data"]))
        elif otype == "error":
            nb_outputs.append(
                nbformat.v4.new_output(
                    "error",
                    ename=o.get("ename", "Error"),
                    evalue=o.get("evalue", ""),
                    traceback=o.get("traceback", []),
                )
            )
    return nb_outputs
