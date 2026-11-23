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

"""Auto-update and standalone notices subsystem.

Owns version detection, the PyPI-style update probe, standalone version-gated
notices fetched from a movable GitHub tag (`notices_url`), the on-disk
`Settings` cache, and the upgrade / notice banners. The CLI's global callback
(`cli.py`) calls `check_for_updates` once per day and
`maybe_show_cached_banner` on every other invocation; the `colab update` Typer
command (`commands/utility.py`) delegates to `check_for_updates`.
"""

import json
import platform
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as installed_version
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from typing import Optional

import typer

from colab_cli.common import state
from colab_cli.state import Settings, UpdateNotice

PYPI_PACKAGE_NAME = "google-colab-cli"
MAX_NOTICES = 3
MAX_NOTICE_LENGTH = 500
_DISABLE_HINT = (
    '[colab] To silence this check, set "enable_update_check": false in '
    "~/.config/colab-cli/settings.json"
)
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))"
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def is_self_install_supported() -> bool:
    """Return True if self-install (--install) is supported on the current platform."""
    return platform.system() in ("Linux", "Darwin")


def get_app_version() -> str:
    """Return the installed package version, falling back to the git short hash."""
    try:
        return installed_version("google-colab-cli")
    except (PackageNotFoundError, InvalidVersion):
        pass

    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
        ).strip()
    except Exception:
        return "unknown"


def _parse_version(payload: Optional[dict]) -> Optional[str]:
    """Returns ``info.version`` from a PyPI-style payload, or None."""
    return (payload or {}).get("info", {}).get("version")


def _fetch_pypi(url: str, quiet: bool) -> Optional[dict]:
    """Fetches and parses the PyPI-style JSON document at ``url``."""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        if not quiet:
            typer.echo(f"[colab] Warning: Failed to fetch update info: {e}")
        return None


def _parse_notices(payload: object) -> Optional[list[UpdateNotice]]:
    """Parse a JSON array (or ``{"notices": [...]}`` object) into valid notices."""
    if isinstance(payload, dict):
        raw_items = payload.get("notices")
    elif isinstance(payload, list):
        raw_items = payload
    else:
        return None
    if not isinstance(raw_items, list):
        return None

    notices: list[UpdateNotice] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            notice = UpdateNotice.model_validate(item)
        except Exception:
            continue
        if notice.affects.strip() and notice.message.strip():
            notices.append(notice)
    return notices


def _fetch_notices(url: str, quiet: bool) -> Optional[list[UpdateNotice]]:
    """Fetch remote standalone notices from ``url``.

    HTTP 404 returns ``[]`` without warning so an absent or deleted ``notices``
    tag cleanly clears stale cached notices; transient errors return ``None``
    so the caller preserves its cached notices.
    """
    if not url:
        return []
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return _parse_notices(payload)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        if not quiet:
            typer.echo(f"[colab] Warning: Failed to fetch update notices: {e}")
        return None
    except Exception as e:
        if not quiet:
            typer.echo(f"[colab] Warning: Failed to fetch update notices: {e}")
        return None


def _is_newer(candidate: Optional[str], current: str) -> bool:
    """True when ``candidate`` strictly exceeds ``current`` (PEP 440)."""
    if not candidate:
        return False
    try:
        return Version(candidate) > Version(current)
    except InvalidVersion:
        return candidate != current


def _sanitize_notice(text: str) -> str:
    cleaned = _ANSI_ESCAPE_RE.sub("", text)
    cleaned = _CONTROL_CHARS_RE.sub(" ", cleaned).strip()
    return cleaned[:MAX_NOTICE_LENGTH]


def matching_notices(notices: list[UpdateNotice], current: str) -> list[str]:
    """Return sanitized messages from ``notices`` whose PEP 440 specifier matches ``current``."""
    try:
        current_ver = Version(current)
    except InvalidVersion:
        return []

    matched: list[str] = []
    for notice in notices:
        spec_text = notice.affects.strip()
        if not spec_text:
            continue
        try:
            spec = SpecifierSet(spec_text)
        except InvalidSpecifier:
            continue
        if not spec.contains(current_ver, prereleases=True):
            continue
        msg = _sanitize_notice(notice.message)
        if msg and msg not in matched:
            matched.append(msg)
        if len(matched) >= MAX_NOTICES:
            break
    return matched


def announce_upgrade(
    latest: str,
    current: str,
    install_cmd: str,
    *,
    show_disable_hint: bool = False,
) -> None:
    """Print the upgrade banner.

    ``show_disable_hint`` controls whether the trailing line that explains
    how to silence the auto-check is included. It is only added when the
    banner is shown unsolicited (the daily background fetch and the cached
    banner on subsequent invocations); explicit ``colab update`` calls
    omit it because the user already opted in to seeing the result.
    """
    typer.echo(
        f"\n[colab] A new version of Colab CLI is available: {latest} (current: {current})"
    )
    if is_self_install_supported() and ("pip" in install_cmd or "uv" in install_cmd):
        typer.echo("[colab] You can run 'colab update --install' to upgrade in place.")
    typer.echo(f"[colab] Run '{install_cmd}' to update.")
    if show_disable_hint:
        typer.echo(_DISABLE_HINT)
    typer.echo("")


def announce_notices(
    messages: list[str],
    *,
    show_disable_hint: bool = False,
) -> None:
    """Print standalone notices matching the installed CLI version."""
    if not messages:
        return
    typer.echo(f"\n[colab] Notice: {messages[0]}")
    for msg in messages[1:]:
        typer.echo(f"[colab] Notice: {msg}")
    if show_disable_hint:
        typer.echo(_DISABLE_HINT)
    typer.echo("")


def _get_install_command() -> str:
    """Return the recommended installation command based on the environment."""
    import sys

    if is_self_install_supported() and "/uv/tools/" in sys.executable:
        return f"uv tool install -U {PYPI_PACKAGE_NAME}"
    return f"pip install --upgrade {PYPI_PACKAGE_NAME}"


def check_for_updates(quiet: bool = False) -> None:
    """Check PyPI and standalone notices, printing applicable banners and caching state.

    The disable-hint is appended only when ``quiet`` is True (the daily
    background fetch); explicit ``colab update`` invocations (``quiet=False``)
    omit it because the user asked for the check.
    """
    settings = state.settings_store.load()
    current = get_app_version()

    try:
        pypi = _fetch_pypi(settings.update_url, quiet)
        pypi_v = _parse_version(pypi)
        fetched_notices = _fetch_notices(settings.notices_url, quiet)
        if fetched_notices is not None:
            settings.update_notices = fetched_notices

        has_upgrade = _is_newer(pypi_v, current)
        matched = matching_notices(settings.update_notices, current)

        if has_upgrade:
            announce_upgrade(
                pypi_v,
                current,
                _get_install_command(),
                show_disable_hint=quiet and not matched,
            )
        elif not quiet:
            suffix = f", latest: {pypi_v}" if pypi_v else ""
            typer.echo(f"[colab] Colab CLI is up to date (version: {current}{suffix}).")

        if matched:
            announce_notices(matched, show_disable_hint=quiet)

        cached = settings.latest_version or "0"
        if _is_newer(pypi_v, cached):
            settings.latest_version = pypi_v

        settings.last_check = datetime.now(timezone.utc)
        state.settings_store.save(settings)

    except Exception as e:
        if not quiet:
            typer.echo(f"[colab] Failed to check for updates: {e}")


def _is_throttled(settings: Settings, *, now: Optional[datetime] = None) -> bool:
    """True if the once-per-day fetch should be skipped."""
    if settings.last_check is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - settings.last_check).days < 1


def maybe_show_cached_banner(settings: Settings) -> None:
    """Print cached upgrade and/or standalone notice banners when throttled.

    Called from the global CLI callback when the daily fetch is throttled.
    Both banners are evaluated independently so standalone notices still appear
    when the user is already on the latest PyPI version.
    """
    current = get_app_version()
    has_upgrade = bool(
        settings.latest_version and _is_newer(settings.latest_version, current)
    )
    matched = matching_notices(settings.update_notices, current)
    if has_upgrade:
        announce_upgrade(
            settings.latest_version,
            current,
            "colab update",
            show_disable_hint=not matched,
        )
    if matched:
        announce_notices(matched, show_disable_hint=True)


def run_background_check() -> None:
    """Entry point for the global CLI callback.

    Performs either the daily fetch (which writes the cache) or, if
    throttled, surfaces the cached banner. Honors the
    ``enable_update_check`` master switch.
    """
    settings = state.settings_store.load()
    if not settings.enable_update_check:
        return
    if _is_throttled(settings):
        maybe_show_cached_banner(settings)
    else:
        check_for_updates(quiet=True)


def self_install() -> None:
    """Upgrade the CLI in place, detecting uv vs pip."""
    import sys

    if "/uv/tools/" in sys.executable:
        cmd = ["uv", "tool", "install", "-U", PYPI_PACKAGE_NAME]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "-U", PYPI_PACKAGE_NAME]

    typer.echo(f"[colab] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)
