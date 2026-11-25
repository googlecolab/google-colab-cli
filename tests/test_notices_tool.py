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

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

_TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "notices.py"
_SPEC = importlib.util.spec_from_file_location("notices_tool", _TOOL_PATH)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_SPEC.name] = _MOD
_SPEC.loader.exec_module(_MOD)
TAG_NAME = _MOD.TAG_NAME
app = _MOD.app

runner = CliRunner()


def _git(cwd: Path, *args: str) -> str:
    cmd = ["git", f"--git-dir={cwd}", *args] if cwd.suffix == ".git" else ["git", *args]
    env = {k: v for k, v in os.environ.items() if k not in _MOD._GIT_REPO_ENV_VARS}
    return subprocess.check_output(
        cmd,
        cwd=cwd,
        env=env,
        encoding="utf-8",
        stderr=subprocess.PIPE,
    ).strip()


@pytest.fixture
def git_repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Create a bare ``origin`` repo and two working clones isolated from host gitconfig."""
    empty_gitconfig = tmp_path / "gitconfig"
    empty_gitconfig.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    origin = tmp_path / "origin.git"
    clone_a = tmp_path / "clone_a"
    clone_b = tmp_path / "clone_b"

    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "clone", str(origin), str(clone_a))
    for repo in (clone_a,):
        _git(repo, "config", "user.name", "Test User")
        _git(repo, "config", "user.email", "test@example.com")
    (clone_a / "README.md").write_text("hello\n", encoding="utf-8")
    _git(clone_a, "add", "README.md")
    _git(clone_a, "commit", "-m", "initial")
    _git(clone_a, "push", "-u", "origin", "HEAD")

    _git(tmp_path, "clone", str(origin), str(clone_b))
    _git(clone_b, "config", "user.name", "Other User")
    _git(clone_b, "config", "user.email", "other@example.com")

    return origin, clone_a, clone_b


def test_list_when_tag_absent(git_repos):
    _, clone_a, _ = git_repos
    res = runner.invoke(app, ["--repo", str(clone_a), "list"])
    assert res.exit_code == 0, res.output
    assert "No active notices" in res.output


def test_add_preserves_working_tree_and_branch_and_pushes(git_repos):
    origin, clone_a, _ = git_repos
    head_before = _git(clone_a, "rev-parse", "HEAD")
    dirty_file = clone_a / "uncommitted.txt"
    dirty_file.write_text("work in progress\n", encoding="utf-8")

    res = runner.invoke(
        app,
        [
            "--repo",
            str(clone_a),
            "add",
            "--affects",
            "<0.9.0",
            "--message",
            "colab console was removed; use colab ssh instead.",
            "--push",
        ],
    )
    assert res.exit_code == 0, res.output
    assert _git(clone_a, "rev-parse", "HEAD") == head_before
    assert dirty_file.read_text(encoding="utf-8") == "work in progress\n"
    assert not (clone_a / "notices.json").exists()

    remote_json = json.loads(_git(origin, "show", f"refs/tags/{TAG_NAME}:notices.json"))
    assert remote_json == [
        {
            "affects": "<0.9.0",
            "message": "colab console was removed; use colab ssh instead.",
        }
    ]
    assert _git(origin, "cat-file", "-t", f"refs/tags/{TAG_NAME}") == "tag"


def test_add_without_push_stays_local_and_prints_push_hint(git_repos):
    origin, clone_a, _ = git_repos
    res = runner.invoke(
        app,
        [
            "--repo",
            str(clone_a),
            "add",
            "--affects",
            "<1.0.0",
            "--message",
            "Local draft notice.",
        ],
    )
    assert res.exit_code == 0, res.output
    assert f"git push -f origin refs/tags/{TAG_NAME}" in res.output

    local_json = json.loads(_git(clone_a, "show", f"refs/tags/{TAG_NAME}:notices.json"))
    assert len(local_json) == 1
    with pytest.raises(subprocess.CalledProcessError):
        _git(origin, "rev-parse", "--verify", f"refs/tags/{TAG_NAME}")


def test_second_clone_fetches_force_moved_tag_and_chains_history(git_repos):
    origin, clone_a, clone_b = git_repos

    runner.invoke(
        app,
        [
            "--repo",
            str(clone_a),
            "add",
            "--affects",
            "<0.8.0",
            "--message",
            "First notice.",
            "--push",
        ],
    )
    runner.invoke(app, ["--repo", str(clone_b), "list"])

    runner.invoke(
        app,
        [
            "--repo",
            str(clone_a),
            "add",
            "--affects",
            "<0.9.0",
            "--message",
            "Second notice.",
            "--push",
        ],
    )

    res_b = runner.invoke(
        app,
        [
            "--repo",
            str(clone_b),
            "add",
            "--affects",
            "<1.0.0",
            "--message",
            "Third notice.",
            "--push",
        ],
    )
    assert res_b.exit_code == 0, res_b.output

    remote_json = json.loads(_git(origin, "show", f"refs/tags/{TAG_NAME}:notices.json"))
    assert [n["affects"] for n in remote_json] == ["<0.8.0", "<0.9.0", "<1.0.0"]

    commits = _git(origin, "log", "--oneline", f"refs/tags/{TAG_NAME}").splitlines()
    assert len(commits) == 3


def test_list_preview_with_version(git_repos):
    _, clone_a, _ = git_repos
    runner.invoke(
        app,
        [
            "--repo",
            str(clone_a),
            "add",
            "--affects",
            "<0.8.0",
            "--message",
            "Old security notice.",
            "--no-fetch",
        ],
    )
    runner.invoke(
        app,
        [
            "--repo",
            str(clone_a),
            "add",
            "--affects",
            "<1.0.0",
            "--message",
            "1.0 migration notice.",
            "--no-fetch",
        ],
    )

    res_080 = runner.invoke(
        app, ["--repo", str(clone_a), "list", "--no-fetch", "--version", "0.8.0"]
    )
    assert res_080.exit_code == 0, res_080.output
    assert "[colab] Notice: 1.0 migration notice." in res_080.output
    assert "[colab] Notice: Old security notice." not in res_080.output


@pytest.mark.parametrize(
    "affects,message",
    [
        ("", "Valid message"),
        ("not-a-pep440-spec", "Valid message"),
        ("<1.0.0", ""),
        ("<1.0.0", "x" * 501),
        ("<1.0.0", "bad \x1b[31mansi"),
    ],
)
def test_add_rejects_invalid_inputs(git_repos, affects, message):
    _, clone_a, _ = git_repos
    res = runner.invoke(
        app,
        [
            "--repo",
            str(clone_a),
            "add",
            "--affects",
            affects,
            "--message",
            message,
            "--no-fetch",
        ],
    )
    assert res.exit_code != 0


def test_set_from_file_and_clear_and_delete_tag(git_repos, tmp_path):
    origin, clone_a, _ = git_repos
    payload_file = tmp_path / "input_notices.json"
    payload_file.write_text(
        json.dumps([{"affects": "<=0.8.0", "message": "From file."}]),
        encoding="utf-8",
    )

    res_set = runner.invoke(
        app, ["--repo", str(clone_a), "set", str(payload_file), "--push"]
    )
    assert res_set.exit_code == 0, res_set.output
    assert json.loads(_git(origin, "show", f"refs/tags/{TAG_NAME}:notices.json")) == [
        {"affects": "<=0.8.0", "message": "From file."}
    ]

    res_clear = runner.invoke(app, ["--repo", str(clone_a), "clear", "--push"])
    assert res_clear.exit_code == 0, res_clear.output
    assert json.loads(_git(origin, "show", f"refs/tags/{TAG_NAME}:notices.json")) == []

    res_del = runner.invoke(
        app, ["--repo", str(clone_a), "clear", "--delete-tag", "--push"]
    )
    assert res_del.exit_code == 0, res_del.output
    with pytest.raises(subprocess.CalledProcessError):
        _git(origin, "rev-parse", "--verify", f"refs/tags/{TAG_NAME}")


def test_run_git_sends_utf8_lf_bytes_for_windows_mktree_compat(mocker, tmp_path):
    """On Windows, text=True translates '\\n' to '\\r\\n' on stdin, causing
    ``git mktree`` to create an entry named ``"notices.json\\r"`` instead of
    ``notices.json``. ``_run_git`` must pass raw UTF-8 bytes without text mode.
    """
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout=b"deadbeef\n", stderr=b"")

    mocker.patch.object(_MOD.subprocess, "run", side_effect=_fake_run)
    res = _MOD._run_git(
        tmp_path, "mktree", stdin_text="100644 blob abc\tnotices.json\n"
    )
    assert res.stdout == "deadbeef\n"
    assert not captured["kwargs"].get("text")
    assert captured["kwargs"]["input"] == b"100644 blob abc\tnotices.json\n"


def test_add_ignores_inherited_git_dir_and_index_env(git_repos, monkeypatch, tmp_path):
    origin, clone_a, _ = git_repos
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "bogus.git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "bogus.index"))

    res = runner.invoke(
        app,
        [
            "--repo",
            str(clone_a),
            "add",
            "--affects",
            "<1.0.0",
            "--message",
            "Isolated from outer GIT_DIR — UTF-8 check.",
            "--push",
        ],
    )
    assert res.exit_code == 0, res.output
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_INDEX_FILE")
    remote_json = json.loads(_git(origin, "show", f"refs/tags/{TAG_NAME}:notices.json"))
    assert remote_json[0]["message"] == "Isolated from outer GIT_DIR — UTF-8 check."
