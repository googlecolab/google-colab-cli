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
"""Live server-side probe for the OOB (urn:ietf:wg:oauth:2.0:oob) flow.

Does NOT complete the flow (no browser, no paste). It builds the OOB
authorization URL exactly as run_console() would, then issues an HTTP GET to
Google's authorize endpoint and inspects the response to determine whether
Google still accepts the OOB redirect_uri for *this* client, or rejects it
(redirect_uri_mismatch / invalid_request / 400).
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from google_auth_oauthlib.flow import InstalledAppFlow

# gcloud's sanctioned remote copy-paste landing page (NOT OOB).
REMOTE_REDIRECT_URI = "https://sdk.cloud.google.com/applicationdefaultauthcode.html"

# Default to the REPO-BUNDLED config (the OAUTH2 provider's real client),
# overridable via env. This is the colab-cli client 764086051850-...
_DEFAULT_BUNDLED = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "src",
    "colab_cli",
    "oauth_config.json",
)
CONFIG_PATH = os.path.expanduser(os.environ.get("COLAB_OAUTH_CONFIG", _DEFAULT_BUNDLED))
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/colaboratory",
]


def build_remote_auth_url() -> str:
    with open(CONFIG_PATH) as f:
        client_config = json.load(f)
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    flow.redirect_uri = REMOTE_REDIRECT_URI
    # token_usage=remote tells Google to render the copy-paste code page.
    auth_url, _ = flow.authorization_url(prompt="consent", token_usage="remote")
    return auth_url


def classify(status: int, final_url: str, body: str) -> str:
    # Google often returns HTTP 200 while redirecting the *browser* to an
    # error page; inspect final_url (which carries an `authError` blob) and
    # body, not just the status code.
    haystack = (final_url + " " + body).lower()
    if "signin/oauth/error" in final_url or "autherror" in final_url.lower():
        return "REJECTED: redirected to Google OAuth error page (see authError)"
    if "redirect_uri_mismatch" in haystack:
        return "REJECTED: redirect_uri_mismatch (OOB not allowed for this client)"
    if "out-of-band" in haystack or "oob) flow has been blocked" in haystack:
        return "REJECTED: OOB flow blocked server-side"
    if "invalid_request" in haystack and "redirect" in haystack:
        return "REJECTED: invalid_request re: redirect_uri"
    if status == 400:
        return "REJECTED: HTTP 400 from authorize endpoint"
    if final_url.startswith("https://accounts.google.com/v3/signin") or (
        "consent" in haystack and status in (200, 302)
    ):
        return "ACCEPTED: reached Google sign-in/consent (no rejection seen)"
    return f"UNKNOWN: status={status}"


def main() -> int:
    if not os.path.exists(CONFIG_PATH):
        print(f"NO CONFIG at {CONFIG_PATH}", file=sys.stderr)
        return 2

    auth_url = build_remote_auth_url()
    print("Generated remote copy-paste auth URL:")
    print(" ", auth_url)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
    print("redirect_uri param:", q.get("redirect_uri"))
    print("client_id param:", q.get("client_id"))
    print("-" * 60)

    req = urllib.request.Request(
        auth_url,
        headers={"User-Agent": "Mozilla/5.0 (oob-probe)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            final_url = resp.geturl()
            body = resp.read(20000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
        final_url = e.url if hasattr(e, "url") else auth_url
        body = e.read(20000).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        print(f"REQUEST FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 3

    print("HTTP status:", status)
    print("final URL:", final_url)
    verdict = classify(status, final_url, body)
    print("-" * 60)
    print("VERDICT:", verdict)
    # Dump a small snippet of the body for manual inspection.
    snippet = " ".join(body.split())[:600]
    print("body snippet:", snippet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
