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

import enum
import json
import logging
import os
from typing import Optional

import google.auth
from google.auth.transport import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)


class AuthProvider(str, enum.Enum):
    """Authentication strategy for talking to the Colab backend.

    Values are the lowercase strings accepted by the global ``--auth`` flag.
    """

    OAUTH2 = "oauth2"
    ADC = "adc"


# Standard Scopes for Colab and Drive (Public Auth)
PUBLIC_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/colaboratory",
    "https://www.googleapis.com/auth/drive.file",
]


TOKEN_CONFIG_PATH = os.path.expanduser("~/.config/colab-cli/token.json")
OAUTH_SERVER_PORT = 8200


def _get_google_auth_credentials(config_path: str) -> Credentials:
    """
    Retrieves credentials using standard public OAuth2 flow.
    """
    client_config = None
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            client_config = json.load(f)
    if not client_config:
        raise FileNotFoundError(
            f"Client OAuth config not found at {config_path}. "
            "Please provide a valid path via -c/--client-oauth-config."
        )

    creds = None

    # Ensure config directory exists for the token file
    os.makedirs(os.path.dirname(TOKEN_CONFIG_PATH), exist_ok=True)

    if os.path.exists(TOKEN_CONFIG_PATH):
        try:
            creds = Credentials.from_authorized_user_file(
                TOKEN_CONFIG_PATH, PUBLIC_SCOPES
            )
        except Exception as e:
            logger.warning(f"Failed to load token from {TOKEN_CONFIG_PATH}: {e}")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                logger.warning(f"Failed to refresh token: {e}")
                creds = None

        if not creds:
            flow = InstalledAppFlow.from_client_config(client_config, PUBLIC_SCOPES)
            creds = flow.run_local_server(port=OAUTH_SERVER_PORT)

        # Save the credentials for the next run
        try:
            with open(TOKEN_CONFIG_PATH, "w") as token_file:
                token_file.write(creds.to_json())
        except Exception as e:
            logger.error(f"Failed to save token to {TOKEN_CONFIG_PATH}: {e}")

    return creds


def _get_adc_credentials() -> Credentials:
    """Retrieves credentials using Google Application Default Credentials.

    Honors the standard ADC discovery chain (``GOOGLE_APPLICATION_CREDENTIALS``,
    ``gcloud auth application-default login``, GCE/GKE metadata server, etc.).

    The RuntimeService at colab.pa.googleapis.com requires the
    `colaboratory` scope (otherwise keep-alive returns 403 SCOPE_NOT_PERMITTED).
    Most ADC credential types (service accounts, GCE/GKE, impersonated)
    support `with_scopes`; user credentials minted by
    `gcloud auth application-default login` do not. For the latter, the user
    must re-run `gcloud auth application-default login` with
    `--scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/colaboratory`.
    """
    creds, _ = google.auth.default(scopes=list(PUBLIC_SCOPES))
    # Some credential subclasses ignore the `scopes=` kwarg in `default()`
    # (e.g. user creds), so re-apply via `with_scopes` when supported.
    if getattr(creds, "requires_scopes", False):
        try:
            creds = creds.with_scopes(list(PUBLIC_SCOPES))
        except Exception as e:  # NotImplementedError for non-scopable creds.
            logger.debug(f"Could not augment ADC scopes via with_scopes: {e}")
    return creds


def get_credentials(
    config_path: Optional[str] = None,
    provider: AuthProvider = AuthProvider.OAUTH2,
) -> requests.AuthorizedSession:
    """Unified entry point for retrieving an authorized session.

    Args:
      config_path: Path to the OAuth2 client config JSON. Only consulted when
        ``provider`` is ``OAUTH2``.
      provider: Which authentication strategy to use.
    """
    if provider == AuthProvider.OAUTH2:
        if not config_path:
            config_path = os.path.expanduser("~/.colab-cli-oauth-config.json")
        creds = _get_google_auth_credentials(config_path)
    elif provider == AuthProvider.ADC:
        creds = _get_adc_credentials()
    else:
        raise ValueError(f"Unknown auth provider: {provider!r}")

    return requests.AuthorizedSession(creds)
