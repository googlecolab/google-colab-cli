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

"""`colab ssh` interactive shell: lands in /content and forwards --identity.

An SSH login lands in root's home (/root); Colab users expect /content (where
notebooks + uploads live). `_run_interactive_ssh` forces a PTY and runs a remote
command that `cd`s to /content before exec'ing the login shell, and threads
`--identity` through to both the outer `ssh -i` and the inner proxy command.
"""

from unittest.mock import MagicMock

from colab_cli.commands import ssh as ssh_module
import pytest


def _make_session(
    name: str = "s1",
    url: str = "https://abc.colab.googleusercontent.com",
    token: str = "TOK",
    endpoint: str = "ep",
):
    s = MagicMock()
    s.name = name
    s.url = url
    s.token = token
    s.endpoint = endpoint
    return s


def test_default_remote_dir_is_content():
    assert ssh_module._DEFAULT_REMOTE_DIR == "/content"


@pytest.mark.parametrize(
    "identity",
    [None, "/home/u/.ssh/id_ed25519"],
    ids=["no-identity", "with-identity"],
)
def test_interactive_ssh_builds_args(mocker, identity):
    call = mocker.patch("subprocess.call", return_value=0)
    rc = ssh_module._run_interactive_ssh(_make_session(), identity)
    assert rc == 0

    args = call.call_args.args[0]
    assert "-t" in args  # PTY forced so the exec'd shell is interactive
    assert ssh_module._SSH_HOST in args
    # the host must precede the remote command (which is the final element)
    assert args.index(ssh_module._SSH_HOST) < len(args) - 1

    remote = args[-1]
    assert "cd /content" in remote
    assert "exec" in remote  # execs a shell after the cd
    # a missing /content must not abort the shell (stderr suppressed).
    assert "2>/dev/null" in remote

    joined = " ".join(args)
    if identity:
        assert "-i" in args  # outer ssh identity
        assert "--identity" in joined  # inner proxy-mode identity
    else:
        assert "-i" not in args
        assert "--identity" not in joined
