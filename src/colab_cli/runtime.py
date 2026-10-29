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
import queue
import time
from typing import Any, Callable, Dict, List, Optional

import jupyter_kernel_client
import requests


# The longest single wait for a kernel message, so a lost connection is noticed.
_WAIT_SLICE_SEC = 1.0
# Kernel states that end any execution in progress.
_KERNEL_GONE = frozenset({"dead", "restarting"})


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

                    # Resolve the kernel client class across
                    # jupyter-kernel-client variants:
                    # - git (googlecolab fork): ColabKernelClient(proxy_token=...)
                    # - PyPI <1.0: KernelClient(token=...)
                    # - PyPI >=1.0: JupyterKernelClient(token=...)
                    #   (KernelClient was renamed, breaking `colab exec` with
                    #   AttributeError when installed from PyPI).
                    if hasattr(jupyter_kernel_client, "ColabKernelClient"):
                        _ClientCls = jupyter_kernel_client.ColabKernelClient
                        _token_kw = "proxy_token"
                    elif hasattr(jupyter_kernel_client, "KernelClient"):
                        _ClientCls = jupyter_kernel_client.KernelClient
                        _token_kw = "token"
                    else:
                        _ClientCls = jupyter_kernel_client.JupyterKernelClient
                        _token_kw = "token"
                    self._kernel_client = _ClientCls(
                        server_url=self.url,
                        **{_token_kw: self.token},
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
        # ``timeout`` means one of two things, depending on the path:
        # - Streaming without stdin (``output_hook`` set, ``allow_stdin``
        #   False; used by run, exec and repl): ``_execute_streaming`` waits
        #   for messages itself, and ``timeout`` is an idle limit. ``None``
        #   or a value <= 0 means no limit.
        # - Otherwise the call goes to ``jupyter_kernel_client``, where
        #   ``timeout`` is a deadline for the whole call, defaulting to
        #   ``REQUEST_TIMEOUT`` (10 s). 0.8.0 and 0.9.0 never enforce it and
        #   poll without waiting once it passes; 1.0.2 raises
        #   ``TimeoutError``. Callers that need a longer ceiling, such as
        #   the interactive automations, pass ``timeout=`` explicitly.
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
            # With an output hook, we stream messages and manage buffering ourselves
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

            if allow_stdin:
                reply = self.kernel_client.execute_interactive(
                    code, output_hook=wrapped_output_hook, **kwargs
                )
            else:
                reply = self._execute_streaming(code, wrapped_output_hook, timeout)
            # Both paths return the raw execute_reply message
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

    def _execute_streaming(
        self,
        code: str,
        output_hook: Callable[[Dict[str, Any]], None],
        timeout: Optional[float],
    ) -> Dict[str, Any]:
        """Run ``code``, pass each output message to ``output_hook``, return the reply.

        ``timeout`` is an idle limit: how long to wait, in seconds, for the next
        message from this execution. A run that keeps printing can take as long
        as it needs, and ``None`` or a value <= 0 waits without a limit. This
        replaces ``jupyter_kernel_client``'s ``execute_interactive``, whose
        deadline covers the whole run and which, in 0.8.0 and 0.9.0, polls
        without waiting once the deadline passes.

        Like ``_apply_ws_hook``, this uses the websocket client behind the
        kernel client. It skips that library's per-client execution lock,
        because a ColabRuntime runs one execution at a time.
        """
        if timeout is not None and timeout <= 0:
            timeout = None
        client = self.kernel_client._manager.client
        msg_id = client.execute(code, allow_stdin=False)
        last_message = time.monotonic()

        def next_message(get_msg, waiting_for):
            nonlocal last_message
            while True:
                if not client.connection_ready.is_set():
                    raise RuntimeError("Connection was lost.")
                wait = _WAIT_SLICE_SEC
                if timeout is not None:
                    left = last_message + timeout - time.monotonic()
                    if left <= 0:
                        raise TimeoutError(
                            f"Timeout waiting for {waiting_for}: no message from "
                            f"the kernel for {timeout:g}s. Use --timeout to allow "
                            "longer silences."
                        )
                    wait = min(wait, left)
                try:
                    msg = get_msg(timeout=wait)
                except queue.Empty:
                    continue
                state = msg["content"].get("execution_state")
                if msg["header"]["msg_type"] == "status" and state in _KERNEL_GONE:
                    # The kernel announces these to every client, without a
                    # parent, and our execution can no longer finish.
                    raise RuntimeError(f"Kernel {state} before the code finished.")
                if msg["parent_header"].get("msg_id") == msg_id:
                    last_message = time.monotonic()
                    return msg

        while True:
            msg = next_message(client.get_iopub_msg, "output")
            output_hook(msg)
            if (
                msg["header"]["msg_type"] == "status"
                and msg["content"].get("execution_state") == "idle"
            ):
                return next_message(client.get_shell_msg, "reply")

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
