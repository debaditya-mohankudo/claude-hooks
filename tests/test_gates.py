"""Tests for hooks/gates.py — Gate ABC, GateContext, the commit gates, and check()."""
import pytest

from hooks.gates import (
    Gate, GateContext, GATES, check,
    GitCommitGate, GitCommitMcpGate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(
    tool_name: str = "imessage__send",
    tool_input: dict | None = None,
    prompt_id: str = "p1",
) -> GateContext:
    return GateContext(tool_name=tool_name, tool_input=tool_input or {}, prompt_id=prompt_id)


# ---------------------------------------------------------------------------
# Gate is ABC — cannot instantiate directly
# ---------------------------------------------------------------------------

def test_gate_is_abstract():
    with pytest.raises(TypeError):
        Gate()


# ---------------------------------------------------------------------------
# GateContext carries only what the gates read (task:84dbcf14)
# ---------------------------------------------------------------------------

def test_gate_context_has_no_session_history():
    """The history fields and helpers (prev_tools, called_this_session,
    called_recently, prompt_texts) were removed because no gate read them and
    the state behind them leaked memory. Re-adding one should be a deliberate
    choice with a reader, so this pins the slim shape."""
    import dataclasses
    assert [f.name for f in dataclasses.fields(GateContext)] == ["tool_name", "tool_input", "prompt_id"]
    for helper in ("prev_tools", "called_this_session", "called_recently", "prompt_texts"):
        assert not hasattr(GateContext, helper)


# ---------------------------------------------------------------------------
# check() dispatch
# ---------------------------------------------------------------------------

def test_check_ungated_tool_always_allowed():
    ctx = _ctx("some__unknown_tool")
    deny, reason = check("some__unknown_tool", ctx)
    assert deny is False
    assert reason == ""


# ---------------------------------------------------------------------------
# GitCommitGate
# ---------------------------------------------------------------------------

def _git_ctx(command: str) -> GateContext:
    return _ctx(tool_name="Bash", tool_input={"command": command})


def test_git_commit_gate_registered():
    assert "Bash" in GATES
    assert isinstance(GATES["Bash"], GitCommitGate)


def test_git_commit_denied_no_task_id():
    ctx = _git_ctx('git commit -m "fix: something"')
    deny, reason = GitCommitGate().verify(ctx)
    assert deny
    assert "task:<id>" in reason


def test_git_commit_allowed_with_task_id_in_body():
    ctx = _git_ctx('git commit -m "$(cat <<\'EOF\'\nfix: something\n\ntask:12168f99\nEOF\n)"')
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_git_commit_allowed_with_task_id_inline():
    ctx = _git_ctx('git commit -m "fix: something\n\ntask:abcdef12"')
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


# The git_local.sh gate cases were removed with the tool (task:3629ecbf).

def test_non_commit_bash_always_allowed():
    ctx = _git_ctx("ls -la /tmp")
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_git_status_bash_always_allowed():
    ctx = _git_ctx("git status --short")
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_git_commit_via_check_dispatch():
    ctx = _git_ctx('git commit -m "no task id here"')
    deny, reason = check("Bash", ctx)
    assert deny
    assert "task:<id>" in reason


def test_git_dash_C_commit_denied_no_task_id():
    """git -C <path> commit must be caught — real-world form used by Claude Code."""
    ctx = _git_ctx('git -C /Users/foo/workspace/claude-hooks commit -m "fix: something"')
    deny, reason = GitCommitGate().verify(ctx)
    assert deny
    assert "task:<id>" in reason


def test_git_dash_C_commit_allowed_with_task_id():
    ctx = _git_ctx(
        'git -C /Users/foo/workspace/claude-hooks commit -m "$(cat <<\'EOF\'\n'
        'fix: something\n\ntask:abcdef12\nEOF\n)"'
    )
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_git_dash_C_amend_denied_no_task_id():
    ctx = _git_ctx('git -C /path commit --amend -m "fix: something"')
    deny, _ = GitCommitGate().verify(ctx)
    assert deny


def test_git_dash_C_log_always_allowed():
    ctx = _git_ctx("git -C /path log --oneline -5")
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_git_add_and_commit_denied_no_task_id():
    """Compound add+commit command without task ID must be blocked."""
    ctx = _git_ctx(
        'git -C /path add file.py && git -C /path commit -m "$(cat <<\'EOF\'\nfix\nEOF\n)"'
    )
    deny, _ = GitCommitGate().verify(ctx)
    assert deny


def test_git_add_and_commit_allowed_with_task_id():
    ctx = _git_ctx(
        'git -C /path add file.py && git -C /path commit -m "$(cat <<\'EOF\'\nfix\n\ntask:abc12345\nEOF\n)"'
    )
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_git_commit_dash_f_allowed_when_file_has_task_id(tmp_path):
    """git commit -F <path> — task id lives in the file, not the command."""
    msg = tmp_path / "commit_msg.txt"
    msg.write_text("Subject line\n\ntask:97b365ac\n\nBody.\n")
    ctx = _git_ctx(f'git commit -F {msg}')
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_git_commit_dash_f_denied_when_file_has_no_task_id(tmp_path):
    msg = tmp_path / "commit_msg.txt"
    msg.write_text("Subject line\n\nBody with no task id.\n")
    ctx = _git_ctx(f'git commit -F {msg}')
    deny, reason = GitCommitGate().verify(ctx)
    assert deny
    assert "task:<id>" in reason


def test_git_commit_dash_f_denied_when_file_missing(tmp_path):
    """A nonexistent -F path must not crash the gate — it just can't confirm traceability."""
    ctx = _git_ctx(f'git commit -F {tmp_path / "nope.txt"}')
    deny, _ = GitCommitGate().verify(ctx)
    assert deny


def test_git_commit_dash_dash_file_long_flag_allowed(tmp_path):
    msg = tmp_path / "commit_msg.txt"
    msg.write_text("Subject\n\ntask:deadbeef\n")
    ctx = _git_ctx(f'git commit --file {msg}')
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_git_commit_dash_f_quoted_path_with_spaces_allowed(tmp_path):
    msg = tmp_path / "commit msg.txt"
    msg.write_text("Subject\n\ntask:cafebabe\n")
    ctx = _git_ctx(f'git commit -F "{msg}"')
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


# ---------------------------------------------------------------------------
# GitCommitMcpGate
# ---------------------------------------------------------------------------

def _mcp_git_ctx(task_id: str = "", message: str = "fix: something") -> GateContext:
    return _ctx(tool_name="git__commit", tool_input={"message": message, "task_id": task_id})


def test_git_commit_mcp_gate_registered():
    assert "git__commit" in GATES
    assert isinstance(GATES["git__commit"], GitCommitMcpGate)


def test_git_commit_mcp_denied_no_task_id():
    deny, reason = GitCommitMcpGate().verify(_mcp_git_ctx(task_id=""))
    assert deny
    assert "task_id" in reason


def test_git_commit_mcp_denied_whitespace_task_id():
    deny, _ = GitCommitMcpGate().verify(_mcp_git_ctx(task_id="   "))
    assert deny


def test_git_commit_mcp_allowed_with_task_id():
    deny, _ = GitCommitMcpGate().verify(_mcp_git_ctx(task_id="task:abc12345"))
    assert not deny


def test_git_commit_mcp_allowed_bare_id():
    deny, _ = GitCommitMcpGate().verify(_mcp_git_ctx(task_id="abc12345"))
    assert not deny


def test_git_commit_mcp_via_check_dispatch():
    deny, reason = check("git__commit", _mcp_git_ctx(task_id=""))
    assert deny
    assert "task_id" in reason


# ---------------------------------------------------------------------------
# Adversarial inputs — corrupted state, None values, malformed tool names
# ---------------------------------------------------------------------------

class TestGateAdversarialInputs:
    """Gate must fail-open (deny=False) or give a clean deny on all bad inputs.
    It must never raise an unhandled exception."""

    # -- None / missing fields -----------------------------------------------

    def test_none_tool_input_does_not_raise(self):
        ctx = _ctx("Bash", tool_input=None)
        deny, reason = check("Bash", ctx)
        # Must not raise — result can be deny or allow
        assert isinstance(deny, bool)

    def test_empty_prompt_id_does_not_raise(self):
        ctx = _ctx("Bash", prompt_id="")
        deny, reason = check("Bash", ctx)
        assert isinstance(deny, bool)

    # -- Odd tool names -----------------------------------------------------

    def test_extremely_long_tool_name_does_not_raise(self):
        tool = "mcp__local-mac__" + "a" * 500
        ctx = _ctx(tool)
        deny, reason = check(tool, ctx)
        # Unknown tool → always allow
        assert deny is False

    def test_empty_string_tool_name_does_not_raise(self):
        ctx = _ctx("")
        deny, reason = check("", ctx)
        assert deny is False

    def test_tool_name_with_special_chars_does_not_raise(self):
        tool = "mcp__local-mac__ma\x00il__compose"
        ctx = _ctx(tool)
        deny, reason = check(tool, ctx)
        assert isinstance(deny, bool)

    # -- Empty / minimal session state ---------------------------------------

    def test_gate_deny_reason_is_always_str(self):
        """reason must always be a str, never None."""
        for tool in ["Bash"]:
            ctx = _ctx(tool, tool_input={"command": "ls"})
            deny, reason = check(tool, ctx)
            assert isinstance(reason, str), f"{tool}: reason is {type(reason)}"

    # -- Fail-open guarantee -------------------------------------------------

    def test_unknown_gated_tool_name_always_allows(self):
        """Any tool not in GATES must be allowed — never accidentally blocked."""
        unknown_tools = [
            "mcp__local-mac__calendar__add_event",
            "mcp__local-mac__music__play",
            "mcp__local-mac__notes__add",
            "Bash",
            "Read",
            "Edit",
        ]
        for tool in unknown_tools:
            ctx = _ctx(tool)
            deny, reason = check(tool, ctx)
            assert deny is False, f"{tool} should not be gated but got deny=True: {reason}"


# ---------------------------------------------------------------------------
# GitCommitGate — commit DETECTION (task:e247268e)
#
# The words are assembled at runtime rather than written literally. A test file
# carrying a bare `git` + `commit` pair in its source is fine under pytest, but
# any Bash tool call that reads or greps this file gets inspected by the very
# gate under test — which is how this bug bit twice in one session.
# ---------------------------------------------------------------------------

_G = "g" + "it"
_C = "com" + "mit"


def test_detection_ignores_commit_word_in_echo_after_git_subcommand():
    """The reported bug: a status call plus an unrelated echo was denied."""
    ctx = _git_ctx(f'rm -rf some_dir\n{_G} status\necho "nothing to {_C}"')
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_detection_ignores_commit_word_across_and_operator():
    ctx = _git_ctx(f'{_G} log --oneline && echo "no {_C} here"')
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_detection_ignores_commit_word_in_shell_comment():
    ctx = _git_ctx(f'{_G} diff\n# TODO: {_C} later')
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_detection_ignores_commit_word_after_semicolon():
    ctx = _git_ctx(f'{_G} push origin main; echo "{_C} done"')
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_detection_ignores_git_commit_as_string_literal():
    """A script that merely CONTAINS the words runs no commit."""
    ctx = _git_ctx(f'python3 -c \'cases = ["{_G} {_C} -m x"]\'')
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_detection_ignores_tool_name_as_string_literal():
    ctx = _git_ctx('echo "git commit"')
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_detection_still_catches_commit_after_unrelated_leading_command():
    """Narrowing detection must not open a hole: a real commit later in a
    chain is still a commit."""
    ctx = _git_ctx(f'cd /tmp && {_G} {_C} -m "no id"')
    deny, reason = GitCommitGate().verify(ctx)
    assert deny
    assert "task:<id>" in reason


def test_detection_still_catches_commit_with_global_flag():
    ctx = _git_ctx(f'{_G} --no-pager {_C} -m "no id"')
    deny, _ = GitCommitGate().verify(ctx)
    assert deny


def test_detection_still_catches_commit_behind_wrapper():
    ctx = _git_ctx(f'sudo {_G} {_C} -m "no id"')
    deny, _ = GitCommitGate().verify(ctx)
    assert deny


def test_detection_survives_unbalanced_quotes_without_raising():
    """Heredocs split mid-quote; shlex raises, and the gate must degrade."""
    ctx = _git_ctx(f'{_G} {_C} -m "$(cat <<\'EOF\'\nsubject\nEOF\n)"')
    deny, _ = GitCommitGate().verify(ctx)
    assert deny  # detected as a commit, denied for want of a task id


# ---------------------------------------------------------------------------
# GitCommitGate — -F path RESOLUTION and --amend (task:ad9cae1c)
#
# Distinct from the detection block above. There, the question was whether a
# command is a commit at all. Here every case IS a commit; what is tested is
# whether the gate can find the task id that is genuinely present.
# ---------------------------------------------------------------------------

def test_dash_f_path_with_env_var_is_expanded(tmp_path, monkeypatch):
    """The gate sees the command before the shell expands it."""
    msg = tmp_path / "msg.txt"
    msg.write_text("subject\n\ntask:abcdef12\n")
    monkeypatch.setenv("GATE_MSG_DIR", str(tmp_path))
    ctx = _git_ctx(f'{_G} {_C} -F $GATE_MSG_DIR/msg.txt')
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_dash_f_path_with_braced_env_var_is_expanded(tmp_path, monkeypatch):
    msg = tmp_path / "msg.txt"
    msg.write_text("subject\n\ntask:abcdef12\n")
    monkeypatch.setenv("GATE_MSG_DIR", str(tmp_path))
    ctx = _git_ctx(f'{_G} {_C} -F ${{GATE_MSG_DIR}}/msg.txt')
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_dash_f_tilde_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "msg.txt").write_text("subject\n\ntask:abcdef12\n")
    ctx = _git_ctx(f'{_G} {_C} -F ~/msg.txt')
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_unreadable_dash_f_path_is_named_in_the_deny_reason():
    """An unopenable -F file must not look like a file with no task id."""
    ctx = _git_ctx(f'{_G} {_C} -F $NOT_SET_ANYWHERE/msg.txt')
    deny, reason = GitCommitGate().verify(ctx)
    assert deny
    assert "could not read" in reason
    assert "$NOT_SET_ANYWHERE/msg.txt" in reason
    assert "literal path" in reason


def test_dash_f_file_without_task_id_still_denies_without_the_note(tmp_path):
    """A readable file that genuinely lacks an id is the author's mistake, and
    must not be muddied with the unreadable-path guidance."""
    msg = tmp_path / "msg.txt"
    msg.write_text("subject with no reference\n")
    ctx = _git_ctx(f'{_G} {_C} -F {msg}')
    deny, reason = GitCommitGate().verify(ctx)
    assert deny
    assert "could not read" not in reason


def test_amend_reusing_head_message_with_task_id_is_allowed(tmp_path):
    """--amend --no-edit keeps HEAD's message, which the command never shows."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, check=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (repo / "f.txt").write_text("x")
    run("git", "add", "f.txt")
    run("git", "commit", "-q", "-m", "subject\n\ntask:abcdef12")

    ctx = _git_ctx(f'{_G} -C {repo} {_C} --amend --no-edit')
    deny, _ = GitCommitGate().verify(ctx)
    assert not deny


def test_amend_over_head_without_task_id_still_denies(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, check=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (repo / "f.txt").write_text("x")
    run("git", "add", "f.txt")
    run("git", "commit", "-q", "-m", "subject with no reference")

    ctx = _git_ctx(f'{_G} -C {repo} {_C} --amend --no-edit')
    deny, reason = GitCommitGate().verify(ctx)
    assert deny
    assert "task:<id>" in reason


def test_amend_against_unreadable_repo_fails_open_to_deny():
    """A bad -C path must not crash the gate; it just finds no message."""
    ctx = _git_ctx(f'{_G} -C /nonexistent/repo/path {_C} --amend --no-edit')
    deny, _ = GitCommitGate().verify(ctx)
    assert deny
