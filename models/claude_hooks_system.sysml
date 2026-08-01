package ClaudeHooksSystem {
    doc /* BDD-equivalent textual model of the claude-hooks agentic system,
         * derived from the subsystem inventory (task:b47afe5f). Four
         * subsystems, each composing the shared Foundation layer, composed
         * into a top-level System part.
         *
         * DELIBERATELY ABSENT — a fifth part, TaskGraph, modelled Jira-style
         * issue tracking over proj_tasks.db. Task tracking left this repo
         * (task:87ec7876, commit 060e823) and lives in task-framework, so the
         * part was retired (task:79d7aa34) rather than left describing a
         * subsystem this repo does not own. What survives here is residue, not
         * structure: one unused config field pointing at the old database, and
         * prose tombstones in the modules that used to read it. Do not re-add
         * a part for it — tests/test_models.py fails if the name returns. */

    private import Foundation::Foundation;

    part def HookServer {
        doc /* FastAPI server (hooks/server.py), event dispatcher, 3 gates,
             * session memory timeline. Owns the LangGraph checkpointer
             * lifecycle. */
        part foundation : Foundation;
    }

    part def MCPTools {
        doc /* FastMCP dispatcher (src/dispatcher.py), 6 domains / 25 actions
             * (hooks 4, memory 9, code_rag 3, diff_rag 3, think 1, scratch 5).
             *
             * The count is asserted against DOMAIN_MAP by tests/test_models.py
             * rather than left as prose: it read "9 domains / 56 actions" for a
             * month after the real figure fell, because nothing checked it. */
        part foundation : Foundation;
    }

    part def MemoryConceptRAGStores {
        doc /* Three distinct persistent stores: the memory store
             * (MEMORY.sqlite, BM25-scored, injected per turn), the concept
             * store (concept_store/concepts.json, architectural knowledge,
             * drift-checked on edit), and the RAG stores (code_rag, diff_rag,
             * over a shared TurboVec core).
             *
             * A fourth, repo_memory, was split out by task:850ddd65 and is
             * gone: task:a7ddc132 removed its per-turn injection, and the
             * directory held nothing but stale bytecode by the time it was
             * deleted (task:79d7aa34). */
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
        part stores : MemoryConceptRAGStores;
        part pipeline : LangGraphPipeline;
        part foundation : Foundation;
    }
}
