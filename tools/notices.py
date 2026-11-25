#!/usr/bin/env -S uv run python
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

"""Maintainer CLI for managing standalone CLI notices on ``refs/tags/notices``.

Builds commits and moves the annotated ``notices`` git tag via git plumbing
(``hash-object``, ``mktree``, ``commit-tree``, ``tag -fa``) so publishing or
retracting notices never touches the current branch, index, or working tree,
and never triggers the ``refs/tags/v*`` PyPI Cloud Build pipeline.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Optional

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
import typer
from typing_extensions import Annotated

from colab_cli.auto_update import (
    MAX_NOTICE_LENGTH,
    MAX_NOTICES,
    _ANSI_ESCAPE_RE,
    _CONTROL_CHARS_RE,
    announce_notices,
    matching_notices,
)
from colab_cli.state import UpdateNotice

TAG_NAME = "notices"
TAG_REF = f"refs/tags/{TAG_NAME}"
NOTICES_FILENAME = "notices.json"
_GIT_REPO_ENV_VARS = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_SUPER_PREFIX",
        "GIT_WORK_TREE",
    }
)

app = typer.Typer(
    help="Manage standalone Colab CLI update notices on the movable 'notices' git tag.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@dataclass
class RepoConfig:
    repo: Path
    remote: str


def _run_git(
    repo: Path,
    *args: str,
    stdin_text: Optional[str] = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``git`` in ``repo`` using UTF-8 bytes so Windows never rewrites LF to CRLF."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REPO_ENV_VARS}
    raw = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        input=stdin_text.encode("utf-8") if stdin_text is not None else None,
        capture_output=True,
        check=check,
    )
    return subprocess.CompletedProcess(
        raw.args,
        raw.returncode,
        stdout=raw.stdout.decode("utf-8", errors="replace"),
        stderr=raw.stderr.decode("utf-8", errors="replace"),
    )


def _validate_notice(affects: str, message: str) -> UpdateNotice:
    spec_text = affects.strip()
    if not spec_text:
        typer.echo("[notices] Error: 'affects' specifier must not be empty.", err=True)
        raise typer.Exit(code=1)
    try:
        SpecifierSet(spec_text)
    except InvalidSpecifier as e:
        typer.echo(
            f"[notices] Error: invalid PEP 440 specifier {affects!r}: {e}", err=True
        )
        raise typer.Exit(code=1)

    if _ANSI_ESCAPE_RE.search(message) or _CONTROL_CHARS_RE.search(message):
        typer.echo(
            "[notices] Error: 'message' must not contain ANSI escapes or control characters.",
            err=True,
        )
        raise typer.Exit(code=1)

    msg_text = message.strip()
    if not msg_text:
        typer.echo("[notices] Error: 'message' must not be empty.", err=True)
        raise typer.Exit(code=1)
    if len(msg_text) > MAX_NOTICE_LENGTH:
        typer.echo(
            f"[notices] Error: 'message' length ({len(msg_text)}) exceeds {MAX_NOTICE_LENGTH} characters.",
            err=True,
        )
        raise typer.Exit(code=1)

    return UpdateNotice(affects=spec_text, message=msg_text)


def _fetch_tag(cfg: RepoConfig) -> None:
    _run_git(
        cfg.repo,
        "fetch",
        "--quiet",
        cfg.remote,
        f"+{TAG_REF}:{TAG_REF}",
        check=False,
    )


def _current_tag_commit(repo: Path) -> Optional[str]:
    res = _run_git(
        repo, "rev-parse", "--verify", "-q", f"{TAG_REF}^{{commit}}", check=False
    )
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def _read_existing_notices(repo: Path) -> list[UpdateNotice]:
    res = _run_git(repo, "show", f"{TAG_REF}:{NOTICES_FILENAME}", check=False)
    if res.returncode != 0 or not res.stdout.strip():
        return []
    payload = json.loads(res.stdout)
    items = payload.get("notices") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    return [UpdateNotice.model_validate(item) for item in items]


def _write_notices_tag(
    cfg: RepoConfig,
    notices: list[UpdateNotice],
    commit_message: str,
    *,
    push: bool,
) -> str:
    serialized = (
        json.dumps([n.model_dump() for n in notices], indent=2, ensure_ascii=False)
        + "\n"
    )
    blob_sha = _run_git(
        cfg.repo, "hash-object", "-w", "--stdin", stdin_text=serialized
    ).stdout.strip()
    tree_entry = f"100644 blob {blob_sha}\t{NOTICES_FILENAME}\n"
    tree_sha = _run_git(cfg.repo, "mktree", stdin_text=tree_entry).stdout.strip()

    commit_args = ["commit-tree", tree_sha]
    parent_sha = _current_tag_commit(cfg.repo)
    if parent_sha:
        commit_args.extend(["-p", parent_sha])
    commit_args.extend(["-m", commit_message])
    commit_sha = _run_git(cfg.repo, *commit_args).stdout.strip()

    _run_git(cfg.repo, "tag", "-fa", TAG_NAME, "-m", commit_message, commit_sha)
    typer.echo(serialized.rstrip("\n"))

    if len(notices) > MAX_NOTICES:
        typer.echo(
            f"[notices] Note: {len(notices)} rules stored; clients display at most {MAX_NOTICES} matching notices at once."
        )

    if push:
        _run_git(cfg.repo, "push", "-f", cfg.remote, TAG_REF)
        typer.echo(f"[notices] Pushed {TAG_REF} ({commit_sha[:7]}) to {cfg.remote}.")
    else:
        typer.echo(
            f"[notices] Updated local {TAG_REF} ({commit_sha[:7]}). "
            f"Run 'git push -f {cfg.remote} {TAG_REF}' (or pass --push) to publish."
        )
    return commit_sha


@app.callback()
def main_callback(
    ctx: typer.Context,
    repo: Annotated[
        Path,
        typer.Option("--repo", help="Path to git repository root."),
    ] = Path("."),
    remote: Annotated[
        str,
        typer.Option("--remote", help="Git remote hosting refs/tags/notices."),
    ] = "origin",
) -> None:
    ctx.obj = RepoConfig(repo=repo.resolve(), remote=remote)


@app.command(name="list")
def list_notices(
    ctx: typer.Context,
    no_fetch: Annotated[
        bool,
        typer.Option("--no-fetch", help="Inspect local tag without fetching remote."),
    ] = False,
    version: Annotated[
        Optional[str],
        typer.Option(
            "--version",
            "-v",
            help="Preview how a client on this PEP 440 version renders the notices.",
        ),
    ] = None,
) -> None:
    """Show active notices on refs/tags/notices (and optionally preview for a version)."""
    cfg: RepoConfig = ctx.obj
    if not no_fetch:
        _fetch_tag(cfg)

    notices = _read_existing_notices(cfg.repo)
    if not notices:
        typer.echo(f"[notices] No active notices on {TAG_REF}.")
        return

    typer.echo(json.dumps([n.model_dump() for n in notices], indent=2))
    if version is not None:
        try:
            Version(version)
        except InvalidVersion as e:
            typer.echo(
                f"[notices] Error: invalid PEP 440 version {version!r}: {e}", err=True
            )
            raise typer.Exit(code=1)
        matched = matching_notices(notices, version)
        if matched:
            typer.echo(f"\n[notices] Preview for version {version}:")
            announce_notices(matched, show_disable_hint=False)
        else:
            typer.echo(f"\n[notices] No notices match version {version}.")


@app.command(name="add")
def add_notice(
    ctx: typer.Context,
    affects: Annotated[
        str,
        typer.Option(
            "--affects",
            "-a",
            help="PEP 440 version specifier (e.g. '<0.9.0' or '>=0.7.0,<0.8.0').",
        ),
    ],
    message: Annotated[
        str,
        typer.Option(
            "--message", "-m", help="Plain-text notice message (<=500 chars)."
        ),
    ],
    no_fetch: Annotated[
        bool,
        typer.Option("--no-fetch", help="Do not fetch remote tag before appending."),
    ] = False,
    push: Annotated[
        bool,
        typer.Option("--push", help="Force-push refs/tags/notices to the remote."),
    ] = False,
) -> None:
    """Validate and append a notice to refs/tags/notices without touching HEAD."""
    cfg: RepoConfig = ctx.obj
    new_notice = _validate_notice(affects, message)
    if not no_fetch:
        _fetch_tag(cfg)

    notices = _read_existing_notices(cfg.repo)
    if new_notice not in notices:
        notices.append(new_notice)
    _write_notices_tag(
        cfg,
        notices,
        f"notices: add rule for {new_notice.affects}",
        push=push,
    )


@app.command(name="set")
def set_notices(
    ctx: typer.Context,
    source: Annotated[
        str,
        typer.Argument(help="Path to a JSON notices file, or '-' to read stdin."),
    ],
    no_fetch: Annotated[
        bool,
        typer.Option("--no-fetch", help="Do not fetch remote tag before replacing."),
    ] = False,
    push: Annotated[
        bool,
        typer.Option("--push", help="Force-push refs/tags/notices to the remote."),
    ] = False,
) -> None:
    """Replace notices.json on refs/tags/notices from a JSON file or stdin."""
    cfg: RepoConfig = ctx.obj
    raw_text = (
        sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    )
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as e:
        typer.echo(f"[notices] Error: invalid JSON: {e}", err=True)
        raise typer.Exit(code=1)

    items = payload.get("notices") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        typer.echo(
            "[notices] Error: JSON root must be a list or {'notices': [...]}.",
            err=True,
        )
        raise typer.Exit(code=1)

    validated: list[UpdateNotice] = []
    for item in items:
        if not isinstance(item, dict) or "affects" not in item or "message" not in item:
            typer.echo(
                "[notices] Error: each item must be an object with 'affects' and 'message'.",
                err=True,
            )
            raise typer.Exit(code=1)
        validated.append(_validate_notice(str(item["affects"]), str(item["message"])))

    if not no_fetch:
        _fetch_tag(cfg)
    _write_notices_tag(cfg, validated, "notices: replace notices.json", push=push)


@app.command(name="clear")
def clear_notices(
    ctx: typer.Context,
    delete_tag: Annotated[
        bool,
        typer.Option(
            "--delete-tag",
            help="Delete refs/tags/notices instead of writing an empty [] list.",
        ),
    ] = False,
    no_fetch: Annotated[
        bool,
        typer.Option("--no-fetch", help="Do not fetch remote tag before clearing."),
    ] = False,
    push: Annotated[
        bool,
        typer.Option("--push", help="Push the cleared or deleted tag to the remote."),
    ] = False,
) -> None:
    """Retract all active notices (writes [] or deletes refs/tags/notices)."""
    cfg: RepoConfig = ctx.obj
    if not no_fetch:
        _fetch_tag(cfg)

    if not delete_tag:
        _write_notices_tag(cfg, [], "notices: clear all notices", push=push)
        return

    _run_git(cfg.repo, "tag", "-d", TAG_NAME, check=False)
    if push:
        _run_git(cfg.repo, "push", cfg.remote, f":{TAG_REF}")
        typer.echo(f"[notices] Deleted {TAG_REF} from {cfg.remote}.")
    else:
        typer.echo(
            f"[notices] Deleted local {TAG_REF}. "
            f"Run 'git push {cfg.remote} :{TAG_REF}' (or pass --push) to delete on {cfg.remote}."
        )


if __name__ == "__main__":
    app()
