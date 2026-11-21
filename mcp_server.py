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


def _bootstrap() -> None:
    """Seed tool_hints keywords."""
    from logger import get_logger
    log = get_logger(__name__)

    # _ensure_ollama removed: it started an Ollama daemon at every MCP connect, and
    # nothing in the running server needs it (memory injection is BM25). Only the
    # standalone scripts/ embedding builders still use Ollama.
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
