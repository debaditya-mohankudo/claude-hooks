"""mine_tool_sequences must be import-safe (task:f1fb2187): it rewrites the live
~/.claude/mcp-tools-domain.json, and an unguarded main() ran on `import`."""
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_import_does_not_run_main(tmp_path):
    # GRAPH is ~-relative. Under an empty HOME, main() would die opening the missing
    # graph, so a clean exit means importing ran nothing (and touched no live file).
    r = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import mine_tool_sequences"],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert not list(tmp_path.rglob("*"))   # nothing written under the fake HOME either
