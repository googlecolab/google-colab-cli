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

"""Native Windows live SSH check; allocates and releases one CPU runtime.

Run from the repository with `uv run python integration/repro_windows_ssh/test.py`.
Exit 77 means cached OAuth credentials are missing; no runtime is allocated.
"""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

from colab_cli.auth import TOKEN_CONFIG_PATH
from colab_cli.commands.ssh import _proxy_command, _ssh_base_args
from colab_cli.common import State


def colab(*args, check=True):
    return subprocess.run(
        [sys.executable, "-m", "colab_cli.cli", "--auth=oauth2", *args],
        stdin=subprocess.DEVNULL,
        check=check,
        timeout=180,
    )


def main():
    if sys.platform != "win32":
        print("SKIP: requires native Windows and Windows OpenSSH.")
        return 77
    if not Path(TOKEN_CONFIG_PATH).is_file():
        print("SKIP: no cached OAuth2 credentials. First run:")
        print("  uv run colab --auth=oauth2 sessions")
        return 77
    if not all(shutil.which(tool) for tool in ("ssh", "ssh-keygen")):
        print("SKIP: install the Windows OpenSSH Client optional feature.")
        return 77

    # Check authentication before creating a billable resource.
    colab("sessions")
    name = "windows-ssh-" + uuid.uuid4().hex[:8]
    state = State()
    endpoint = None
    try:
        with tempfile.TemporaryDirectory(prefix="colab ssh ") as directory:
            key = str(Path(directory) / "id_ed25519")
            subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key],
                check=True,
                timeout=30,
            )
            colab("new", "-s", name)
            session = state.store.get(name)
            if session is None:
                raise RuntimeError("Created session missing from local state")
            endpoint = session.endpoint
            print(f"Created endpoint: {endpoint}", flush=True)
            proxy = _proxy_command(session, key) + " --rm"
            args = _ssh_base_args(proxy, key)
            args.extend(
                [
                    "-F",
                    "NUL",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=60",
                    "root@colab-runtime",
                    "printf 'windows-ssh-ok\\n'",
                ]
            )
            result = subprocess.run(
                args,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=120,
            )
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            result.check_returncode()
            if "windows-ssh-ok" not in result.stdout.splitlines():
                raise RuntimeError("Remote command marker missing")
    finally:
        # The bridge's --rm path should stop the runtime; also clean up on
        # connection failure or timeout, using the recorded endpoint if needed.
        saved = state.store.get(name)
        endpoint = endpoint or (saved.endpoint if saved else None)
        try:
            colab("stop", "-s", name, check=False)
            if endpoint:
                active = {a.endpoint for a in state.client.list_assignments()}
                if endpoint in active:
                    state.client.unassign(endpoint)
                remaining = {a.endpoint for a in state.client.list_assignments()}
                if endpoint in remaining:
                    raise RuntimeError(f"Runtime cleanup failed: {endpoint}")
        finally:
            colab("sessions")
    print("PASS: native Windows SSH remote execution and runtime cleanup.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
