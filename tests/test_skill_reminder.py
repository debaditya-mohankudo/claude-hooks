"""The skill reminder: one advisory note when a skill's action runs without it (task:df16e538)."""
import pytest

from hooks import dispatcher, skill_reminder as sr


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    sr._LOADED.clear()
    sr._SHOWN.clear()
    monkeypatch.setattr(sr.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(dispatcher, "_gate_check", lambda *a: (False, ""))


def pre(command, session="s1"):
    out = dispatcher._handle_pre_tool_use(
        {"tool_name": "Bash", "session_id": session, "tool_input": {"command": command}})
    return out["hookSpecificOutput"] if out else None


def note(command, session="s1"):
    out = pre(command, session)
    return out["additionalContext"] if out else ""


def load_by_tool(skill, session="s1", cwd=""):
    dispatcher._handle_post_tool_use(
        {"tool_name": "Skill", "session_id": session, "cwd": cwd, "tool_input": {"skill": skill}})


def write_skill(root, name, body):
    path = root / ".claude" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(body)


VAULT_HEREDOC = "mac vault write \"a/b.md\" \"$(cat <<'EOF'\n# t\nit's \"odd\nEOF\n)\""


@pytest.mark.parametrize("command,skill", [
    ("git add -A", "commit"),
    ("git commit -m 'task:1 x'", "commit"),
    ("cd /r && git -C /r -c a=b commit -F /tmp/m", "commit"),
    ("FOO=1 git add x && ls", "commit"),
    ("mac memory add slug --type feedback", "add-memory"),
    ("mac memory add_batch --memories-file /tmp/m.json", "add-memory"),
    ("~/bin/mac vault append n.md hi", "vault-save"),
    (VAULT_HEREDOC, "vault-save"),
])
def test_triggers_name_the_skill(command, skill):
    assert f"`{skill}` skill" in note(command)


@pytest.mark.parametrize("command", [
    "git status",
    "git log --grep commit",
    "echo \"run; git commit now\"",
    "grep 'mac memory add' f",
    "cat <<EOF\ngit commit -m x\nmac memory add y\nEOF",
    "git commit --help",
    "git add --dry-run .",
    "mac memory add --help",
    "mac vault write -h",
    "mac memory get slug",
    "mac vault read n.md",
])
def test_non_matches_are_silent(command):
    assert note(command) == ""


def test_note_never_decides_permission():
    assert "permissionDecision" not in pre("git add .")


def test_one_note_per_skill_per_session():
    assert note("git add .")
    assert note("git commit -m x") == ""
    assert "add-memory" in note("mac memory add s")
    assert note("git add .", session="s2")


def test_loaded_by_skill_tool_is_silent():
    load_by_tool("commit")
    assert note("git commit -m x") == ""


@pytest.mark.parametrize("prompt", ["/commit", "/commit the fix", "/plugin:commit"])
def test_loaded_by_typed_command_is_silent(prompt, monkeypatch):
    monkeypatch.setattr(dispatcher, "_run_user_prompt_turn", lambda *a: {"memories": []})
    dispatcher._handle_user_prompt_submit({"session_id": "s1", "prompt": prompt, "cwd": "/tmp"})
    assert note("git commit -m x") == ""


def test_prose_or_a_path_loads_nothing():
    sr.note_prompt("s1", "please run /commit")
    sr.note_prompt("s1", "/usr/local/commit is broken")
    assert note("git add .")


@pytest.mark.parametrize("where", ["home", "project"])
def test_a_skill_that_prescribes_vault_writes_suppresses_the_vault_note(tmp_path, where):
    root = tmp_path if where == "home" else tmp_path / "repo"
    write_skill(root, "market", "then `mac vault write PATH CONTENT`")
    load_by_tool("market", cwd=str(tmp_path / "repo" / "sub"))
    assert note("mac vault write a.md hi") == ""
    assert note("git add .")  # only the vault note is suppressed


def test_a_skill_without_vault_writes_does_not_suppress(tmp_path):
    write_skill(tmp_path, "other", "uses mac vault read")
    load_by_tool("other")
    assert "vault-save" in note("mac vault write a.md hi")


def test_denied_call_does_not_use_up_the_note(monkeypatch):
    monkeypatch.setattr(dispatcher, "_gate_check", lambda *a: (True, "no task id"))
    assert pre("git commit -m x")["permissionDecision"] == "deny"
    monkeypatch.setattr(dispatcher, "_gate_check", lambda *a: (False, ""))
    assert note("git commit -m x")


def test_a_note_never_sets_a_permission_decision():
    """An "allow" would approve the Bash call and skip the user's permission rules."""
    out = pre("git add .")
    assert out["additionalContext"] and "permissionDecision" not in out


def test_a_failing_check_gives_no_note(monkeypatch):
    monkeypatch.setattr(sr, "parse_bash", lambda c: 1 / 0)
    assert pre("git add .") is None
