import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
import last_response as lr


def test_stop_overwrites_previous(tmp_path, monkeypatch):
    f = tmp_path / "last.md"
    monkeypatch.setenv("CLAUDE_LAST_RESPONSE", str(f))
    lr.update("Stop", {"last_assistant_message": "first"})
    lr.update("Stop", {"last_assistant_message": "second"})
    assert f.read_text() == "second\n"


def test_falls_back_to_transcript(tmp_path, monkeypatch):
    f = tmp_path / "last.md"
    monkeypatch.setenv("CLAUDE_LAST_RESPONSE", str(f))
    tr = tmp_path / "t.jsonl"
    tr.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant",
                  "content": [{"type": "text", "text": "from transcript"}]}}))
    lr.update("Stop", {"transcript_path": str(tr)})
    assert f.read_text() == "from transcript\n"


def test_ignores_other_events_and_empty_and_disabled(tmp_path, monkeypatch):
    f = tmp_path / "last.md"
    monkeypatch.setenv("CLAUDE_LAST_RESPONSE", str(f))
    lr.update("UserPromptSubmit", {"prompt": "x", "last_assistant_message": "no"})
    lr.update("Stop", {"last_assistant_message": ""})
    assert not f.exists()
    monkeypatch.setenv("CLAUDE_LAST_RESPONSE", "0")
    lr.update("Stop", {"last_assistant_message": "no"})
    assert not f.exists()
