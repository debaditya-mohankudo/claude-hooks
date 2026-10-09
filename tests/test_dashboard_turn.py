import importlib.util, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
import dashboard_turn as dt

_spec = importlib.util.spec_from_file_location("dashboard", Path(__file__).parent.parent / "kitty" / "dashboard.py")
dash = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(dash)


def test_prompt_then_stop_from_transcript(tmp_path, monkeypatch):
    f = tmp_path / "turn.json"
    monkeypatch.setenv("CLAUDE_DASHBOARD_TURN", str(f))
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join(json.dumps(e) for e in [
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "old"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "final words"}]}},
    ]))
    dt.update("UserPromptSubmit", {"prompt": "do x"})
    assert json.loads(f.read_text()) == {"prompt": "do x", "summary": ""}
    dt.update("Stop", {"transcript_path": str(tr)})
    assert json.loads(f.read_text())["summary"] == "final words"
    out = dash.read_turn(f, 40)
    assert "do x" in out and "final words" in out


def test_next_shows_only_pending(tmp_path):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({"items": [{"text": "a", "done": True}, {"text": "b", "done": False}]}))
    out = dash.read_next(p, 40)
    assert "b" in out and "→ a" not in out
