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

import sys
import pytest
from unittest.mock import patch
from colab_cli.cli import main


def test_cli_log_list():
    with patch.object(sys, "argv", ["colab", "log"]):
        with patch("colab_cli.commands.utility.state") as mock_state:
            mock_state.history.list_sessions.return_value = ["s1"]

            with patch("colab_cli.commands.utility.typer.echo") as mock_print:
                with pytest.raises(SystemExit) as exitinfo:
                    main()
                assert exitinfo.value.code == 0
                mock_print.assert_any_call("  s1")


def test_cli_log_show():
    with patch.object(sys, "argv", ["colab", "log", "-s", "s1"]):
        with patch("colab_cli.commands.utility.state") as mock_state:
            mock_state.history.get_history.return_value = [
                {
                    "timestamp": "2026-03-23T12:00:00.000000+00:00",
                    "event_type": "execution",
                    "code": "print(1)",
                }
            ]

            with patch("colab_cli.commands.utility.typer.echo") as mock_print:
                with pytest.raises(SystemExit) as exitinfo:
                    main()
                assert exitinfo.value.code == 0
                # Check for EXEC: print(1)
                found = any(
                    "EXEC: print(1)" in str(call) for call in mock_print.call_args_list
                )
                assert found


def test_cli_log_show_filter():
    with patch.object(
        sys, "argv", ["colab", "log", "-s", "s1", "-t", "file_operation"]
    ):
        with patch("colab_cli.commands.utility.state") as mock_state:
            mock_state.history.get_history.return_value = [
                {
                    "timestamp": "2026-03-23T12:00:00.000000+00:00",
                    "event_type": "execution",
                    "code": "print(1)",
                },
                {
                    "timestamp": "2026-03-23T12:01:00.000000+00:00",
                    "event_type": "file_operation",
                    "op": "ls",
                    "path": "content",
                },
            ]

            with patch("colab_cli.commands.utility.typer.echo") as mock_print:
                with pytest.raises(SystemExit) as exitinfo:
                    main()
                assert exitinfo.value.code == 0
                # Should see FILE: ls content but NOT EXEC: print(1)
                printed = [str(call) for call in mock_print.call_args_list]
                assert any("FILE: ls" in p for p in printed)
                assert not any("EXEC: print(1)" in p for p in printed)


def test_cli_log_export():
    with patch.object(sys, "argv", ["colab", "log", "-s", "s1", "-o", "test.ipynb"]):
        with patch("colab_cli.commands.utility.state") as mock_state:
            with patch("colab_cli.converter.export_history") as mock_export:
                mock_state.history.get_history.return_value = [
                    {"event_type": "execution"}
                ]

                with pytest.raises(SystemExit) as exitinfo:
                    main()
                assert exitinfo.value.code == 0
                mock_export.assert_called_once()
