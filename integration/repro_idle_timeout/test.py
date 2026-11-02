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

"""Live CPU runtime verification for `--timeout` idle-limit behavior (#183 / #185).

Covers all verification cases from PR #185 across `jupyter-kernel-client`
0.8.0 (git fork), 0.9.0, and 1.0.2:

| Case | --timeout | Expected Result |
|---|---|---|
| `colab run`, prints every 5 s for 60 s | default (30) | Completes on 0.8.0, 0.9.0, 1.0.2; client CPU ~0.0-0.1% |
| `colab run`, silent for 45 s | default (30) | `TimeoutError: Timeout waiting for output: no message from the kernel for 30s. Use --timeout to allow longer silences.`, exit code 1, session stopped |
| `colab run`, silent for 45 s | 60 | Completes, exit code 0, session stopped |
| `colab run`, silent for 45 s | 0 | Completes, exit code 0, session stopped |
| `colab exec` on existing session, prints every 5 s for 60 s | default (30) | Completes at ~0.0% CPU |
| `colab exec` on existing session, silent for 45 s | default (30) | `TimeoutError`, exit code 1 |
| Ctrl-C 40 s into a silent `colab run` | 600 | Exits quickly after SIGINT with exit code 130, session stopped |

Run from the repository root:
    uv run python integration/repro_idle_timeout/test.py
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from colab_cli.auth import TOKEN_CONFIG_PATH

REPO_ROOT = Path(__file__).resolve().parents[2]

EVERY_5S_FOR_60S_CODE = """\
import time
for i in range(12):
    print(f"step {i + 1}/12", flush=True)
    time.sleep(5)
print("finished", flush=True)
"""

SILENT_45S_CODE = """\
import time
time.sleep(45)
print("finished after 45s silence", flush=True)
"""

SILENT_120S_CODE = """\
import time
time.sleep(120)
print("finished after 120s silence", flush=True)
"""

JKC_VERSIONS = {
    "0.8.0": ["uv", "sync", "--quiet"],
    "0.9.0": ["uv", "pip", "install", "--quiet", "jupyter-kernel-client==0.9.0"],
    "1.0.2": ["uv", "pip", "install", "--quiet", "jupyter-kernel-client==1.0.2"],
}


def detect_auth_flag() -> str | None:
    """Return '--auth=oauth2' or '--auth=adc' if usable, else None."""
    if Path(TOKEN_CONFIG_PATH).is_file():
        res = subprocess.run(
            [sys.executable, "-m", "colab_cli.cli", "--auth=oauth2", "sessions"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode == 0:
            return "--auth=oauth2"

    res = subprocess.run(
        [sys.executable, "-m", "colab_cli.cli", "--auth=adc", "sessions"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if res.returncode == 0:
        return "--auth=adc"
    return None


def install_jkc_version(version: str) -> str:
    """Install the requested jupyter-kernel-client version into the active venv."""
    cmd = JKC_VERSIONS[version]
    subprocess.run(cmd, cwd=REPO_ROOT, check=True, timeout=120)
    out = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import importlib.metadata; print(importlib.metadata.version('jupyter-kernel-client'))",
        ],
        text=True,
    ).strip()
    if out != version:
        raise RuntimeError(f"Expected jupyter-kernel-client {version}, got {out!r}")
    return out


def _read_proc_cpu_seconds(pid: int) -> float | None:
    """Read (utime + stime) in seconds for `pid` from /proc/<pid>/stat on Linux."""
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.exists():
        return None
    try:
        data = stat_path.read_text()
        rparen = data.rfind(")")
        fields = data[rparen + 2 :].split()
        utime_ticks = int(fields[11])
        stime_ticks = int(fields[12])
        clk_tck = os.sysconf("SC_CLK_TCK")
        return (utime_ticks + stime_ticks) / clk_tck
    except Exception:
        return None


def run_colab_measured(
    auth_flag: str,
    config_path: Path,
    args: list[str],
    *,
    send_sigint_after_ready_sec: float | None = None,
    timeout_sec: float = 240.0,
) -> dict:
    """Run `colab` in a subprocess, measuring CPU usage during execution."""
    cmd = [
        sys.executable,
        "-m",
        "colab_cli.cli",
        auth_flag,
        "--config",
        str(config_path),
        *args,
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    ready_event = threading.Event()
    exec_cpu_start: list[float | None] = [None]
    exec_wall_start: list[float | None] = [None]

    def _record_exec_start():
        if not ready_event.is_set():
            exec_cpu_start[0] = _read_proc_cpu_seconds(proc.pid)
            exec_wall_start[0] = time.monotonic()
            ready_event.set()

    def _read_stdout():
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_lines.append(line)
            _record_exec_start()

    def _read_stderr():
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            if "Session READY" in line or "Executing" in line:
                _record_exec_start()

    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    # For `colab exec`, execution begins right after startup; if nothing has
    # been printed after 2s, start the CPU measurement window.
    if send_sigint_after_ready_sec is None:
        if ready_event.wait(timeout=2.0) is False:
            _record_exec_start()

    sigint_sent_at: float | None = None
    if send_sigint_after_ready_sec is not None:
        if not ready_event.wait(timeout=120.0):
            proc.kill()
            raise RuntimeError(
                "Timed out waiting for 'Session READY' before sending SIGINT.\n"
                f"stderr:\n{''.join(stderr_lines)}"
            )
        time.sleep(send_sigint_after_ready_sec)
        sigint_sent_at = time.monotonic()
        proc.send_signal(signal.SIGINT)

    # Wait for process using wait4 on POSIX so we get exact child rusage.
    deadline = time.monotonic() + timeout_sec
    rusage = None
    if hasattr(os, "wait4"):
        while True:
            pid_done, status, rusage = os.wait4(proc.pid, os.WNOHANG)
            if pid_done == proc.pid:
                if os.WIFEXITED(status):
                    returncode = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    returncode = 128 + os.WTERMSIG(status)
                else:
                    returncode = 1
                proc.returncode = returncode
                break
            if time.monotonic() > deadline:
                proc.kill()
                raise TimeoutError(f"Subprocess timed out after {timeout_sec}s: {cmd}")
            time.sleep(0.05)
    else:
        returncode = proc.wait(timeout=timeout_sec)

    finished_at = time.monotonic()
    t_out.join(timeout=5.0)
    t_err.join(timeout=5.0)

    total_cpu_sec = (rusage.ru_utime + rusage.ru_stime) if rusage is not None else 0.0
    if exec_cpu_start[0] is not None and exec_wall_start[0] is not None:
        exec_wall = max(0.001, finished_at - exec_wall_start[0])
        exec_cpu = max(0.0, total_cpu_sec - exec_cpu_start[0])
        cpu_pct = 100.0 * exec_cpu / exec_wall
    else:
        cpu_pct = 0.0

    return {
        "returncode": returncode,
        "stdout": "".join(stdout_lines),
        "stderr": "".join(stderr_lines),
        "cpu_pct": cpu_pct,
        "sigint_exit_latency": (
            (finished_at - sigint_sent_at) if sigint_sent_at is not None else None
        ),
    }


def assert_no_active_sessions(auth_flag: str, config_path: Path) -> None:
    """Verify no sessions remain on the server (and unassign any orphans)."""
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "colab_cli.cli",
            auth_flag,
            "--config",
            str(config_path),
            "sessions",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = res.stdout + res.stderr
    if "No active sessions found on server." not in output:
        raise AssertionError(
            f"Expected all sessions to be stopped, but `colab sessions` reported:\n{output}"
        )


def cleanup_all_server_assignments(auth_flag: str) -> None:
    """Best-effort cleanup of any remaining server-side assignments."""
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from colab_cli.auth import AuthProvider; "
                "from colab_cli.common import state; "
                f"state.auth_provider = AuthProvider({auth_flag.split('=')[1]!r}); "
                "[state.client.unassign(a.endpoint) for a in state.client.list_assignments()]"
            ),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--versions",
        default="0.8.0,0.9.0,1.0.2",
        help="Comma-separated jupyter-kernel-client versions for Case 1 (default: 0.8.0,0.9.0,1.0.2)",
    )
    args = parser.parse_args()
    versions = [v.strip() for v in args.versions.split(",") if v.strip()]

    auth_flag = detect_auth_flag()
    if not auth_flag:
        print("SKIP: no usable OAuth2 or ADC credentials found.")
        return 77
    print(f"[*] Using {auth_flag}", flush=True)

    with tempfile.TemporaryDirectory(prefix="colab-timeout-test-") as tmp:
        tmp_dir = Path(tmp)
        config_path = tmp_dir / "sessions.json"
        every5_script = tmp_dir / "every5.py"
        silent45_script = tmp_dir / "silent45.py"
        silent120_script = tmp_dir / "silent120.py"

        every5_script.write_text(EVERY_5S_FOR_60S_CODE)
        silent45_script.write_text(SILENT_45S_CODE)
        silent120_script.write_text(SILENT_120S_CODE)

        try:
            # ------------------------------------------------------------------
            # Case 1: colab run, prints every 5 s for 60 s, default timeout (30)
            #         Tested across jupyter-kernel-client 0.8.0, 0.9.0, 1.0.2.
            # ------------------------------------------------------------------
            for ver in versions:
                installed = install_jkc_version(ver)
                print(
                    f"\n=== Case 1 ({installed}): colab run, prints every 5 s for 60 s (default --timeout 30) ===",
                    flush=True,
                )
                res = run_colab_measured(
                    auth_flag, config_path, ["run", str(every5_script)]
                )
                print(
                    f"  exit={res['returncode']}, client CPU={res['cpu_pct']:.2f}%",
                    flush=True,
                )
                assert res["returncode"] == 0, (
                    f"Case 1 ({installed}) failed:\nstdout:\n{res['stdout']}\nstderr:\n{res['stderr']}"
                )
                assert "step 12/12" in res["stdout"] and "finished" in res["stdout"], (
                    f"Missing expected output:\n{res['stdout']}"
                )
                assert res["cpu_pct"] < 2.0, (
                    f"Expected idle client CPU (<2%), got {res['cpu_pct']:.2f}%"
                )
                assert_no_active_sessions(auth_flag, config_path)

            # Use 0.9.0 (the PyPI pin in pyproject.toml) for the remaining cases,
            # or the last requested version if 0.9.0 wasn't in `versions`.
            target_ver = "0.9.0" if "0.9.0" in JKC_VERSIONS else versions[-1]
            install_jkc_version(target_ver)

            # ------------------------------------------------------------------
            # Case 2: colab run, silent for 45 s, default timeout (30)
            # ------------------------------------------------------------------
            print(
                "\n=== Case 2: colab run, silent for 45 s (default --timeout 30) ===",
                flush=True,
            )
            res = run_colab_measured(
                auth_flag, config_path, ["run", str(silent45_script)]
            )
            combined = res["stdout"] + res["stderr"]
            print(f"  exit={res['returncode']}", flush=True)
            assert res["returncode"] == 1, f"Expected exit 1, got {res['returncode']}"
            expected_msg = (
                "TimeoutError: Timeout waiting for output: no message from "
                "the kernel for 30s. Use --timeout to allow longer silences."
            )
            assert expected_msg in combined, (
                f"Expected {expected_msg!r} in output, got:\n{combined}"
            )
            assert_no_active_sessions(auth_flag, config_path)

            # ------------------------------------------------------------------
            # Case 3: colab run, silent for 45 s, --timeout 60
            # ------------------------------------------------------------------
            print(
                "\n=== Case 3: colab run, silent for 45 s (--timeout 60) ===",
                flush=True,
            )
            res = run_colab_measured(
                auth_flag,
                config_path,
                ["run", "--timeout", "60", str(silent45_script)],
            )
            print(f"  exit={res['returncode']}", flush=True)
            assert res["returncode"] == 0, (
                f"Case 3 failed:\nstdout:\n{res['stdout']}\nstderr:\n{res['stderr']}"
            )
            assert "finished after 45s silence" in res["stdout"]
            assert_no_active_sessions(auth_flag, config_path)

            # ------------------------------------------------------------------
            # Case 4: colab run, silent for 45 s, --timeout 0
            # ------------------------------------------------------------------
            print(
                "\n=== Case 4: colab run, silent for 45 s (--timeout 0) ===",
                flush=True,
            )
            res = run_colab_measured(
                auth_flag,
                config_path,
                ["run", "--timeout", "0", str(silent45_script)],
            )
            print(f"  exit={res['returncode']}", flush=True)
            assert res["returncode"] == 0, (
                f"Case 4 failed:\nstdout:\n{res['stdout']}\nstderr:\n{res['stderr']}"
            )
            assert "finished after 45s silence" in res["stdout"]
            assert_no_active_sessions(auth_flag, config_path)

            # ------------------------------------------------------------------
            # Cases 5 & 6: colab exec on an existing session
            # ------------------------------------------------------------------
            session_name = f"repro-timeout-{uuid.uuid4().hex[:6]}"
            print(
                f"\n[*] Creating session {session_name} for `colab exec` cases...",
                flush=True,
            )
            new_res = run_colab_measured(
                auth_flag, config_path, ["new", "-s", session_name]
            )
            assert new_res["returncode"] == 0, (
                f"colab new failed:\n{new_res['stdout']}\n{new_res['stderr']}"
            )
            try:
                print(
                    "\n=== Case 5: colab exec on existing session, prints every 5 s for 60 s (default --timeout 30) ===",
                    flush=True,
                )
                res = run_colab_measured(
                    auth_flag,
                    config_path,
                    ["exec", "-s", session_name, "-f", str(every5_script)],
                )
                print(
                    f"  exit={res['returncode']}, client CPU={res['cpu_pct']:.2f}%",
                    flush=True,
                )
                assert res["returncode"] == 0, (
                    f"Case 5 failed:\nstdout:\n{res['stdout']}\nstderr:\n{res['stderr']}"
                )
                assert "step 12/12" in res["stdout"] and "finished" in res["stdout"]
                assert res["cpu_pct"] < 2.0, (
                    f"Expected idle client CPU (<2%), got {res['cpu_pct']:.2f}%"
                )

                print(
                    "\n=== Case 6: colab exec on existing session, silent for 45 s (default --timeout 30) ===",
                    flush=True,
                )
                res = run_colab_measured(
                    auth_flag,
                    config_path,
                    ["exec", "-s", session_name, "-f", str(silent45_script)],
                )
                combined = res["stdout"] + res["stderr"]
                print(f"  exit={res['returncode']}", flush=True)
                assert res["returncode"] == 1, (
                    f"Expected exit 1, got {res['returncode']}"
                )
                assert "TimeoutError" in combined, (
                    f"Expected TimeoutError in output, got:\n{combined}"
                )
            finally:
                run_colab_measured(auth_flag, config_path, ["stop", "-s", session_name])
            assert_no_active_sessions(auth_flag, config_path)

            # ------------------------------------------------------------------
            # Case 7: Ctrl-C 40 s into a silent colab run (--timeout 600)
            # ------------------------------------------------------------------
            print(
                "\n=== Case 7: Ctrl-C 40 s into a silent colab run (--timeout 600) ===",
                flush=True,
            )
            res = run_colab_measured(
                auth_flag,
                config_path,
                ["run", "--timeout", "600", str(silent120_script)],
                send_sigint_after_ready_sec=40.0,
            )
            latency = res["sigint_exit_latency"] or 0.0
            print(
                f"  exit={res['returncode']}, exited {latency:.2f}s after SIGINT",
                flush=True,
            )
            assert res["returncode"] == 130, (
                f"Expected exit code 130 on SIGINT, got {res['returncode']}\n"
                f"stdout:\n{res['stdout']}\nstderr:\n{res['stderr']}"
            )
            assert latency < 15.0, (
                f"Expected quick exit after SIGINT (including VM teardown), took {latency:.2f}s"
            )
            assert_no_active_sessions(auth_flag, config_path)

            print("\n[SUCCESS] All 7 timeout verification cases passed!", flush=True)
            return 0
        finally:
            cleanup_all_server_assignments(auth_flag)
            # Always restore .venv to the repo lockfile (0.8.0 fork).
            subprocess.run(
                ["uv", "sync", "--quiet"], cwd=REPO_ROOT, check=False, timeout=120
            )


if __name__ == "__main__":
    raise SystemExit(main())
