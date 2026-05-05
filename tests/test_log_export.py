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

import json
import os
import shutil
import sys
import tempfile
import unittest
import pytest
from unittest.mock import patch
from colab_cli.cli import main
from colab_cli.history import HistoryLogger


class TestLogExport(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.history_dir = os.path.join(self.temp_dir, "history")
        os.makedirs(self.history_dir)
        self.session_name = "test-export"
        self.log_path = os.path.join(self.history_dir, f"{self.session_name}.jsonl")

        # Sample events
        events = [
            {
                "timestamp": "2026-03-23T12:00:00.000000+00:00",
                "event_type": "session_created",
                "endpoint": "ep1",
                "accelerator": "NONE",
            },
            {
                "timestamp": "2026-03-23T12:01:00.000000+00:00",
                "event_type": "execution",
                "code": "print(1)",
                "outputs": [{"text": "1\n"}],
            },
            {
                "timestamp": "2026-03-23T12:02:00.000000+00:00",
                "event_type": "file_operation",
                "op": "ls",
                "path": "content",
            },
        ]
        with open(self.log_path, "w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

    def tearDown(self):
        shutil.rmtree(self.temp_dir)
        if os.path.exists(f"{self.session_name}.ipynb"):
            os.remove(f"{self.session_name}.ipynb")

    @patch("colab_cli.commands.utility.state")
    def test_log_export(self, mock_state):
        # Setup mocks to return our test events

        # Real HistoryLogger to read our temp log
        real_history = HistoryLogger(log_dir=self.history_dir)
        mock_state.history.get_history.side_effect = real_history.get_history

        with patch.object(
            sys,
            "argv",
            [
                "colab",
                "log",
                "-s",
                self.session_name,
                "-o",
                f"{self.session_name}.ipynb",
            ],
        ):
            with pytest.raises(SystemExit) as exitinfo:
                main()
            self.assertEqual(exitinfo.value.code, 0)

        output_file = f"{self.session_name}.ipynb"
        self.assertTrue(os.path.exists(output_file))

        with open(output_file, "r") as f:
            nb = json.load(f)
            # Should have title, session_created md, execution code, and file_op md cells
            # Total cells: 4 (title, session_created, execution, file_op)
            self.assertEqual(len(nb["cells"]), 4)
            self.assertEqual(nb["cells"][2]["cell_type"], "code")
            source = nb["cells"][2]["source"]
            if isinstance(source, list):
                source = "".join(source)
            self.assertEqual(source, "print(1)")
            output_text = nb["cells"][2]["outputs"][0]["text"]
            if isinstance(output_text, list):
                output_text = "".join(output_text)
            self.assertEqual(output_text, "1\n")


if __name__ == "__main__":
    unittest.main()
