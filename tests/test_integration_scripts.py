# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from pathlib import Path
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_SCRIPTS = (
    REPO_ROOT / "integration/repro_runtime_token_refresh/test.sh",
    REPO_ROOT / "integration/repro_console_reconnect/test.sh",
    REPO_ROOT / "integration/repro_console_flow_control/test.sh",
)


def _write_fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "uv.log"
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        """#!/bin/bash
set -eu
printf '%s\\n' "$*" >> "$FAKE_UV_LOG"

if [ "$1" = run ] && [ "$2" = colab ]; then
    shift 2
    config=""
    previous=""
    for arg in "$@"; do
        if [ "$previous" = --config ]; then
            config="$arg"
        fi
        previous="$arg"
    done

    case " $* " in
        *" sessions "*)
            if [ "$FAKE_UV_MODE" = sessions-fail ]; then
                exit 7
            fi
            exit 0
            ;;
        *" new "*)
            printf 'new\\n' >> "$FAKE_UV_LOG"
            mkdir -p "$(dirname "$config")"
            printf '{"fake-session":{"endpoint":"test-endpoint","accelerator":"NONE"}}\\n' > "$config"
            exit 0
            ;;
        *" stop "*)
            printf 'stop\\n' >> "$FAKE_UV_LOG"
            exit 0
            ;;
    esac
fi

if [ "$1" = run ] && [ "$2" = python ]; then
    count_file="$FAKE_UV_PYTHON_COUNT"
    count=0
    if [ -f "$count_file" ]; then
        count=$(cat "$count_file")
    fi
    count=$((count + 1))
    printf '%s' "$count" > "$count_file"
    if [ "$FAKE_UV_MODE" = endpoint-fail ] && [ "$count" -eq 1 ]; then
        exit 8
    fi
    if [ "$#" -ge 5 ] && [ "$4" = oauth2 ]; then
        printf 'unassign %s\\n' "$5" >> "$FAKE_UV_LOG"
        exit 0
    fi
    printf 'test-endpoint\\n'
    exit 0
fi

exit 99
"""
    )
    fake_uv.chmod(0o755)
    return bin_dir, log_path


def _run_script(
    script: Path, tmp_path: Path, mode: str
) -> tuple[subprocess.CompletedProcess, str]:
    bin_dir, log_path = _write_fake_uv(tmp_path)
    fake_home = tmp_path / "home"
    token = fake_home / ".config/colab-cli/token.json"
    token.parent.mkdir(parents=True)
    token.write_text("{}")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "HOME": str(fake_home),
            "FAKE_UV_LOG": str(log_path),
            "FAKE_UV_MODE": mode,
            "FAKE_UV_PYTHON_COUNT": str(tmp_path / "python-count"),
        }
    )
    result = subprocess.run(
        ["bash", str(script)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return result, log_path.read_text() if log_path.exists() else ""


@pytest.mark.parametrize("script", LIVE_SCRIPTS, ids=lambda path: path.parent.name)
def test_live_script_cleans_assignment_when_endpoint_read_fails(script, tmp_path):
    result, log = _run_script(script, tmp_path, "endpoint-fail")

    assert result.returncode != 0
    assert "new" in log
    assert "stop" in log
    assert "unassign test-endpoint" in log


@pytest.mark.parametrize("script", LIVE_SCRIPTS, ids=lambda path: path.parent.name)
def test_live_script_stops_before_allocating_when_assignment_snapshot_fails(
    script, tmp_path
):
    result, log = _run_script(script, tmp_path, "sessions-fail")

    assert result.returncode != 0
    assert "new" not in log
