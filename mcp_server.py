"""claude-hooks MCP server — self-contained tool server for task, memory, session, hooks tools."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
# task:53c9f817 — seed_all_tool_keywords() imports core.tool_registry (hooks/core/),
# same requirement LogToolUsageNode already has at hook-server runtime.
sys.path.insert(0, str(Path(__file__).parent / "hooks"))

from mcp.server import MCPServer
from src.dispatcher import build_dispatcher

mcp = MCPServer("claude-hooks")
build_dispatcher(mcp)


def _ensure_ollama() -> bool:
    """Start Ollama daemon if not already running. Returns True if ready."""
    import subprocess
    import time
    import urllib.request
    from logger import get_logger
    log = get_logger(__name__)

    try:
        urllib.request.urlopen("http://localhost:11434/", timeout=2)
        log.debug("[bootstrap] Ollama already running")
        return True
    except Exception:
        pass

    print("[claude-hooks] Ollama not running — starting daemon...", flush=True)
    log.info("[bootstrap] Ollama not running — starting daemon")
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(10):
            time.sleep(0.5)
            try:
                urllib.request.urlopen("http://localhost:11434/", timeout=1)
                print("[claude-hooks] Ollama started.", flush=True)
                log.info("[bootstrap] Ollama started")
                return True
            except Exception:
                pass
        print("[claude-hooks] Ollama did not respond after 5s — semantic search may be unavailable.", flush=True)
        log.warning("[bootstrap] Ollama did not respond after 5s")
        return False
    except FileNotFoundError:
        print("[claude-hooks] ollama binary not found — install via: brew install ollama", flush=True)
        log.warning("[bootstrap] ollama binary not found")
        return False


def _bootstrap() -> None:
    """Ensure Ollama is running, then seed tool_hints keywords."""
    from logger import get_logger
    log = get_logger(__name__)

    _ensure_ollama()

    # Task-index rebuild removed here (task:87ec7876, found post-deploy — this
    # import crashed every fresh claude-hooks MCP connection). rebuild_task_index
    # and _TASKS_TVIM lived in src/tools/tasks.py, deleted along with the whole
    # semantic-index capability task:d6ddb40f decided to drop: task-framework's
    # FTS index is written inside TaskStore.save(), so it cannot go stale the
    # way this rebuild-on-missing scheme could.

    try:
        from langchain_learning.nodes.log_tool_usage import seed_all_tool_keywords
        seed_all_tool_keywords()
    except Exception as exc:
        log.warning("[bootstrap] tool_hints keyword seeding failed: %s", exc)


_bootstrap()

if __name__ == "__main__":
    mcp.run()
