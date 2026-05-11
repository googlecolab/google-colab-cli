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

"""Tests for the developer-only `colab whoami` command.

This is a debugging aid that resolves the active credentials, mints an access
token, and queries Google's tokeninfo endpoint to print the human-readable
identity (email), grant scopes, and expiry of whatever the CLI is about to
send to colab.research.google.com / colab.pa.googleapis.com.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from colab_cli.cli import main
from colab_cli.auth import AuthProvider


# Unambiguously-fake placeholders so credential-scanner pre-commit hooks
# don't false-positive on `ya29.*` strings. The whoami code only ever uses
# these as opaque payloads passed to a mocked urllib.request.urlopen.
_FAKE_TOKEN = "TEST-TOKEN-PLACEHOLDER"


def _fake_creds(token: str = _FAKE_TOKEN):
    """Build a credentials-like mock: has .token and .refresh()."""
    creds = MagicMock()
    creds.token = token
    creds.refresh = MagicMock()
    return creds


def _fake_authed_session(token: str = _FAKE_TOKEN):
    """Mimic google.auth.transport.requests.AuthorizedSession enough for whoami."""
    sess = MagicMock()
    sess.credentials = _fake_creds(token)
    return sess


def test_whoami_prints_human_readable_summary(mock_common_state, capsys):
    """Default invocation should fetch the token, hit tokeninfo, and print
    a labelled summary including email, the active auth provider, scopes
    (one per line), and an Expires line."""
    mock_common_state.auth_provider = AuthProvider.ADC

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "email": "user@example.com",
        "scope": (
            "https://www.googleapis.com/auth/userinfo.email "
            "https://www.googleapis.com/auth/colaboratory"
        ),
        "expires_in": "2847",
        "audience": "32555940559.apps.googleusercontent.com",
    }

    with patch("colab_cli.auth.get_credentials", return_value=_fake_authed_session()):
        with patch("urllib.request.urlopen") as mock_urlopen:
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cm)
            cm.__exit__ = MagicMock(return_value=False)
            cm.read.return_value = b'{"email":"user@example.com","scope":"https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/colaboratory","expires_in":"2847","audience":"32555940559.apps.googleusercontent.com"}'
            cm.status = 200
            mock_urlopen.return_value = cm

            with patch.object(sys, "argv", ["colab", "--auth=adc", "whoami"]):
                with pytest.raises(SystemExit) as error:
                    main()
                assert error.value.code == 0

    out = capsys.readouterr().out
    assert "user@example.com" in out
    assert "adc" in out.lower()
    assert "userinfo.email" in out
    assert "colaboratory" in out
    # Expiry should be rendered in a human form (minutes), not raw seconds.
    assert "47m" in out or "47 min" in out


def test_whoami_handles_tokeninfo_error_gracefully(mock_common_state, capsys):
    """If tokeninfo returns 4xx (e.g. expired/revoked token), whoami should
    print an error and exit non-zero rather than blowing up with a stack trace.
    Developers reading the message should be able to tell what happened.
    """
    mock_common_state.auth_provider = AuthProvider.ADC

    with patch("colab_cli.auth.get_credentials", return_value=_fake_authed_session()):
        with patch("urllib.request.urlopen") as mock_urlopen:
            import urllib.error

            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="https://oauth2.googleapis.com/tokeninfo",
                code=400,
                msg="Bad Request",
                hdrs=None,
                fp=None,
            )

            with patch.object(sys, "argv", ["colab", "--auth=adc", "whoami"]):
                with pytest.raises(SystemExit) as error:
                    main()
                assert error.value.code != 0

    captured = capsys.readouterr()
    assert "tokeninfo" in (captured.out + captured.err).lower() or "400" in (
        captured.out + captured.err
    )


def test_whoami_is_hidden_from_top_level_help(mock_common_state, capsys):
    """`colab --help` should not list `whoami` (it's a developer tool).
    Also asserts that `colab whoami --help` still works (the command is
    hidden, not removed)."""
    # `--help` exits 0
    with patch.object(sys, "argv", ["colab", "--help"]):
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 0
    out = capsys.readouterr().out
    assert "whoami" not in out, (
        f"`whoami` should be hidden from `colab --help`, but appeared in:\n{out}"
    )

    # Confirm `colab whoami --help` is still reachable.
    with patch.object(sys, "argv", ["colab", "whoami", "--help"]):
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 0
    out2 = capsys.readouterr().out
    assert "whoami" in out2.lower(), (
        f"`colab whoami --help` should describe the command, got:\n{out2}"
    )


def test_whoami_refreshes_credentials_before_reading_token(mock_common_state):
    """Some credentials (ADC service account, GCE) lazy-mint the token only
    when refresh() is called. whoami must call refresh() before reading
    creds.token, otherwise creds.token may be None even for valid creds.
    """
    mock_common_state.auth_provider = AuthProvider.ADC
    sess = _fake_authed_session(token="TEST-TOKEN-AFTER-REFRESH")

    with patch("colab_cli.auth.get_credentials", return_value=sess):
        with patch("urllib.request.urlopen") as mock_urlopen:
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cm)
            cm.__exit__ = MagicMock(return_value=False)
            cm.read.return_value = (
                b'{"email":"x@y.com","scope":"a b","expires_in":"60"}'
            )
            cm.status = 200
            mock_urlopen.return_value = cm

            with patch.object(sys, "argv", ["colab", "--auth=adc", "whoami"]):
                with pytest.raises(SystemExit):
                    main()

    sess.credentials.refresh.assert_called_once()
