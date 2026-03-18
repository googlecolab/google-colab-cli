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

import pytest
import sys
from unittest.mock import patch
from colab_cli.cli import main


def test_cli_pay(mock_common_state):
    with patch.object(sys, "argv", ["colab", "pay"]):
        with patch("webbrowser.open") as mock_open:
            with pytest.raises(SystemExit) as error:
                main()

            assert error.value.code == 0
            mock_open.assert_called_once_with(
                "https://colab.research.google.com/signup"
            )
