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

"""Kernel-side recording, installed without wrapping or rewriting user code.

This module's source is sent to the Colab kernel in an isolated namespace.
"""

import json
import os
from pathlib import Path
import threading
import time


def install(session, request_id, path):
    from jupyter_client.jsonutil import json_default
    from jupyter_client.session import Message

    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Fail before submitting user code if a recovery record cannot be created.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    state = getattr(session, "_colab_cli_journal", None)
    if state is None:
        state = {"requests": {}, "original": session.send, "lock": threading.RLock()}

        def send(
            stream,
            msg_or_type,
            content=None,
            parent=None,
            ident=None,
            buffers=None,
            track=False,
            header=None,
            metadata=None,
        ):
            msg = (
                msg_or_type
                if isinstance(msg_or_type, (dict, Message))
                else session.msg(
                    msg_or_type,
                    content=content,
                    parent=parent,
                    header=header,
                    metadata=metadata,
                )
            )
            with state["lock"]:
                record = state["requests"].get(
                    msg.get("parent_header", {}).get("msg_id")
                )
                kind = msg["header"]["msg_type"]
                if record is not None and kind in {
                    "stream",
                    "display_data",
                    "update_display_data",
                    "clear_output",
                    "execute_result",
                    "error",
                    "status",
                    "execute_reply",
                }:
                    record["sequence"] += 1
                    try:
                        # Record before delivery: a client can disconnect as soon
                        # as it sees a message. Never retain binary comm payloads.
                        payload = {
                            key: msg.get(key, {})
                            for key in (
                                "header",
                                "parent_header",
                                "metadata",
                                "content",
                            )
                        }
                        payload["_colab_cli_sequence"] = record["sequence"]
                        with record["path"].open("a", encoding="utf-8") as output:
                            output.write(
                                json.dumps(payload, default=json_default) + "\n"
                            )
                    except (OSError, TypeError, ValueError):
                        # A disk failure must not change execution or hide live
                        # output. Recovery will refuse an incomplete journal.
                        record["failed"] = True
                    if kind == "execute_reply":
                        record["reply"] = True
                    if (
                        kind == "status"
                        and msg["content"].get("execution_state") == "idle"
                    ):
                        record["idle"] = True
                    if record["reply"] and record["idle"]:
                        del state["requests"][msg["parent_header"]["msg_id"]]
                        if not state["requests"] and session.send is send:
                            session.send = state["original"]
                            del session._colab_cli_journal
            return state["original"](
                stream,
                msg,
                ident=ident,
                buffers=buffers,
                track=track,
            )

        session._colab_cli_journal = state
        session.send = send

    with state["lock"]:
        state["requests"][request_id] = {
            "path": path,
            "reply": False,
            "idle": False,
            "failed": False,
            "sequence": 0,
        }
        # Incomplete executions are retained for diagnosis. Reclaim old files
        # only when they are not still being recorded by this kernel.
        active = {item["path"] for item in state["requests"].values()}
        for old in path.parent.glob("*.jsonl"):
            try:
                if old not in active and time.time() - old.stat().st_mtime > 86400:
                    old.unlink()
            except OSError:
                pass
