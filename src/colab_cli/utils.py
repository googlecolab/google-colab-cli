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
import html2text
import logging
import sys
import tempfile

from typing import Optional, Union

from rich.markdown import Markdown
from rich.text import Text


class RuntimeProxyError(Exception):
    """The runtime tunnel rejected an expired or invalid proxy token."""

    def __init__(self, status_code: int, response_body: bytes = b""):
        super().__init__(f"Runtime proxy rejected credentials (HTTP {status_code})")
        self.status_code = status_code
        self.response_body = response_body


def get_status_code(e: Exception) -> Optional[int]:
    """Safely extracts status code from various exception types."""
    if hasattr(e, "response") and e.response is not None:
        if hasattr(e.response, "status_code"):
            return e.response.status_code
    if hasattr(e, "status_code"):
        return e.status_code
    return None


def is_runtime_proxy_error(e: Exception) -> bool:
    """Returns whether a connection failed due to proxy authentication.

    Prefer structured status codes. A narrow text fallback is retained for
    jupyter-kernel-client and websocket-client versions that only expose the
    HTTP handshake status in their exception message.
    """
    if isinstance(e, RuntimeProxyError):
        return True
    if isinstance(e, FileNotFoundError):
        return False
    code = get_status_code(e)
    if code in (401, 404):
        return True
    message = str(e).lower()
    return any(
        marker in message
        for marker in (
            "handshake status 401",
            "handshake status 404",
            "http 401",
            "http 404",
            "401 unauthorized",
            "404 not found",
        )
    )


def print_kitty(image_bytes: bytes):
    """
    Outputs an image using the Kitty Graphics Protocol.
    Expects PNG bytes.

    No-op when stdout is not a TTY: the escape sequence is meaningless to a
    file/pipe and visually corrupts captured output (e.g. when piping
    `colab exec` into a shell tool, redirecting to a log file, or running
    under non-Kitty terminals). Callers still get the image via
    `handle_image`'s file-write path.
    """
    if not sys.stdout.isatty():
        return
    try:
        b64_data = base64.b64encode(image_bytes).decode("ascii")
        sys.stdout.write("\n\033_Ga=T,f=100;")
        sys.stdout.write(b64_data)
        sys.stdout.write("\033\\\n")
        sys.stdout.flush()
    except Exception:
        logging.exception("Kitty rendering failed")


def render_display_data(data: dict) -> Union[Markdown, Text, None]:
    """Extract the best text representation from a display_data dict.

    Priority: text/markdown > text/html (via html2text) > text/plain.
    Returns a Rich renderable (Markdown or Text) or None when no text mime
    type is present.  Callers can pass the result directly to Console.print().
    """
    if "text/markdown" in data:
        return Markdown(data["text/markdown"])
    if "text/html" in data:
        return Markdown(html2text.html2text(data["text/html"]))
    if "text/plain" in data:
        return Text.from_ansi(data["text/plain"])
    return None


def handle_image(image_b64: str, mime_type: str = "image/png", target_path: str = None):
    image_bytes = base64.b64decode(image_b64)
    # Print inline using Kitty protocol
    print_kitty(image_bytes)

    if target_path:
        # If a target path is specified, save it there
        with open(target_path, "wb") as f:
            f.write(image_bytes)
        print(f"\n[Image saved to: {target_path}]")
    else:
        # Save to temp file as fallback
        ext = mime_type.split("/")[-1]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
        tmp.write(image_bytes)
        tmp.close()
        print(f"\n[Image saved to: {tmp.name}]")
