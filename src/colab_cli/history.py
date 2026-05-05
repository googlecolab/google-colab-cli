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

import datetime
import json
import os
from typing import Any, Dict, List


class HistoryLogger:
    def __init__(self, log_dir: str = "~/.config/colab-cli/history"):
        self.log_dir = os.path.expanduser(log_dir)
        os.makedirs(self.log_dir, exist_ok=True)

    def _get_log_path(self, session_name: str) -> str:
        return os.path.join(self.log_dir, f"{session_name}.jsonl")

    def log_event(self, session_name: str, event_type: str, data: Dict[str, Any]):
        """
        Appends a structured event to the session's history file.

        event_types:
          - session_created
          - session_terminated
          - execution (code + outputs)
          - input_requested (stdin prompts/replies)
          - file_operation (ls, rm, upload, download)
          - automation (auth, install, drivemount)
        """
        log_path = self._get_log_path(session_name)
        event = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "event_type": event_type,
            **data,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

    def list_sessions(self) -> List[str]:
        if not os.path.exists(self.log_dir):
            return []
        return [f[:-6] for f in os.listdir(self.log_dir) if f.endswith(".jsonl")]

    def get_history(self, session_name: str) -> List[Dict[str, Any]]:
        log_path = self._get_log_path(session_name)
        if not os.path.exists(log_path):
            return []

        history = []
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    history.append(json.loads(line))
        return history
