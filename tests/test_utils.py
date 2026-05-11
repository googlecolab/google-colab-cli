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

import base64
from unittest.mock import MagicMock, patch

from colab_cli.utils import handle_image, print_kitty


@patch("colab_cli.utils.sys.stdout.isatty", return_value=True)
def test_print_kitty_emits_escape_sequence_on_tty(_mock_isatty, capsys):
    print_kitty(b"fake-png-bytes")
    captured = capsys.readouterr()
    assert "_Ga=T,f=100;" in captured.out
    b64 = base64.b64encode(b"fake-png-bytes").decode("ascii")
    assert b64 in captured.out


@patch("colab_cli.utils.sys.stdout.isatty", return_value=False)
def test_print_kitty_silent_when_stdout_not_tty(_mock_isatty, capsys):
    """Kitty graphics escape sequences are useless and visually corrupt the
    output when stdout is redirected (e.g. `colab exec ... > log.txt`,
    `colab exec ... | grep ...`, or any non-Kitty terminal). When stdout is
    not a TTY, print_kitty must not emit anything.
    """
    print_kitty(b"fake-png-bytes")
    captured = capsys.readouterr()
    assert captured.out == "", (
        f"Expected no output when stdout is not a TTY, got: {captured.out!r}"
    )


@patch("colab_cli.utils.tempfile.NamedTemporaryFile")
@patch("colab_cli.utils.print_kitty")
def test_handle_image(mock_print_kitty, mock_tempfile, capsys):
    mock_tmp = MagicMock()
    mock_tmp.name = "/tmp/fake.png"
    mock_tempfile.return_value = mock_tmp

    handle_image(base64.b64encode(b"test").decode("ascii"), "image/png")

    mock_print_kitty.assert_called_once_with(b"test")
    mock_tmp.write.assert_called_once_with(b"test")
    mock_tmp.close.assert_called_once()

    captured = capsys.readouterr()
    assert "/tmp/fake.png" in captured.out
