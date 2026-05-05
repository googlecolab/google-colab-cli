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

"""Tests for `colab url` (b/509662345): print a browser URL that connects
the Colab frontend to an existing colab-cli session.

URL format (validated empirically against google3, not via Duckie which
incorrectly claimed `dbu` was unparsed):

    https://<host>/notebooks/empty.ipynb?dbu=<urlencoded-/tun/m/endpoint>

Authoritative sources in google3:
  - googledata/experiments/colab/features/development_flags.gcl:589 defines
    the mendel param `datalab_backend_url` with `flag_name = 'dbu'` and the
    docstring sample `https://colab.research.google.com/?dbu=/tun/m/m-s-abc`.
  - research/colab/frontend/common/base/experiment_flags.ts:439 binds
    `DATALAB_BACKEND_URL: flagFetcher.string(developmentFlags.DBU)`.
  - research/colab/frontend/common/model/traits.ts:359 consumes the flag and
    enforces same-origin via `new URL(value, window.location.origin)` and
    `result.origin === window.location.origin`.
  - research/colab/frontend/external/notebooks/empty.ipynb is the real
    blank-notebook route; landing on it gives the user a usable surface
    once the kernel attaches to the existing VM.

Note: `dbu` is a mendel development flag, so URL-overriding it is gated to
sandbox users and Googlers. External users would need the hash-form
`#datalabBackendUrl=...`; we don't support that today (file an issue if
you need it).
"""

from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, quote, urlparse

from typer.testing import CliRunner

from colab_cli.cli import app

runner = CliRunner()


def _make_session(name: str = "s1", endpoint: str = "abc123def"):
    s = MagicMock()
    s.name = name
    s.endpoint = endpoint
    return s


def _parse_url_output(output: str) -> str:
    """Pull the single URL line out of `colab url` output."""
    candidates = [line.strip() for line in output.splitlines() if "dbu=" in line]
    assert len(candidates) == 1, (
        f"Expected exactly one URL line containing 'dbu=', got {candidates!r}"
    )
    return candidates[0]


def test_url_explicit_session(mock_common_state):
    """`colab url -s NAME` prints the connect URL for that session.

    Format: ``https://<host>/notebooks/empty.ipynb?dbu=<urlencoded path>``.
    The path must land on `empty.ipynb` so the user sees a usable notebook
    UI; the `dbu` query param tells the frontend to skip /tun/m/assign and
    attach to our existing endpoint.
    """
    s = _make_session(name="my-sess", endpoint="ep-XYZ")
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "my-sess"

    result = runner.invoke(app, ["url", "-s", "my-sess"])

    assert result.exit_code == 0, result.output
    url = _parse_url_output(result.output)

    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "colab.research.google.com"
    assert parsed.path == "/notebooks/empty.ipynb"

    # `dbu` must be the URL-encoded path `/tun/m/<endpoint>`. We assert on
    # the decoded form rather than the raw encoding to keep the test robust
    # to which characters the encoder happens to escape (e.g. `/` may or
    # may not be escaped depending on `safe=`); what matters is round-trip
    # decoding produces the right backend path.
    qs = parse_qs(parsed.query)
    assert qs.get("dbu") == ["/tun/m/ep-XYZ"]

    # And we DO actually URL-encode the slashes so the value survives any
    # downstream re-parsing that treats the query string non-strictly.
    assert "dbu=%2Ftun%2Fm%2Fep-XYZ" in url


def test_url_resolves_unique_session(mock_common_state):
    """`colab url` (no -s) uses the unique-session resolution path."""
    s = _make_session(name="only-sess", endpoint="solo-EP")
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "only-sess"

    result = runner.invoke(app, ["url"])

    assert result.exit_code == 0, result.output
    url = _parse_url_output(result.output)
    assert "%2Ftun%2Fm%2Fsolo-EP" in url
    # Resolution went through the shared helper, not by hardcoding the name.
    mock_common_state.resolve_session.assert_called_once_with(None)


def test_url_session_not_found(mock_common_state):
    """If the resolved session has no local state, exit non-zero with a clear
    message rather than printing a malformed URL."""
    mock_common_state.resolve_session.return_value = "ghost"
    mock_common_state.store.get.return_value = None

    result = runner.invoke(app, ["url", "-s", "ghost"])

    assert result.exit_code != 0
    assert "ghost" in result.output
    assert "not found" in result.output.lower()


def test_url_custom_host(mock_common_state):
    """`--host` overrides the default frontend host. `dbu` is a path-only
    value (resolved against `window.location.origin` in the frontend), so
    the host swap only affects the page origin, not the embedded backend
    path."""
    s = _make_session(endpoint="ep1")
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"

    result = runner.invoke(
        app, ["url", "-s", "s1", "--host", "https://colab.sandbox.google.com"]
    )

    assert result.exit_code == 0, result.output
    url = _parse_url_output(result.output)
    parsed = urlparse(url)
    assert parsed.netloc == "colab.sandbox.google.com"
    assert parsed.path == "/notebooks/empty.ipynb"
    assert parse_qs(parsed.query).get("dbu") == ["/tun/m/ep1"]


def test_url_host_normalises_trailing_slash(mock_common_state):
    """`--host https://example.com/` (with trailing slash) must not produce
    a double slash before `/notebooks/empty.ipynb`."""
    s = _make_session(endpoint="ep2")
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"

    result = runner.invoke(
        app, ["url", "-s", "s1", "--host", "https://colab.research.google.com/"]
    )

    assert result.exit_code == 0
    assert "https://colab.research.google.com//notebooks/" not in result.output
    assert "https://colab.research.google.com/notebooks/empty.ipynb" in result.output


def test_url_endpoint_with_special_chars_is_encoded(mock_common_state):
    """Endpoints are opaque server-issued IDs but we should not assume their
    character set. Anything outside the unreserved URL set must be escaped
    so the frontend's `new URL(...)` parser sees the intended path."""
    # Intentionally include characters that MUST be escaped if they ever
    # appeared in an endpoint (e.g. `&`, `?`, `#`, space, `=`).
    s = _make_session(endpoint="weird ep&?=#")
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"

    result = runner.invoke(app, ["url", "-s", "s1"])
    assert result.exit_code == 0, result.output
    url = _parse_url_output(result.output)
    parsed = urlparse(url)

    # The encoded endpoint must round-trip via the standard query parser.
    assert parse_qs(parsed.query).get("dbu") == ["/tun/m/weird ep&?=#"]
    # And the raw URL must contain the percent-encoded form (not the literal).
    assert quote("weird ep&?=#", safe="") in url


def test_url_open_flag_launches_browser(mock_common_state):
    """`--open` calls webbrowser.open() with the same URL it printed."""
    s = _make_session(endpoint="ep-OPEN")
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"

    with patch("webbrowser.open") as mock_open:
        result = runner.invoke(app, ["url", "-s", "s1", "--open"])

    assert result.exit_code == 0, result.output
    mock_open.assert_called_once()
    opened_url = mock_open.call_args[0][0]
    assert "dbu=" in opened_url
    assert "%2Ftun%2Fm%2Fep-OPEN" in opened_url
    # And the URL was also printed (so users see what was opened, and
    # piping still works).
    assert opened_url in result.output


def test_url_no_open_by_default(mock_common_state):
    """Default behaviour: print only, do NOT auto-open the browser. This keeps
    the command pipeable (`colab url | xclip`, `colab url | pbcopy`, etc.)."""
    s = _make_session(endpoint="ep3")
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"

    with patch("webbrowser.open") as mock_open:
        result = runner.invoke(app, ["url", "-s", "s1"])

    assert result.exit_code == 0
    mock_open.assert_not_called()


def test_url_output_is_pipeable(mock_common_state):
    """The printed URL line must be machine-parseable: a single line with no
    leading `[colab]` chatter, so `colab url -s s1 | xclip` works.
    """
    s = _make_session(endpoint="ep-PIPE")
    mock_common_state.store.get.return_value = s
    mock_common_state.resolve_session.return_value = "s1"

    result = runner.invoke(app, ["url", "-s", "s1"])

    assert result.exit_code == 0
    url_lines = [line for line in result.output.splitlines() if "dbu=" in line]
    assert len(url_lines) == 1, f"Expected exactly one URL line, got: {url_lines}"
    assert not url_lines[0].lstrip().startswith("[colab]"), (
        f"URL line should not be prefixed with '[colab]' so it's pipeable: "
        f"{url_lines[0]!r}"
    )
