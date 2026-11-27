"""commit_link: a Bash `git commit` runs taskfw-backfill on that repo (task:e60e2713).

Real git repos and a stand-in backfill script that records its arguments.
"""
import subprocess

import pytest

from hooks import commit_link
from hooks.commit_link import commit_dirs


@pytest.fixture
def calls(tmp_path, monkeypatch):
    out = tmp_path / "args.txt"
    script = tmp_path / "backfill"
    script.write_text(f'#!/bin/sh\necho "$@" >> {out}\n')
    script.chmod(0o755)
    monkeypatch.setenv("CLAUDE_HOOKS_BACKFILL_BIN", str(script))
    return lambda: out.read_text().splitlines() if out.exists() else []


def make_repo(path, commits=1):
    path.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], capture_output=True, check=True)  # noqa: E731
    run("init", "-q")
    for n in range(commits):
        run("-c", "user.email=t@example.com", "-c", "user.name=T", "-c", "core.hooksPath=/dev/null",
            "commit", "-q", "--allow-empty", "-m", f"c{n}")
    return path.resolve()


def bash(command, cwd):
    return {"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}}


def test_commit_reads_only_head(tmp_path, calls):
    repo = make_repo(tmp_path / "r", commits=2)
    commit_link.link(bash("git commit -m x", repo))
    assert calls() == [f"--repo {repo} --since HEAD~1"]


def test_root_commit_has_no_since(tmp_path, calls):
    repo = make_repo(tmp_path / "r")
    commit_link.link(bash(f"cd {repo} && git commit -m x", tmp_path))
    assert calls() == [f"--repo {repo}"]


@pytest.mark.parametrize("command", ["git log -1", 'echo "git commit"', "ls"])
def test_other_commands_run_nothing(tmp_path, calls, command):
    commit_link.link(bash(command, make_repo(tmp_path / "r")))
    assert calls() == []


def test_outside_a_repo_runs_nothing(tmp_path, calls):
    commit_link.link(bash("git commit -m x", tmp_path))
    assert calls() == []


def test_missing_cli_fails_open(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOOKS_BACKFILL_BIN", str(tmp_path / "absent"))
    commit_link.link(bash("git commit -m x", make_repo(tmp_path / "r")))


@pytest.mark.parametrize("command,expected", [
    ("git commit -m x", ["/a"]),
    ("cd sub && git commit -F m.txt", ["/a/sub"]),
    ("git -c user.name=x commit -m y", ["/a"]),
    ("git -C /b -c k=v commit", ["/b"]),
    ("GIT_EDITOR=true git commit", ["/a"]),
])
def test_commit_dirs(command, expected):
    assert commit_dirs(command, "/a") == expected
