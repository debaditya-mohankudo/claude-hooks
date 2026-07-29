"""Tests for hooks.dispatcher._record_bash_commit — PostToolUse commit-SHA capture."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from hooks.dispatcher import _record_bash_commit, _record_mcp_commit


def _fake_rev_parse(sha: str, returncode: int = 0):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = f"{sha}\n"
    result.stderr = ""
    return result


class TestRecordBashCommit:
    def test_non_commit_command_is_ignored(self):
        with patch("subprocess.run") as mock_run:
            _record_bash_commit({"cwd": "/repo"}, {"command": "git status"}, "sess1")
        mock_run.assert_not_called()

    def test_commit_without_task_id_is_ignored(self):
        with patch("subprocess.run") as mock_run:
            _record_bash_commit({"cwd": "/repo"}, {"command": "git commit -m fix"}, "sess1")
        mock_run.assert_not_called()

    def test_commit_with_task_id_records_head_sha(self):
        with patch("subprocess.run", return_value=_fake_rev_parse("abc1234def")) as mock_run, \
             patch("src.tools.tasks.record_commit", return_value={"ok": True}) as mock_record:
            _record_bash_commit(
                {"cwd": "/repo"},
                {"command": "git commit -m 'fix\n\ntask:abc12345'"},
                "sess1",
            )
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[:2] == ["git", "-C"]
        assert "/repo" in args
        assert args[-2:] == ["rev-parse", "HEAD"]
        mock_record.assert_called_once_with(task_id="abc12345", commit_hash="abc1234def", repo_path="/repo")

    def test_rev_parse_failure_does_not_raise(self):
        with patch("subprocess.run", return_value=_fake_rev_parse("", returncode=128)), \
             patch("src.tools.tasks.record_commit") as mock_record:
            _record_bash_commit(
                {"cwd": "/repo"},
                {"command": "git commit -m 'fix\n\ntask:abc12345'"},
                "sess1",
            )
        mock_record.assert_not_called()

    def test_earlier_prose_task_id_does_not_shadow_closing_tag(self):
        """A commit body may legitimately mention a different task in prose
        (e.g. reverting/superseding it) before its own closing task:<id>
        tag — the closing tag must win, not whichever appears first
        (task:9f75c02c, found via task:a7ddc132's commit landing under the
        wrong task_id in commit_task_map)."""
        with patch("subprocess.run", return_value=_fake_rev_parse("abc1234def")), \
             patch("src.tools.tasks.record_commit", return_value={"ok": True}) as mock_record:
            _record_bash_commit(
                {"cwd": "/repo"},
                {"command": "git commit -m 'Revert X\n\ntask:850ddd65 marked abandoned, pointing here.\n\ntask:a7ddc132'"},
                "sess1",
            )
        mock_record.assert_called_once_with(task_id="a7ddc132", commit_hash="abc1234def", repo_path="/repo")

    def test_missing_cwd_falls_back_to_env(self):
        with patch.dict("os.environ", {"CLAUDE_CWD": "/env/repo"}, clear=False), \
             patch("subprocess.run", return_value=_fake_rev_parse("abc1234def")) as mock_run, \
             patch("src.tools.tasks.record_commit", return_value={"ok": True}) as mock_record:
            _record_bash_commit(
                {},
                {"command": "git commit -m 'fix\n\ntask:abc12345'"},
                "sess1",
            )
        assert "/env/repo" in mock_run.call_args[0][0]
        mock_record.assert_called_once_with(task_id="abc12345", commit_hash="abc1234def", repo_path="/env/repo")


class TestRecordMcpCommit:
    def test_successful_commit_records_sha(self):
        with patch("src.tools.tasks.record_commit", return_value={"ok": True}) as mock_record:
            _record_mcp_commit(
                {"task_id": "task:abc12345", "message": "fix"},
                {"ok": True, "output": "...", "commit_sha": "abc1234def"},
                "sess1",
                "/repo",
            )
        mock_record.assert_called_once_with(task_id="abc12345", commit_hash="abc1234def", repo_path="/repo")

    def test_bare_task_id_without_prefix(self):
        with patch("src.tools.tasks.record_commit", return_value={"ok": True}) as mock_record:
            _record_mcp_commit(
                {"task_id": "abc12345"},
                {"ok": True, "commit_sha": "abc1234def"},
                "sess1",
                "/repo",
            )
        mock_record.assert_called_once_with(task_id="abc12345", commit_hash="abc1234def", repo_path="/repo")

    def test_failed_commit_is_ignored(self):
        with patch("src.tools.tasks.record_commit") as mock_record:
            _record_mcp_commit(
                {"task_id": "task:abc12345"},
                {"ok": False, "error": "nothing to commit"},
                "sess1",
                "/repo",
            )
        mock_record.assert_not_called()

    def test_missing_commit_sha_is_ignored(self):
        with patch("src.tools.tasks.record_commit") as mock_record:
            _record_mcp_commit(
                {"task_id": "task:abc12345"},
                {"ok": True, "output": "..."},
                "sess1",
                "/repo",
            )
        mock_record.assert_not_called()

    def test_missing_task_id_is_ignored(self):
        with patch("src.tools.tasks.record_commit") as mock_record:
            _record_mcp_commit(
                {},
                {"ok": True, "commit_sha": "abc1234def"},
                "sess1",
                "/repo",
            )
        mock_record.assert_not_called()
