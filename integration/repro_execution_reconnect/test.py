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

"""Drop a live kernel socket twice and follow the original execution to completion."""

import socket
import subprocess
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.runtime import ColabRuntime


def main():
    name = f"repro-reconnect-{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory(prefix="colab-reconnect-") as directory:
        config = str(Path(directory) / "sessions.json")
        cli = ["colab", "--config", config]
        try:
            subprocess.run([*cli, "new", "-s", name], check=True, timeout=300)
            original_streaming = ColabRuntime._execute_streaming
            dropped = set()
            requests = []
            sessions = []

            def inject_disconnect(runtime, code, output_hook, timeout):
                client = runtime.kernel_client._manager.client

                def observe(msg):
                    output_hook(msg)
                    if msg["header"]["msg_type"] != "stream":
                        return
                    for marker in ("RECONNECT_FIRST", "RECONNECT_SECOND"):
                        if marker in msg["content"]["text"] and marker not in dropped:
                            dropped.add(marker)
                            requests.append(msg["parent_header"]["msg_id"])
                            sessions.append(client.session.session)
                            # Break only the TCP connection. WebSocketApp.close()
                            # would request a deliberate shutdown and disable recovery.
                            client.kernel_socket.sock.sock.shutdown(socket.SHUT_RDWR)

                return original_streaming(runtime, code, observe, timeout)

            code = """import time
monitor_reconnect_count = globals().get('monitor_reconnect_count', 0) + 1
print('RECONNECT_FIRST', flush=True)
time.sleep(5)
print('RECONNECT_SECOND', flush=True)
time.sleep(5)
print(f'RECONNECT_DONE count={monitor_reconnect_count}', flush=True)
"""
            runner = CliRunner()
            with patch.object(ColabRuntime, "_execute_streaming", inject_disconnect):
                result = runner.invoke(
                    app,
                    ["--config", config, "exec", "-s", name, "--timeout", "30"],
                    input=code,
                )
            print(result.output)
            if result.exception:
                raise result.exception
            assert dropped == {"RECONNECT_FIRST", "RECONNECT_SECOND"}, dropped
            assert len(set(requests)) == 1, requests
            assert len(set(sessions)) == 1, sessions
            assert result.output.count("RECONNECT_DONE count=1") == 1, result.output

            # A new CLI invocation must attach to the same kernel and see the
            # side effect exactly once; the first task was never resubmitted.
            verify = subprocess.run(
                [*cli, "exec", "-s", name],
                input="print(monitor_reconnect_count)",
                text=True,
                capture_output=True,
                check=True,
                timeout=60,
            )
            assert verify.stdout.strip() == "1", verify.stdout
            print(
                "PASS: recovered twice, completed the original request, ran code once."
            )
        finally:
            subprocess.run([*cli, "stop", "-s", name], check=True, timeout=60)
            subprocess.run([*cli, "sessions"], check=True, timeout=60)


if __name__ == "__main__":
    main()
