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

import logging
import time
from typing import Any, Callable, Dict, List, Optional

import jupyter_kernel_client
import requests


class ColabRuntime:
    def __init__(
        self,
        url: str,
        token: str,
        session_name: Optional[str] = None,
        history: Optional[Any] = None,
        kernel_id: Optional[str] = None,
        session_id: Optional[str] = None,
        on_kernel_started: Optional[Callable[[str], None]] = None,
        on_session_started: Optional[Callable[[str], None]] = None,
    ):
        self.url = url
        self.token = token
        self.session_name = session_name
        self.history = history
        self.kernel_id = kernel_id
        self.session_id = session_id
        self.on_kernel_started = on_kernel_started
        self.on_session_started = on_session_started
        self._kernel_client = None
        self.colab_request_hook: Optional[Callable[[Dict[str, Any], Any], None]] = None

    def _apply_ws_hook(self):
        wsclient = self._kernel_client._manager.client
        original_on_message = wsclient.kernel_socket.on_message

        def hooked_on_message(s_ws, message):
            if not self.colab_request_hook:
                return original_on_message(s_ws, message)

            try:
                from jupyter_kernel_client.wsclient import JupyterSubprotocol

                if wsclient._subprotocol == JupyterSubprotocol.DEFAULT:
                    from jupyter_kernel_client.wsclient import (
                        deserialize_msg_from_ws_default,
                    )

                    deserialize_msg = deserialize_msg_from_ws_default(message)
                elif wsclient._subprotocol == JupyterSubprotocol.V1:
                    from jupyter_kernel_client.wsclient import (
                        deserialize_msg_from_ws_v1,
                    )

                    channel, msg_list = deserialize_msg_from_ws_v1(message)
                    deserialize_msg = wsclient.session.deserialize(msg_list)
                else:
                    deserialize_msg = None

                if deserialize_msg:
                    msg_type = deserialize_msg.get("msg_type")
                    if msg_type == "colab_request":
                        # We pass the deserialized msg and the wsclient to the hook
                        if self.colab_request_hook(deserialize_msg, wsclient):
                            # If the hook returns True, we intercept and do NOT pass to original
                            return

            except Exception as e:
                logging.debug(f"Error in colab_request hook: {e}")

            # Call original for all other messages
            original_on_message(s_ws, message)

        wsclient.kernel_socket.on_message = hooked_on_message

    @property
    def kernel_client(self):
        if not self._kernel_client:
            retries = 3
            backoff = 2
            last_err = None

            for i in range(retries):
                try:
                    client_kwargs = {
                        "subprotocol": jupyter_kernel_client.JupyterSubprotocol.DEFAULT,
                        "extra_params": {"colab-runtime-proxy-token": self.token},
                    }
                    if self.session_id:
                        # WSSession (Session) expects 'session' for the ID
                        client_kwargs["session"] = self.session_id

                    self._kernel_client = jupyter_kernel_client.KernelClient(
                        server_url=self.url,
                        token=self.token,
                        kernel_id=self.kernel_id,
                        client_kwargs=client_kwargs,
                        headers={
                            "X-Colab-Client-Agent": "colab-cli",
                            "X-Colab-Runtime-Proxy-Token": self.token,
                        },
                    )
                    # Force _own_kernel to False. This prevents jupyter-kernel-client
                    # from automatically deleting the kernel when the client is closed or deleted.
                    self._kernel_client._own_kernel = False

                    self._kernel_client.start()
                    self._apply_ws_hook()

                    # Capture IDs if we started fresh
                    if not self.kernel_id and self._kernel_client.id:
                        self.kernel_id = self._kernel_client.id
                        if self.on_kernel_started:
                            self.on_kernel_started(self.kernel_id)

                    if (
                        not self.session_id
                        and self._kernel_client._manager.client.session.session
                    ):
                        self.session_id = (
                            self._kernel_client._manager.client.session.session
                        )
                        if self.on_session_started:
                            self.on_session_started(self.session_id)
                    break
                except (
                    requests.exceptions.ReadTimeout,
                    requests.exceptions.ConnectTimeout,
                ) as e:
                    last_err = e
                    if i < retries - 1:
                        sleep_time = backoff ** (i + 1)
                        logging.debug(
                            f"Kernel startup timeout, retrying in {sleep_time}s..."
                            f" ({i + 1}/{retries})"
                        )
                        time.sleep(sleep_time)
                    else:
                        raise last_err
                except Exception as e:
                    raise e

        return self._kernel_client

    def restart(
        self,
        timeout: Optional[float] = None,
    ):
        self.kernel_client.restart(timeout=timeout)

    def execute_code(
        self,
        code: str,
        allow_stdin: bool = False,
        stdin_hook: Any = None,
        output_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        # ``jupyter_kernel_client`` defaults ``timeout`` to ``REQUEST_TIMEOUT``
        # (10 seconds) on both ``execute`` and ``execute_interactive``. That
        # value is a wall-clock budget that shrinks every time the poll loop
        # iterates -- as long as iopub/stdin events arrive back-to-back the
        # call survives, but a single >10s quiet stretch (e.g. a kernel
        # blocked on ``input_request`` while the user OAuths in the browser)
        # will raise ``TimeoutError`` even though the underlying execution is
        # still healthy. Callers that know they need a longer ceiling can
        # pass ``timeout=`` here; otherwise we forward whatever the upstream
        # default is (currently 10s).
        kwargs = {"allow_stdin": allow_stdin}
        if timeout is not None:
            kwargs["timeout"] = timeout

        # Wrap stdin_hook to log inputs
        original_stdin_hook = stdin_hook

        def wrapped_stdin_hook(prompt):
            if self.history and self.session_name:
                self.history.log_event(
                    self.session_name, "stdin_request", {"prompt": prompt}
                )

            res = original_stdin_hook(prompt) if original_stdin_hook else input(prompt)

            if self.history and self.session_name:
                self.history.log_event(self.session_name, "input_reply", {"value": res})
            return res

        if allow_stdin:
            kwargs["stdin_hook"] = wrapped_stdin_hook

        if output_hook:
            # If we have an output hook, we use execute_interactive and manage buffering ourselves
            outputs = []

            def wrapped_output_hook(msg):
                from jupyter_kernel_client.client import (
                    output_hook as default_output_hook,
                )

                # Update local outputs list using the default logic
                new_indexes = default_output_hook(outputs, msg)
                # If new outputs were added, call our streaming hook with the new data
                if new_indexes:
                    for idx in sorted(new_indexes):
                        if idx < len(outputs):
                            output_hook(outputs[idx])

            reply = self.kernel_client.execute_interactive(
                code, output_hook=wrapped_output_hook, **kwargs
            )
            # execute_interactive returns the raw reply message
            reply_content = reply["content"] if reply else {"status": "error"}
        else:
            reply = self.kernel_client.execute(code, **kwargs)
            if not reply:
                return []
            outputs = reply.get("outputs", [])
            reply_content = reply

        # If there's an error status but no error in outputs, synthesize one
        if reply_content.get("status") == "error":
            has_error_output = any(o.get("output_type") == "error" for o in outputs)
            if not has_error_output:
                outputs.append(
                    {
                        "output_type": "error",
                        "ename": reply_content.get("ename", "Error"),
                        "evalue": reply_content.get("evalue", "Unknown error"),
                        "traceback": reply_content.get("traceback", []),
                    }
                )

        return outputs

    def stop(self, shutdown_kernel: bool = False):
        if self._kernel_client:
            try:
                # We manage kernel lifecycle explicitly.
                # To prevent automatic shutdown, we bypass the manager's stop() and
                # directly close the channels and socket.
                client = self._kernel_client._manager.client
                client.stop_channels()
                if client.kernel_socket:
                    client.kernel_socket.close()

                if shutdown_kernel:
                    self._kernel_client._manager.shutdown_kernel(now=True)
            except Exception:
                logging.exception("Error stopping kernel client")
