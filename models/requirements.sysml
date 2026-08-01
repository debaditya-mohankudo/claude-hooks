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
        doc /* Source: concept gates-commit-traceability (hooks/gates.py) +
             * commit-task-traceability-capture (src/tools/tasks.py).
             * Every version-control commit (Bash or MCP git__commit) must
             * include task:<id>; the commit_task_map table then records
             * which commit implemented which task, post-commit. */
    }
    satisfy requirement CommitTaskTraceabilityRequirement by system.hookServer;
    satisfy requirement CommitTaskTraceabilityRequirement by system.taskGraph;

    requirement def AdditiveOnlyMigrationRequirement {
        doc /* Source: concept db-schema-ddl-and-migrations invariant
             * (src/db/schema.py): "All migrations are additive-only
             * (ALTER TABLE ADD COLUMN) — never destructive." */
    }
    satisfy requirement AdditiveOnlyMigrationRequirement by system.foundation;

    requirement def LogReadViaMCPOnlyRequirement {
        doc /* Source: CLAUDE.md "Observability" section: "Always use the
             * MCP tool to read logs — never query the DB directly with
             * sqlite3" (mcp__claude-hooks__hooks__read_logs_sqlite). */
    }
    satisfy requirement LogReadViaMCPOnlyRequirement by system.mcpTools;

    requirement def CheckpointNonPersistenceRequirement {
        doc /* Source: CLAUDE.md "Development Workflow" section: the hook
             * server runs with MemorySaver (in-process, in-memory) after
             * two SqliteSaver corruption incidents (task:b3964f85).
             * Checkpoint state does NOT survive server restarts. */
    }
    satisfy requirement CheckpointNonPersistenceRequirement by system.pipeline;

    requirement def MemoryScoringPerDomainRequirement {
        doc /* Source: concept memory-scoring-per-domain-batch-limit
             * (langchain_learning/nodes/_memory_scoring.py): candidate
             * memory rows are fetched per-domain, not via one aggregate
             * query across the whole store — bounds cost per domain. */
    }
    satisfy requirement MemoryScoringPerDomainRequirement by system.stores;

    requirement def JiraHierarchyRequirement {
        doc /* Source: concept gates-jira-hierarchy (hooks/gates.py):
             * stories/tasks/bugs must have an epic parent — enforced on
             * tasks__create, not just a convention. */
    }
    satisfy requirement JiraHierarchyRequirement by system.taskGraph;
    satisfy requirement JiraHierarchyRequirement by system.hookServer;
}
