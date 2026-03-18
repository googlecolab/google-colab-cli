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
import logging
import os
import re
import subprocess
import sys
from importlib import resources
from typing import Optional

from google.auth.transport import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

# Standard Scopes for Colab and Drive (Public Auth)
PUBLIC_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/colaboratory",
    "https://www.googleapis.com/auth/drive.file",
]

# Internal 1P Scopes
INTERNAL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"

TOKEN_CONFIG_PATH = os.path.expanduser("~/.config/colab-cli/token.json")
OAUTH_SERVER_PORT = 8200


def _run_stubby_exchange() -> str:
    user = os.environ.get("USER")
    if not user:
        import getpass

        user = getpass.getuser()

    stubby_input = f"""
target: {{
  scope: GAIA_USER
  name: "{user}@google.com"
}}
target_credential: {{
  type: OAUTH2_TOKEN
  oauth2_attributes: {{
    scope: '{INTERNAL_SCOPE}'
  }}
}}
"""
    cmd = ["stubby", "--proto2", "call", "blade:sso", "CorpLogin.Exchange"]
    try:
        logging.info("cmd: %s" % " ".join(cmd))
        result = subprocess.run(
            cmd, input=stubby_input, text=True, capture_output=True, check=True
        )
        # Parse output for oauth2_token: "..."
        match = re.search(r'oauth2_token:\s*"([^"]+)"', result.stdout)
        if match:
            return match.group(1)
    except subprocess.CalledProcessError as e:
        logger.debug(f"Stubby exchange failed: {e.stderr}")

    return ""


def _get_loas2_credentials() -> Credentials:
    """Retrieves credentials using internal 1P auth (Stubby/SSO)."""
    # Check if gcert is valid for at least 5 minutes
    gcert_status = subprocess.run(["gcertstatus", "--check_remaining=5m", "--quiet"])
    if gcert_status.returncode != 0:
        print("[colab] LOAS credentials are missing or expired. Refreshing (gcert)...")
        try:
            # Run gcert interactively
            subprocess.run(["gcert"], check=True)
        except Exception as e:
            print(f"[colab] Error running gcert: {e}")
            sys.exit(1)

    logging.info("Trying to get token...")
    token = _run_stubby_exchange()

    if not token:
        print("[colab] ERROR: Failed to obtain internal OAuth2 token via stubby.")
        print("[colab] Please ensure you have valid credentials (run 'gcert').")
        sys.exit(1)

    return Credentials(token=token)


def _get_google_auth_credentials(config_path: str) -> Credentials:
    """
    Retrieves credentials using standard public OAuth2 flow.
    """
    client_config = None

    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            client_config = json.load(f)
    else:
        # Last resort: try inlined config
        try:
            config_resource = resources.files("colab_cli").joinpath("oauth_config.json")
            if config_resource.is_file():
                client_config = json.loads(config_resource.read_text())
        except Exception as e:
            logger.debug(f"Failed to load inlined config: {e}")

    if not client_config:
        raise FileNotFoundError(
            f"Client OAuth config not found at {config_path} and no inlined config available. "
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


def get_credentials(
    config_path: Optional[str] = None, use_loas2: bool = True
) -> requests.AuthorizedSession:
    """
    Unified entry point for retrieving an authorized session.
    """
    if use_loas2:
        creds = _get_loas2_credentials()
    else:
        if not config_path:
            config_path = os.path.expanduser("~/.colab-cli-oauth-config.json")
        creds = _get_google_auth_credentials(config_path)

    return requests.AuthorizedSession(creds)
