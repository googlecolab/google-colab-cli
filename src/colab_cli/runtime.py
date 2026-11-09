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
import uuid
from collections import deque
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

import jupyter_kernel_client
import requests
import websocket

from colab_cli.client import ColabRequestError
from colab_cli.execution_journal import ExecutionJournal

# The longest single wait for a kernel message, so a lost connection is noticed.
_WAIT_SLICE_SEC = 1.0
# Bound transport recovery even when execution has no idle timeout.
_RECONNECT_TIMEOUT_SEC = 30.0
# Kernel states that end any execution in progress.
_KERNEL_GONE = frozenset({"dead", "restarting"})
_logger = logging.getLogger(__name__)


class ExecutionOutcomeUnknown(RuntimeError):
    """Monitoring failed without establishing whether remote work finished."""


class ExecutionIdleTimeout(TimeoutError):
    """The idle limit elapsed; the original execution may still be running."""


class KernelTerminated(RuntimeError):
    """The kernel explicitly reported that execution cannot continue."""


class RuntimeUnavailable(ExecutionOutcomeUnknown):
    """The original assignment is no longer available for recovery."""


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
        refresh_connection: Callable[[float], tuple[str, str]] | None = None,
        on_execution_record: Callable[[dict[str, str] | None], None] | None = None,
    ):
        self.url = url
        self.token = token
        self.session_name = session_name
        self.history = history
        self.kernel_id = kernel_id
        self.session_id = session_id
        self.on_kernel_started = on_kernel_started
        self.on_session_started = on_session_started
        self.refresh_connection = refresh_connection
        self.on_execution_record = on_execution_record
        self._restart_transport = False
        self._last_proxy_refresh = None
        # Jupyter IDs include session, PID and a counter. A saved session may
        # be reopened by multiple runtimes in one process (e.g. an embedded
        # CLI), so start in a unique range and preserve it across socket rebuilds.
        self._message_count = uuid.uuid4().int
        self._kernel_client = None
        self._transport_interrupted = False
        self.colab_request_hook: Optional[Callable[[Dict[str, Any], Any], None]] = None

    def _apply_ws_hook(self):
        wsclient = self._kernel_client._manager.client
        original_on_message = wsclient.kernel_socket.on_message
        original_on_error = wsclient.kernel_socket.on_error
        original_on_close = wsclient.kernel_socket.on_close

        def on_error(*args):
            self._transport_interrupted = True
            if original_on_error:
                original_on_error(*args)

        def on_close(*args):
            self._transport_interrupted = True
            if original_on_close:
                original_on_close(*args)

        wsclient.kernel_socket.on_error = on_error
        wsclient.kernel_socket.on_close = on_close

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
        if self._restart_transport:
            self.stop()
            self._kernel_client = None
            self._restart_transport = False
        if not self._kernel_client:
            retries = 3
            backoff = 2
            last_err = None

            for i in range(retries):
                try:
                    client_kwargs = {
                        "subprotocol": jupyter_kernel_client.JupyterSubprotocol.DEFAULT,
                        "extra_params": {"colab-runtime-proxy-token": self.token},
                        "reconnect_interval": 1,
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
                    session = self._kernel_client._manager.client.session
                    if isinstance(session.message_count, int):
                        # A rebuilt client retains the Jupyter session ID. Its
                        # counter must not restart and match buffered old replies.
                        session.message_count = max(
                            session.message_count, self._message_count
                        )
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
                except (requests.RequestException, websocket.WebSocketException) as e:
                    last_err = e
                    self.stop()
                    self._kernel_client = None
                    if i < retries - 1:
                        self._refresh_proxy(5.0)
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

    def _refresh_proxy(self, timeout, journal=None):
        if self.refresh_connection is None:
            return False
        now = time.monotonic()
        if self._last_proxy_refresh is not None and now - self._last_proxy_refresh < 5:
            return False
        self._last_proxy_refresh = now
        url, token = self.refresh_connection(timeout)
        self._restart_transport |= (url, token) != (self.url, self.token)
        self.url, self.token = url, token
        if journal is not None:
            journal.update_connection(url, token)
        return True

    def _finish_journal(self, journal):
        if self.on_execution_record:
            self.on_execution_record(None)
        try:
            journal.remove()
        except requests.RequestException:
            _logger.warning("Could not remove completed execution recovery record.")

    def resume_execution(self, request_id, path, output_hook=None, timeout=30.0):
        """Read the original transcript; never start a kernel or submit code."""
        from jupyter_kernel_client.client import output_hook as collect_output

        journal = ExecutionJournal(self.url, self.token, request_id, path=path)
        outputs = []

        def receive(msg):
            indexes = collect_output(outputs, msg)
            if output_hook and indexes:
                for index in sorted(indexes):
                    output_hook(outputs[index])

        reply = self._wait_for_execution(None, request_id, receive, timeout, journal)
        if reply["content"].get("status") != "ok" and not any(
            out.get("output_type") == "error" for out in outputs
        ):
            content = reply["content"]
            error = {
                "output_type": "error",
                "ename": content.get("ename", "ExecutionAborted"),
                "evalue": content.get(
                    "evalue", f"Execution returned {content.get('status', 'no status')}"
                ),
                "traceback": content.get("traceback", []),
            }
            outputs.append(error)
            if output_hook:
                output_hook(error)
        self._finish_journal(journal)
        return outputs

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
        if allow_stdin and output_hook is not None:
            raise NotImplementedError(
                "output_hook is not supported when allow_stdin is True"
            )

        # ``timeout`` means one of two things, depending on ``allow_stdin``:
        # - Without stdin (``allow_stdin`` False; used by run, exec, repl, and
        #   install): ``_execute_streaming`` waits for messages itself, and
        #   ``timeout`` is an idle limit. ``None`` or a value <= 0 means no
        #   limit.
        # - With stdin (``allow_stdin`` True; used by auth and drivemount):
        #   the call goes to ``jupyter_kernel_client``, where ``timeout`` is a
        #   deadline for the whole call, defaulting to ``REQUEST_TIMEOUT``
        #   (10 s). Callers that need a longer ceiling pass ``timeout=``
        #   explicitly.
        if allow_stdin:
            original_stdin_hook = stdin_hook

            def wrapped_stdin_hook(prompt):
                if self.history and self.session_name:
                    self.history.log_event(
                        self.session_name, "stdin_request", {"prompt": prompt}
                    )

                res = (
                    original_stdin_hook(prompt)
                    if original_stdin_hook
                    else input(prompt)
                )

                if self.history and self.session_name:
                    self.history.log_event(
                        self.session_name, "input_reply", {"value": res}
                    )
                return res

            kwargs: Dict[str, Any] = {
                "allow_stdin": True,
                "stdin_hook": wrapped_stdin_hook,
            }
            if timeout is not None:
                kwargs["timeout"] = timeout

            reply = self.kernel_client.execute(code, **kwargs)
            if not reply:
                return []
            outputs = reply.get("outputs", [])
            reply_content = reply
        else:
            outputs = []

            def wrapped_output_hook(msg):
                from jupyter_kernel_client.client import (
                    output_hook as default_output_hook,
                )

                # Update local outputs list using the default logic
                new_indexes = default_output_hook(outputs, msg)
                # If new outputs were added, call our streaming hook with the new data
                if output_hook and new_indexes:
                    for idx in sorted(new_indexes):
                        if idx < len(outputs):
                            output_hook(outputs[idx])

            reply = self._execute_streaming(code, wrapped_output_hook, timeout)
            reply_content = reply["content"] if reply else {"status": "error"}

        # If there's an error status but no error in outputs, synthesize one
        if reply_content.get("status", "ok" if allow_stdin else None) != "ok":
            has_error_output = any(o.get("output_type") == "error" for o in outputs)
            if not has_error_output:
                outputs.append(
                    {
                        "output_type": "error",
                        "ename": reply_content.get("ename", "ExecutionAborted"),
                        "evalue": reply_content.get(
                            "evalue",
                            f"Execution returned {reply_content.get('status', 'no status')}",
                        ),
                        "traceback": reply_content.get("traceback", []),
                    }
                )

        return outputs

    def _start_execution(self, client, code):
        # Build the request before installing its journal, but send user code
        # only after the kernel confirms that recording is ready. Keeping the
        # original source intact preserves IPython magics and top-level await.
        request = client.session.msg(
            "execute_request",
            {
                "code": code,
                "silent": False,
                "store_history": True,
                "user_expressions": {},
                "allow_stdin": False,
                "stop_on_error": True,
            },
        )
        msg_id = request["header"]["msg_id"]
        journal = ExecutionJournal(self.url, self.token, msg_id)
        setup_id = client.execute(
            journal.install_code(),
            silent=True,
            store_history=False,
            allow_stdin=False,
        )
        try:
            setup = self._wait_for_execution(client, setup_id, lambda msg: None, 30)
        except (ExecutionOutcomeUnknown, ExecutionIdleTimeout) as error:
            raise RuntimeError(
                "Could not confirm execution recovery setup; user code was not submitted."
            ) from error
        if setup["content"].get("status") != "ok":
            raise RuntimeError(
                "Could not prepare execution recovery; user code was not submitted. "
                + setup["content"].get("evalue", "Kernel journal setup failed.")
            )
        self._transport_interrupted = False
        if self.on_execution_record:
            self.on_execution_record({"request_id": msg_id, "path": journal.path})
        try:
            client.shell_channel.send(request)
        except Exception as error:
            raise ExecutionOutcomeUnknown(
                "Connection failed while submitting code; whether the remote "
                "execution started is unknown. The code has not been resubmitted."
            ) from error
        return msg_id, journal

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
        for attempt in range(2):
            client = self.kernel_client._manager.client
            try:
                msg_id, journal = self._start_execution(client, code)
                break
            except ExecutionOutcomeUnknown:
                # Submission may already have happened. Never retry it.
                raise
            except RuntimeError:
                # Setup failed before submitting user code. A fresh route and
                # journal may safely be prepared on the same kernel.
                if attempt or not self._refresh_proxy(5.0):
                    raise
                self._restart_transport = True
        try:
            reply = self._wait_for_execution(
                client, msg_id, output_hook, timeout, journal
            )
        except (ExecutionOutcomeUnknown, ExecutionIdleTimeout, KernelTerminated):
            raise
        except Exception as error:
            raise ExecutionOutcomeUnknown(
                "Execution monitoring failed; the remote result is unknown. "
                "The code has not been resubmitted."
            ) from error
        if journal is not None:
            self._finish_journal(journal)
        return reply

    def _wait_for_execution(self, client, msg_id, output_hook, timeout, journal=None):
        if timeout is not None and timeout <= 0:
            timeout = None
        disconnected_at = None
        recovering = client is None
        last_read = None
        journal_unavailable_at = None
        completion_seen_at = None
        last_kernel_check = None
        kernel_idle_at = None
        pending = {"output": deque(), "reply": deque()}
        seen = set()
        queued = set()

        def enqueue(msg):
            identity = msg["header"]["msg_id"]
            if (
                msg["parent_header"].get("msg_id") != msg_id
                or identity in seen
                or identity in queued
            ):
                return
            channel = (
                "reply" if msg["header"]["msg_type"] == "execute_reply" else "output"
            )
            pending[channel].append(msg)
            queued.add(identity)

        def replay(deadline, force=False):
            nonlocal last_read, journal_unavailable_at
            if journal is None:
                return
            now = time.monotonic()
            if (
                not force
                and last_read is not None
                and now - last_read < _WAIT_SLICE_SEC
            ):
                return
            last_read = now
            budget = min(5.0, max(0.1, deadline - now)) if deadline is not None else 5.0
            try:
                for msg in journal.read(timeout=budget):
                    enqueue(msg)
                journal_unavailable_at = None
            except (requests.RequestException, OSError):
                # A second transport (HTTP) may also be temporarily unavailable.
                # Keep the same request and deadlines; never rerun user code.
                logging.debug("Execution recovery record is temporarily unavailable.")
                if journal_unavailable_at is None:
                    journal_unavailable_at = time.monotonic()
                try:
                    self._refresh_proxy(budget, journal)
                except RuntimeUnavailable:
                    raise
                except (requests.RequestException, ColabRequestError, OSError):
                    _logger.debug("Runtime proxy refresh is temporarily unavailable.")
            except ValueError as error:
                raise ExecutionOutcomeUnknown(
                    "Execution recovery record is incomplete or invalid; the remote "
                    "execution result is unknown. The code has not been resubmitted."
                ) from error

        def next_message(get_msg, waiting_for):
            nonlocal disconnected_at, recovering, completion_seen_at
            nonlocal last_kernel_check, kernel_idle_at
            deadline = (time.monotonic() + timeout) if timeout is not None else None
            while True:
                # websocket-client reconnects using the same socket app and
                # Jupyter session. Keep waiting for msg_id; resubmitting code
                # here would execute the user's work twice. connection_ready
                # alone can remain set while the transport is reconnecting.
                socket_app = client.kernel_socket if client is not None else None
                socket = socket_app.sock if socket_app is not None else None
                connected = (
                    client is not None
                    and client.connection_ready.is_set()
                    and socket is not None
                    and socket.connected
                )
                if connected:
                    disconnected_at = None
                else:
                    recovering = True
                    if client is not None and (
                        client.connection_thread is None
                        or not client.connection_thread.is_alive()
                    ):
                        self._restart_transport = True
                    if disconnected_at is None:
                        disconnected_at = time.monotonic()
                        logging.warning(
                            "Kernel connection lost; waiting for reconnection."
                        )
                recovering = recovering or self._transport_interrupted
                if recovering:
                    replay(deadline)
                if pending[waiting_for]:
                    msg = pending[waiting_for].popleft()
                    identity = msg["header"]["msg_id"]
                    queued.remove(identity)
                    seen.add(identity)
                    if (
                        journal is not None
                        and msg["header"]["msg_type"] == "status"
                        and msg["content"].get("execution_state") == "idle"
                        and completion_seen_at is None
                    ):
                        completion_seen_at = time.monotonic()
                    return msg
                if any(
                    start is not None
                    and time.monotonic() - start >= _RECONNECT_TIMEOUT_SEC
                    for start in (journal_unavailable_at, completion_seen_at)
                ):
                    raise ExecutionOutcomeUnknown(
                        "Execution recovery could not confirm all output and completion. "
                        "The remote result is unknown; the code has not been resubmitted."
                    )
                if (
                    not connected
                    and journal is None
                    and (
                        client.connection_thread is None
                        or not client.connection_thread.is_alive()
                        or time.monotonic() - disconnected_at >= _RECONNECT_TIMEOUT_SEC
                    )
                ):
                    raise ExecutionOutcomeUnknown(
                        "Connection was lost and could not be restored. "
                        "The remote execution may still be running; "
                        "the code has not been resubmitted."
                    )
                # HTTP journal recovery remains usable even after the socket
                # reconnect worker exits. Check the original kernel, never
                # create a replacement to make an incomplete record look live.
                now = time.monotonic()
                if (
                    not connected
                    and journal is not None
                    and self.kernel_id
                    and (last_kernel_check is None or now - last_kernel_check >= 5)
                ):
                    last_kernel_check = now
                    try:
                        response = requests.get(
                            f"{self.url.rstrip('/')}/api/kernels/{quote(self.kernel_id, safe='')}",
                            params={
                                "authuser": "0",
                                "colab-runtime-proxy-token": self.token,
                            },
                            timeout=min(5.0, max(0.1, deadline - now))
                            if deadline
                            else 5.0,
                        )
                        response.raise_for_status()
                        state = response.json().get("execution_state")
                        if state in _KERNEL_GONE:
                            raise KernelTerminated(f"Original kernel is {state}.")
                        if state == "idle":
                            if kernel_idle_at is None:
                                kernel_idle_at = now
                            elif now - kernel_idle_at >= _RECONNECT_TIMEOUT_SEC:
                                raise ExecutionOutcomeUnknown(
                                    "Original kernel is idle but its completion record is missing. "
                                    "The code has not been resubmitted."
                                )
                        else:
                            kernel_idle_at = None
                    except (requests.RequestException, OSError, ValueError):
                        if journal_unavailable_at is None:
                            # A readable journal may have survived a kernel
                            # restart. Bound the wait for its missing completion.
                            if kernel_idle_at is None:
                                kernel_idle_at = now
                            elif now - kernel_idle_at >= _RECONNECT_TIMEOUT_SEC:
                                raise ExecutionOutcomeUnknown(
                                    "Could not confirm the original kernel's result. "
                                    "The code has not been resubmitted."
                                )
                wait = _WAIT_SLICE_SEC
                if deadline is not None:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        replay(deadline, force=True)
                        if pending[waiting_for]:
                            continue
                        raise ExecutionIdleTimeout(
                            f"Timeout waiting for {waiting_for}: no message from "
                            f"the kernel for {timeout:g}s. Use --timeout to allow "
                            "longer silences. The remote execution may still be "
                            "running; the code has not been resubmitted."
                        )
                    wait = min(wait, left)
                try:
                    if get_msg is None:
                        time.sleep(wait)
                        continue
                    msg = get_msg(timeout=wait)
                except queue.Empty:
                    continue
                state = msg["content"].get("execution_state")
                if msg["header"]["msg_type"] == "status" and state in _KERNEL_GONE:
                    # The kernel announces these to every client, without a
                    # parent, and our execution can no longer finish.
                    raise KernelTerminated(f"Kernel {state} before the code finished.")
                if msg["parent_header"].get("msg_id") == msg_id:
                    # Reconcile before accepting completion even if a brief
                    # outage occurred entirely between socket-state polls.
                    idle = msg["header"]["msg_type"] == "status" and state == "idle"
                    if journal is not None and idle and completion_seen_at is None:
                        completion_seen_at = time.monotonic()
                    if journal is not None and (
                        recovering or self._transport_interrupted or idle
                    ):
                        recovering = True
                        replay(deadline, force=True)
                    else:
                        enqueue(msg)

        while True:
            msg = next_message(
                client.get_iopub_msg if client is not None else None, "output"
            )
            output_hook(msg)
            if (
                msg["header"]["msg_type"] == "status"
                and msg["content"].get("execution_state") == "idle"
            ):
                return next_message(
                    client.get_shell_msg if client is not None else None, "reply"
                )

    def stop(self, shutdown_kernel: bool = False):
        if self._kernel_client:
            try:
                # We manage kernel lifecycle explicitly.
                # To prevent automatic shutdown, we bypass the manager's stop() and
                # directly close the channels and socket.
                client = self._kernel_client._manager.client
                if isinstance(client.session.message_count, int):
                    self._message_count = max(
                        self._message_count, client.session.message_count
                    )
                client.stop_channels()
                if client.kernel_socket:
                    client.kernel_socket.close()

                if shutdown_kernel:
                    self._kernel_client._manager.shutdown_kernel(now=True)
            except Exception:
                logging.exception("Error stopping kernel client")
