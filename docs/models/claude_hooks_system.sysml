package ClaudeHooksSystem {
    doc /* BDD-equivalent textual model of the claude-hooks agentic system,
         * derived from the subsystem inventory (task:b47afe5f). Five
         * subsystems, each composing the shared Foundation layer, composed
         * into a top-level System part. */

    private import Foundation::Foundation;

    part def HookServer {
        doc /* FastAPI server (hooks/server.py), event dispatcher, 3 gates,
             * session memory timeline. Owns the LangGraph checkpointer
             * lifecycle. */
        part foundation : Foundation;
    }

    part def MCPTools {
        doc /* FastMCP dispatcher (src/dispatcher.py), 8 domains / 51 actions
             * (hooks, memory, tasks, code_rag, diff_rag, think, scratch,
             * concept). */
        part foundation : Foundation;
    }

    part def TaskGraph {
        doc /* Jira-style issue tracking backed by proj_tasks.db
             * (open_tasks, task_events, commit_task_map, task_edges).
             * Reflexive: this very epic is tracked here. */
        part foundation : Foundation;
    }

    part def MemoryConceptRAGStores {
        doc /* Three distinct persistent stores: memory store (MEMORY.sqlite,
             * BM25-scored), concept store (concepts.json, architectural
             * knowledge), and RAG stores (code_rag, diff_rag, shared
             * TurboVec core). */
        part foundation : Foundation;
    }

    part def LangGraphPipeline {
        doc /* StateGraph(SessionState) with ~28 nodes wired via explicit
             * add_edge calls, compiled with a checkpointer. Currently
             * MemorySaver (in-memory only) after a prior SqliteSaver
             * corrupted. */
        part foundation : Foundation;
    }

    part def System {
        part hookServer : HookServer;
        part mcpTools : MCPTools;
        part taskGraph : TaskGraph;
        part stores : MemoryConceptRAGStores;
        part pipeline : LangGraphPipeline;
        part foundation : Foundation;
    }
}
