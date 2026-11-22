import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
import dashboard_turn as dt

# The renderer cases (read_turn, read_next) moved to cli-graph with the viewer: task:3f30d74d.


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
