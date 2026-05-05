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

"""Auto-update subsystem.

Owns version detection, multi-source update probes, the on-disk
``latest_version`` cache, and the upgrade-banner UX. The CLI's global
callback (``cli.py``) calls ``check_for_updates`` once per day and
``maybe_show_cached_banner`` on every other invocation; the
``colab update`` Typer command (``commands/utility.py``) delegates to
``check_for_updates`` plus ``self_install``.
"""

import json
import subprocess
import urllib.request
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as installed_version
from packaging.version import InvalidVersion, Version
from pathlib import Path
from typing import Optional

import typer

from colab_cli.common import state
from colab_cli.state import Settings


# ---------- Version detection -------------------------------------------


def get_app_version() -> str:
    """Return the installed package version, falling back to the git short hash."""
    try:
        return installed_version("colab")
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


# ---------- Source fetchers ---------------------------------------------


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


def _fetch_local(path: Path, quiet: bool) -> Optional[dict]:
    """Reads and parses the local update file at ``path``."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        # Expected: the default path is only resolvable inside Google's Piper.
        return None
    except Exception as e:
        if not quiet:
            typer.echo(f"[colab] Warning: Failed to read update file {path}: {e}")
        return None


# ---------- Version comparison ------------------------------------------


def _is_newer(candidate: Optional[str], current: str) -> bool:
    """True when ``candidate`` strictly exceeds ``current`` (PEP 440)."""
    if not candidate:
        return False
    try:
        return Version(candidate) > Version(current)
    except InvalidVersion:
        return candidate != current


def _max_version(*candidates: Optional[str]) -> Optional[str]:
    """Returns the highest non-None version string (PEP 440), or None."""
    found = [v for v in candidates if v]
    if not found:
        return None
    try:
        return max(found, key=Version)
    except InvalidVersion:
        return max(found)


# ---------- UX ----------------------------------------------------------


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
    typer.echo(f"[colab] Run '{install_cmd}' to update.")
    if show_disable_hint:
        typer.echo(
            "[colab] To silence this check, set "
            '"enable_update_check": false in '
            "~/.config/colab-cli/settings.json"
        )
    typer.echo("")


# ---------- Orchestration -----------------------------------------------


def check_for_updates(quiet: bool = False) -> Optional[str]:
    """Check for updates and print a message if a new version is available.

    Two independent sources are consulted:

    * ``settings.update_url`` (default: PyPI-style JSON on GitHub).
    * ``settings.update_file_path`` (default: an internal release manifest).

    If the local file reports a newer version it takes precedence and the
    install hint is derived from ``releases[<version>].url`` inside that file.
    Otherwise the PyPI source is used and the standard ``pip install --upgrade``
    hint is printed.

    The disable-hint is appended to the banner only when ``quiet`` is True
    (the daily background fetch); explicit ``colab update`` invocations
    (``quiet=False``) omit it because the user asked for the check.

    Returns the local-file release URL (i.e. ``releases[<version>].url``) when
    the local file reports a strictly-newer version and a URL is recorded;
    otherwise ``None``. Callers can use this to drive a self-install flow.
    """
    settings = state.settings_store.load()
    current = get_app_version()
    local_release_url: Optional[str] = None

    try:
        pypi = _fetch_pypi(settings.update_url, quiet)
        local = _fetch_local(settings.update_file_path, quiet)
        pypi_v = _parse_version(pypi)
        local_v = _parse_version(local)

        if _is_newer(local_v, current):
            url = (local or {}).get("releases", {}).get(local_v, {}).get("url")
            local_release_url = url
            announce_upgrade(
                local_v,
                current,
                f"uv tool install {url or 'colab'}",
                show_disable_hint=quiet,
            )
        elif _is_newer(pypi_v, current):
            announce_upgrade(
                pypi_v,
                current,
                "pip install --upgrade colab",
                show_disable_hint=quiet,
            )
        elif not quiet:
            latest_str = _max_version(pypi_v, local_v)
            suffix = f", latest: {latest_str}" if latest_str else ""
            typer.echo(f"[colab] Colab CLI is up to date (version: {current}{suffix}).")

        # Cache the highest observed version; never downgrade.
        latest = _max_version(pypi_v, local_v)
        cached = settings.latest_version or "0"
        if _is_newer(latest, cached):
            settings.latest_version = latest

        settings.last_check = datetime.now(timezone.utc)
        state.settings_store.save(settings)

    except Exception as e:
        if not quiet:
            typer.echo(f"[colab] Failed to check for updates: {e}")

    return local_release_url


# ---------- Background hooks (called from cli.py) -----------------------


def _is_throttled(settings: Settings, *, now: Optional[datetime] = None) -> bool:
    """True if the once-per-day fetch should be skipped."""
    if settings.last_check is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - settings.last_check).days < 1


def maybe_show_cached_banner(settings: Settings) -> None:
    """Print the cached upgrade banner if the cache reports a newer version.

    Called from the global CLI callback when the daily fetch is throttled.
    The banner uses a generic ``colab update`` install hint because the
    cache does not record which source supplied the version; the disable
    hint is shown because this is unsolicited output.
    """
    if not settings.latest_version:
        return
    current = get_app_version()
    if not _is_newer(settings.latest_version, current):
        return
    announce_upgrade(
        settings.latest_version,
        current,
        "colab update",
        show_disable_hint=True,
    )


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


# ---------- Self-install ------------------------------------------------


def self_install(url: str) -> None:
    """Run ``uv tool install <url>`` to upgrade the locally-installed CLI."""
    cmd = ["uv", "tool", "install", url]
    typer.echo(f"[colab] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)
