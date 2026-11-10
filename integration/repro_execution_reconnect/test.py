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

"""Real CPU tests for disconnected output, completion, and uncertain run cleanup."""

import argparse
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import requests as http
from rich.text import Text
from typer.testing import CliRunner

from colab_cli.cli import app
from colab_cli.common import State
from colab_cli.contents import ContentsClient
from colab_cli.runtime import ColabRuntime, ExecutionIdleTimeout


def main(selected_scenarios=None):
    name = f"repro-reconnect-{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory(prefix="colab-reconnect-") as directory:
        config = str(Path(directory) / "sessions.json")
        command = [sys.executable, "-m", "colab_cli.cli", "--config", config]
        state = State()
        state.config_path = config
        state.store.list()
        owned = set()

        def cli(*args, **kwargs):
            check = kwargs.pop("check", False)
            return subprocess.run(
                [*command, *args], text=True, timeout=180, check=check, **kwargs
            )

        def remember(session_name):
            saved = state.store.get(session_name)
            if saved is not None:
                owned.add(saved.endpoint)
                print(f"Recorded test endpoint: {saved.endpoint}", flush=True)
            return saved

        def verify(code):
            result = cli("exec", "-s", name, input=code, capture_output=True)
            assert result.returncode == 0, re.sub(
                r'colab-runtime-proxy-token=[^\s&\'"<>]+',
                "colab-runtime-proxy-token=<redacted>",
                result.stderr,
            )
            return result.stdout

        def verify_missing_kernel():
            result = cli(
                "exec",
                "-s",
                name,
                "--timeout",
                "1",
                input="import time; print('KERNEL_LOSS_STARTED', flush=True); time.sleep(60)",
                capture_output=True,
            )
            assert result.returncode != 0
            saved = state.store.get(name)
            identity = saved.execution.copy()
            params = {"authuser": "0", "colab-runtime-proxy-token": saved.token}
            route = saved.url.rstrip("/") + "/api/kernels"
            response = http.delete(
                route + "/" + saved.kernel_id, params=params, timeout=10
            )
            response.raise_for_status()
            before = {a.endpoint for a in state.client.list_assignments()}
            result = cli("resume", "-s", name, "--timeout", "0", capture_output=True)
            assert result.returncode != 0, (
                "A destroyed kernel must not be reported as successful work"
            )
            response = http.get(route, params=params, timeout=10)
            response.raise_for_status()
            assert not response.json(), "Resume must not create a replacement kernel"
            assert {a.endpoint for a in state.client.list_assignments()} == before
            saved = state.store.get(name)
            if saved.execution is None:
                assert "Result recovered" in result.stderr
                print(
                    "PASS: kernel shutdown error recovered from the original journal; no replacement created.",
                    flush=True,
                )
            else:
                assert saved.execution == identity
                print(
                    "PASS: lost kernel reported uncertainty without replacement or resubmission.",
                    flush=True,
                )

        try:
            # A real idle timeout leaves work in progress. `run` must preserve it.
            script = Path(directory) / "still_running.py"
            receipt = f"/content/{name}.json"
            script.write_text(
                "import json, time\nfrom pathlib import Path\n"
                "run_reconnect_count = globals().get('run_reconnect_count', 0) + 1\n"
                "print('RUN_STARTED', flush=True)\ntime.sleep(3)\n"
                f"Path({receipt!r}).write_text(json.dumps({{'count': run_reconnect_count, 'finished': True}}))\n",
                encoding="utf-8",
            )
            result = CliRunner().invoke(
                app,
                [
                    "--config",
                    config,
                    "run",
                    "-s",
                    name,
                    "--timeout",
                    "1",
                    str(script),
                ],
            )
            saved = remember(name)
            print(result.output, flush=True)
            assert isinstance(result.exception, ExecutionIdleTimeout), repr(
                result.exception
            )
            assert saved is not None and saved.running.startswith("unknown:")
            assert "was preserved" in result.output
            assert saved.endpoint in {
                a.endpoint for a in state.client.list_assignments()
            }
            resumed = cli(
                "resume", "-s", name, "--timeout", "30", capture_output=True, check=True
            )
            assert "Result recovered" in resumed.stderr
            expected = json.dumps({"count": 1, "finished": True})
            assert expected in verify(
                f"from pathlib import Path; print(Path({receipt!r}).read_text())"
            )
            saved = state.store.get(name)
            kernel, session = saved.kernel_id, saved.session_id
            print(
                "PASS: uncertain run preserved its runtime; work finished once.",
                flush=True,
            )
            if selected_scenarios == ["missing_kernel"]:
                verify_missing_kernel()
                return

            created_journals = {}
            for scenario, delay in (
                ("two_drops", 15),
                ("offline_success", 0.2),
                ("offline_error", 0.2),
                ("offline_no_limit", 0.2),
                ("stale_token", 0.2),
                ("stale_route", 0.2),
                ("stopped_socket_thread", 20),
            ):
                if (
                    selected_scenarios is not None
                    and scenario not in selected_scenarios
                ):
                    continue
                original = ColabRuntime._execute_streaming
                original_start = ColabRuntime._start_execution
                dropped, requests, sessions = set(), [], []

                def capture_journal(
                    runtime,
                    client,
                    code,
                    original_start=original_start,
                    scenario=scenario,
                ):
                    identity, journal = original_start(runtime, client, code)
                    label = (
                        "followup"
                        if "AFTER_SOCKET_RECOVERY" in code
                        else ("main" if "RECONNECT_FIRST" in code else "prelude")
                    )
                    created_journals[journal.path] = f"{scenario}:{label}"
                    runtime._test_journal = journal
                    return identity, journal

                def inject_disconnect(
                    runtime,
                    code,
                    output_hook,
                    timeout,
                    *,
                    scenario=scenario,
                    dropped=dropped,
                    requests=requests,
                    sessions=sessions,
                    original=original,
                ):
                    client = runtime.kernel_client._manager.client

                    def observe(msg):
                        output_hook(msg)
                        if msg["header"]["msg_type"] != "stream":
                            return
                        markers = (
                            ("RECONNECT_FIRST", "RECONNECT_SECOND")
                            if scenario == "two_drops"
                            else ("RECONNECT_FIRST",)
                        )
                        for marker in markers:
                            if (
                                marker in msg["content"]["text"]
                                and marker not in dropped
                            ):
                                dropped.add(marker)
                                requests.append(msg["parent_header"]["msg_id"])
                                sessions.append(client.session.session)
                                if scenario in ("stale_token", "stale_route"):
                                    if scenario == "stale_token":
                                        runtime.token = "deliberately-invalid"
                                    else:
                                        runtime.url += "/deliberately-stale-route"
                                    runtime._test_journal.update_connection(
                                        runtime.url, runtime.token
                                    )
                                if scenario == "stopped_socket_thread":
                                    client.stop_channels()
                                    assert (
                                        client.connection_thread is None
                                        or not client.connection_thread.is_alive()
                                    )
                                    continue
                                client.kernel_socket.sock.sock.shutdown(
                                    socket.SHUT_RDWR
                                )

                    result = original(runtime, code, observe, timeout)
                    if (
                        scenario == "stopped_socket_thread"
                        and "RECONNECT_FIRST" in code
                    ):
                        ids = (runtime.kernel_id, runtime.session_id)
                        following = runtime.execute_code(
                            "print('AFTER_SOCKET_RECOVERY')"
                        )
                        assert any(
                            "AFTER_SOCKET_RECOVERY" in out.get("text", "")
                            for out in following
                        )
                        assert (runtime.kernel_id, runtime.session_id) == ids
                    return result

                ending = (
                    "raise ValueError('OFFLINE_ERROR')"
                    if scenario == "offline_error"
                    else "print('RECONNECT_DONE count=1', flush=True)"
                )
                code = f"""import asyncio, json, sys, time
from pathlib import Path
assert 'install' not in globals()
await asyncio.sleep(0)
reconnect_counts = globals().setdefault('reconnect_counts', {{}})
reconnect_counts[{scenario!r}] = reconnect_counts.get({scenario!r}, 0) + 1
print('RECONNECT_FIRST', flush=True)
time.sleep({delay})
print('RECONNECT_SECOND', flush=True)
print('OFFLINE_STDERR', file=sys.stderr, flush=True)
from IPython.display import display
display({{'recovered_display': {scenario!r}}})
display({{'image/png': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lU8AAAAASUVORK5CYII='}}, raw=True)
time.sleep({delay})
Path({receipt!r}).write_text(json.dumps({{'count': reconnect_counts[{scenario!r}], 'finished': True}}))
{ending}
"""
                with (
                    patch.object(ColabRuntime, "_execute_streaming", inject_disconnect),
                    patch.object(ColabRuntime, "_start_execution", capture_journal),
                ):
                    result = CliRunner().invoke(
                        app,
                        [
                            "--config",
                            config,
                            "exec",
                            "-s",
                            name,
                            "--timeout",
                            "0"
                            if scenario in ("offline_no_limit", "stopped_socket_thread")
                            else "30",
                            "--output-image",
                            str(Path(directory) / (scenario + ".png")),
                        ],
                        input=code,
                    )
                print(result.output, flush=True)
                if result.exception:
                    raise result.exception
                assert len(dropped) == (2 if scenario == "two_drops" else 1), dropped
                assert len(set(requests)) == len(set(sessions)) == 1
                for marker in ("RECONNECT_FIRST", "RECONNECT_SECOND", "OFFLINE_STDERR"):
                    assert result.output.count(marker) == 1, (
                        scenario,
                        marker,
                        result.output,
                    )
                assert "recovered_display" in result.output
                assert (
                    (Path(directory) / (scenario + ".png"))
                    .read_bytes()
                    .startswith(b"\x89PNG\r\n\x1a\n")
                )
                if scenario == "offline_error":
                    assert (
                        "ValueError: OFFLINE_ERROR"
                        in Text.from_ansi(result.output).plain
                    )
                else:
                    assert result.output.count("RECONNECT_DONE count=1") == 1
                assert expected in verify(
                    f"from pathlib import Path; print(Path({receipt!r}).read_text())"
                )
                saved = state.store.get(name)
                assert (saved.kernel_id, saved.session_id) == (kernel, session)
                print(
                    f"PASS: {scenario}, complete output/result, same kernel/session, ran once.",
                    flush=True,
                )

            assert "MAGIC_OK" in verify("!echo MAGIC_OK")
            entries = ContentsClient(state.store.get(name)).list_dir(
                "/content/.colab-cli-executions"
            )
            # Completed and explicitly resumed work both remove their records.
            records = [entry for entry in entries["content"] if entry["type"] == "file"]
            assert len(records) == 0, [
                (
                    entry["name"],
                    created_journals.get("/" + entry["path"]),
                    entry["size"],
                )
                for entry in records
            ]

            # Kill a real CLI process after its user code starts. A different
            # process must recover the original transcript, with no submission.
            code = f"""import time, json
from pathlib import Path
process_resume_count = globals().get('process_resume_count', 0) + 1
print('PROCESS_WORK_STARTED', flush=True)
time.sleep(15)
Path({receipt!r}).write_text(json.dumps({{'count': process_resume_count, 'finished': True}}))
print('PROCESS_WORK_FINISHED', flush=True)
"""
            transcript = Path(directory) / "killed-process.txt"
            with transcript.open("w") as captured:
                process = subprocess.Popen(
                    [*command, "exec", "-s", name, "--timeout", "0"],
                    stdin=subprocess.PIPE,
                    stdout=captured,
                    stderr=captured,
                    text=True,
                )
                try:
                    process.stdin.write(code)
                    process.stdin.close()
                    deadline = time.monotonic() + 90
                    while "PROCESS_WORK_STARTED" not in transcript.read_text():
                        assert process.poll() is None, (
                            "CLI exited before submitting test work"
                        )
                        assert time.monotonic() < deadline, (
                            "CLI did not reach test marker"
                        )
                        time.sleep(0.1)
                finally:
                    process.kill()
                    process.wait(timeout=10)
            saved = state.store.get(name)
            assert saved.execution is not None
            result = cli(
                "resume", "-s", name, "--timeout", "30", capture_output=True, check=True
            )
            assert result.stdout.count("PROCESS_WORK_STARTED") == 1
            assert result.stdout.count("PROCESS_WORK_FINISHED") == 1
            assert state.store.get(name).execution is None
            assert expected in verify(
                f"from pathlib import Path; print(Path({receipt!r}).read_text())"
            )
            print(
                "PASS: a new CLI process recovered the original work; side effect occurred once.",
                flush=True,
            )

            # Stale credentials at startup must refresh before any submission.
            saved = state.store.get(name)
            saved.token = "deliberately-invalid"
            state.store.add(saved)
            assert "STARTUP_REFRESH_OK" in verify("print('STARTUP_REFRESH_OK')")
            assert state.store.get(name).token != "deliberately-invalid"
            print(
                "PASS: stale startup proxy token refreshed on the original assignment.",
                flush=True,
            )
            verify_missing_kernel()

            regular_name = name + "-normal"
            script.write_text(
                "print('NORMAL_RUN_DONE', flush=True)\n", encoding="utf-8"
            )
            try:
                result = CliRunner().invoke(
                    app, ["--config", config, "run", "-s", regular_name, str(script)]
                )
            finally:
                remember(regular_name)
            print(result.output, flush=True)
            if result.exception:
                raise result.exception
            assert "NORMAL_RUN_DONE" in result.output
            assert state.store.get(regular_name) is None
            print("PASS: confirmed normal run still releases its runtime.", flush=True)
        finally:
            # Never touch assignments that existed before this test.
            remember(name)
            remember(name + "-normal")
            for endpoint in owned:
                if endpoint in {a.endpoint for a in state.client.list_assignments()}:
                    state.client.unassign(endpoint)
            active = {a.endpoint for a in state.client.list_assignments()}
            assert not owned.intersection(active), "A test runtime was not released"
            cli("sessions", check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario", action="append", help="Run only selected disconnect scenarios"
    )
    main(parser.parse_args().scenario)
