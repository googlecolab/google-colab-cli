#!/usr/bin/env python3
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
"""INTERACTIVE end-to-end check of the remote copy-paste OAuth flow.

This proves the full code->token exchange works for the colab-cli bundled
client BEFORE we wire it into auth.py. Requires a browser + paste; cannot run
unattended.

Run:  uv run python integration/repro_oob_probe/verify_remote_exchange.py
"""

import json
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

REMOTE_REDIRECT_URI = "https://sdk.cloud.google.com/applicationdefaultauthcode.html"

_DEFAULT_BUNDLED = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "src",
    "colab_cli",
    "oauth_config.json",
)
CONFIG_PATH = os.path.expanduser(os.environ.get("COLAB_OAUTH_CONFIG", _DEFAULT_BUNDLED))

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/colaboratory",
    "https://www.googleapis.com/auth/drive.file",
]


def main() -> int:
    with open(CONFIG_PATH) as f:
        client_config = json.load(f)

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    flow.redirect_uri = REMOTE_REDIRECT_URI
    auth_url, _ = flow.authorization_url(prompt="consent", token_usage="remote")

    print("\n1. Visit this URL in any browser:\n")
    print("   " + auth_url + "\n")
    print("2. Sign in & approve. Google will show you a code on the")
    print("   'gcloud CLI Remote Login' page.\n")
    code = input("3. Paste the authorization code here: ").strip()

    flow.fetch_token(code=code)
    creds = flow.credentials

    print("\n--- SUCCESS ---")
    print("token present:", bool(creds.token))
    print("refresh_token present:", bool(creds.refresh_token))
    print("scopes:", creds.scopes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
