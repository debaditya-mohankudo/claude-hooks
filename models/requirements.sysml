package Requirements {
    doc /* Requirements traced from CLAUDE.md rules and concept_store
         * invariants (concept_store/concepts.json) to the subsystem model
         * (ClaudeHooksSystem, models/claude_hooks_system.sysml).
         *
         * Sourcing: each requirement below cites where it actually came
         * from — either a CLAUDE.md section or a concept's `invariants`
         * field — not invented. Traceability follows the epic's stated
         * deliverable: "requirements traced from CLAUDE.md rules to model
         * elements." concept_store invariants are included too since they
         * are finer-grained, code-verified constraints that CLAUDE.md's
         * prose rules often summarize (see task:dd8ac574 discussion:
         * the BDD is a curated summary of concept_store; this file is
         * where the dropped invariant detail comes back in). */

    private import ClaudeHooksSystem::*;

    part system : System;

    requirement def CommitTaskTraceabilityRequirement {
        doc /* Source: hooks/gates.py (GitCommitGate, GitCommitMcpGate).
             * Every version-control commit — Bash or the MCP git__commit
             * tool — must carry a task:<id> reference.
             *
             * The requirement is about the REFERENCE, not about any table.
             * An earlier revision allocated this to a second part as well,
             * because it also described commit_task_map recording the link
             * post-commit; that storage and its dispatcher-side capture were
             * removed with the task subsystem (task:87ec7876). Nothing was
             * lost: the reference in the message is the durable record, and
             * task-framework's backfill re-derives every link from git log
             * alone. Allocated to hookServer only — the gates are the whole
             * mechanism. */
    }
    satisfy requirement CommitTaskTraceabilityRequirement by system.hookServer;

    requirement def AdditiveOnlyMigrationRequirement {
        doc /* Source: concept db-schema-ddl-and-migrations invariant
             * (src/db/schema.py): "All migrations are additive-only
             * (ALTER TABLE ADD COLUMN) — never destructive." */
    }
    satisfy requirement AdditiveOnlyMigrationRequirement by system.foundation;

    requirement def LogReadViaMCPOnlyRequirement {
        doc /* Source: src/tools/hooks.py (handle_read_logs_sqlite). Logs are
             * read through the MCP tool, never by opening the database
             * directly: the tool applies the level/logger/search filtering and
             * the TOON encoding that the raw table does not, and the table is
             * a rolling window that auto-prunes, so a direct reader silently
             * sees a partial history. */
    }
    satisfy requirement LogReadViaMCPOnlyRequirement by system.mcpTools;

    requirement def CheckpointNonPersistenceRequirement {
        doc /* Source: hooks/server.py (lifespan, MemorySaver construction).
             * The graph is compiled with an in-process, in-memory
             * checkpointer, so checkpoint state does NOT survive a server
             * restart. This is chosen, not incidental: SqliteSaver was retired
             * after two corruption incidents (task:b3964f85), on the view that
             * losing a session's checkpoint is cheap and recoverable while a
             * corrupt one is neither. */
    }
    satisfy requirement CheckpointNonPersistenceRequirement by system.pipeline;

    requirement def MemoryScoringPerDomainRequirement {
        doc /* Source: concept memory-scoring-per-domain-batch-limit
             * (langchain_learning/nodes/_memory_scoring.py): candidate
             * memory rows are fetched per-domain, not via one aggregate
             * query across the whole store — bounds cost per domain. */
    }
    satisfy requirement MemoryScoringPerDomainRequirement by system.stores;

    requirement def VaultContextTransientReadFailureRequirement {
        doc /* Source: concept cache-store (hooks/cache_store.py) and
             * hooks/dispatcher.py (_load_vault_context). iCloud's file
             * provider briefly locks LIFE_OS vault files during sync,
             * raising OSError [Errno 11] EDEADLK; this fired on nearly
             * every prompt on 2026-08-20 and silently dropped vault
             * context. A transient read failure must fall back to the
             * last successfully cached content (in-process, per-name
             * registry in cache_store.py) rather than drop context, and
             * is logged at INFO/DEBUG once a cached value exists —
             * WARNING is reserved for a first-ever failed read with
             * nothing cached yet (task:a07b7f1b). */
    }
    satisfy requirement VaultContextTransientReadFailureRequirement by system.hookServer;

    // DELIBERATELY ABSENT — JiraHierarchyRequirement (stories/tasks/bugs must
    // have an epic parent) was removed with the gate that enforced it. The gate
    // went first (task:6240c675) because it registered under the bare name
    // tasks__create and so policed any MCP server providing that name; the
    // validation function followed with its last caller (task:87ec7876). Task
    // hierarchy is task-framework's rule to state, not this repo's. Removed
    // from the model by task:79d7aa34.
    //
    // DELIBERATELY ABSENT — no requirement covers repo_memory. Its per-turn
    // injection was removed by task:a7ddc132 and the directory deleted once it
    // proved to hold only stale bytecode.
}
