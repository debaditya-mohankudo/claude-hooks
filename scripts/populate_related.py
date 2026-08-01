"""One-time script: populate related column in MEMORY.sqlite for claude-hooks memories."""
import sqlite3
from pathlib import Path

MEMORY_DB = Path.home() / ".claude" / "MEMORY.sqlite"

RELATED: dict[str, str] = {
    "claude-hooks-gate-framework":         "claude-hooks-current-gates,claude-hooks-gate-prereq-tracking,gate-policy-boundary-in-node",
    "claude-hooks-current-gates":          "claude-hooks-gate-framework,claude-hooks-gate-prereq-tracking,dev-gate-removal-task-close-gotcha",
    "claude-hooks-gate-prereq-tracking":   "claude-hooks-gate-framework,claude-hooks-current-gates",
    "gate-policy-boundary-in-node":        "claude-hooks-gate-framework,claude-hooks-node-design,protocol-as-pipe-joint",
    "dev-gate-removal-task-close-gotcha":  "claude-hooks-current-gates,claude-hooks-deploy-workflow",
    "claude-hooks-ups-pipeline":           "claude-hooks-graph-event-routing,claude-hooks-node-design,claude-hooks-session-lifecycle,langgraph-checkpointer-pattern,claude-hooks-hook-delivery",
    "claude-hooks-graph-event-routing":    "claude-hooks-ups-pipeline,claude-hooks-node-design,claude-hooks-posttooluse-bridge,claude-hooks-hook-delivery",
    "claude-hooks-node-design":            "claude-hooks-ups-pipeline,claude-hooks-graph-event-routing,gate-policy-boundary-in-node,protocol-as-pipe-joint",
    "langgraph-checkpointer-pattern":      "claude-hooks-session-lifecycle,checkpoint-is-ipc,claude-hooks-ups-pipeline",
    "claude-hooks-session-lifecycle":      "langgraph-checkpointer-pattern,claude-hooks-ups-pipeline,claude-hooks-stop-vs-sessionend,checkpoint-is-ipc",
    "classify-domain-node-design":         "claude-hooks-domain-detection,claude-hooks-node-design,domain-classifier-llm-when",
    "claude-hooks-domain-detection":       "classify-domain-node-design,domain-classifier-llm-when",
    "claude-hooks-load-memories-node":     "memory-add-batch-prefer-over-serial,tool-hints-saturation-problem,memory-search-slug-normalization,memory-atomicity-audit-pattern,claude-hooks-load-vault-context,task-framework-memory-delegation",
    "memory-add-batch-prefer-over-serial": "claude-hooks-load-memories-node,memory-atomicity-audit-pattern",
    "memory-search-slug-normalization":    "claude-hooks-load-memories-node",
    "memory-atomicity-audit-pattern":      "claude-hooks-load-memories-node,memory-add-batch-prefer-over-serial,file-token-matching-stem-only,sqlite-idempotent-column-migration,invisible-process-principle",
    "sqlite-idempotent-column-migration":  "memory-atomicity-audit-pattern,claude-hooks-databases,tasks-migrate-check-correct-table",
    "file-token-matching-stem-only":       "memory-atomicity-audit-pattern",
    "claude-hooks-task-state-machine":     "tasks-active-status-checkpoint-only,claude-hooks-posttooluse-bridge,claude-hooks-task-body-gate,claude-hooks-type-vocabulary",
    "claude-hooks-posttooluse-bridge":     "claude-hooks-task-state-machine,tasks-active-status-checkpoint-only,claude-hooks-graph-event-routing",
    "tasks-active-status-checkpoint-only": "claude-hooks-task-state-machine,claude-hooks-posttooluse-bridge,tasks-ui-live-session-gate",
    "activate-task-before-work":           "always-activate-task-after-create,task-framework-memory-delegation,small-task-use-existing-parent",
    "always-activate-task-after-create":   "activate-task-before-work,task-framework-memory-delegation",
    "task-always-add-completion-memory":   "task-body-checklist-for-steps,activate-task-before-work",
    "task-body-checklist-for-steps":       "claude-hooks-task-body-gate,task-title-keyword-quality,task-always-add-completion-memory",
    "claude-hooks-task-body-gate":         "task-body-checklist-for-steps,claude-hooks-task-state-machine",
    "task-title-keyword-quality":          "task-body-checklist-for-steps,claude-hooks-related-tasks-node",
    "small-task-use-existing-parent":      "activate-task-before-work,task-title-keyword-quality",
    "task-framework-memory-delegation":    "activate-task-before-work,always-activate-task-after-create,claude-hooks-load-memories-node",
    "tasks-ui-live-session-gate":          "tasks-active-status-checkpoint-only,claude-hooks-server-identity,claude-hooks-jinja2-ui-reference",
    "tasks-migrate-check-correct-table":   "sqlite-idempotent-column-migration",
    "replay-session-ui-filter":            "claude-hooks-replay-harness,tasks-ui-live-session-gate",
    "claude-hooks-worktree-dev":           "claude-hooks-worktree-test,claude-hooks-deploy-workflow,feedback-fix-in-dev-first",
    "claude-hooks-worktree-test":          "claude-hooks-worktree-dev,claude-hooks-deploy-workflow,claude-hooks-server-process",
    "claude-hooks-deploy-workflow":        "claude-hooks-worktree-dev,claude-hooks-worktree-test,claude-hooks-server-deploy-restart,feedback-fix-in-dev-first",
    "claude-hooks-server-deploy-restart":  "claude-hooks-deploy-workflow,launchd-plist-reload-required,claude-hooks-server-process",
    "feedback-fix-in-dev-first":           "claude-hooks-worktree-dev,claude-hooks-deploy-workflow",
    "launchd-plist-reload-required":       "claude-hooks-server-deploy-restart,claude-hooks-server-process",
    "server-project-root-hardcoded":       "hooks-paths-module,claude-hooks-key-files",
    "claude-hooks-server-process":         "claude-hooks-worktree-test,claude-hooks-server-deploy-restart,claude-hooks-server-identity,launchd-plist-reload-required",
    "claude-hooks-server-identity":        "claude-hooks-server-process,claude-hooks-databases,server-memory-no-assistant-turns",
    "claude-hooks-databases":              "claude-hooks-key-files,hooks-paths-module,claude-hooks-server-identity,sqlite-idempotent-column-migration,claude-hooks-mcp-hooks-boundary",
    "claude-hooks-key-files":             "claude-hooks-databases,hooks-paths-module,claude-hooks-hook-delivery",
    "hooks-paths-module":                  "claude-hooks-key-files,claude-hooks-databases,server-project-root-hardcoded",
    "claude-hooks-hook-delivery":          "claude-hooks-key-files,claude-hooks-ups-pipeline,claude-hooks-graph-event-routing,claude-hooks-hook-payload-shape",
    "claude-hooks-related-tasks-node":     "claude-hooks-replay-harness,claude-hooks-load-memories-node,task-title-keyword-quality",
    "claude-hooks-replay-harness":         "claude-hooks-related-tasks-node,exclude-active-session-from-live-index-baseline,replay-baseline-recapture-after-memory-add,replay-session-ui-filter",
    "claude-hooks-code-rag-scope-decision":"gitignore-rag-files,prefer-commit-rag-over-git,docstrings-improve-rag-chunks",
    "gitignore-rag-files":                 "claude-hooks-code-rag-scope-decision",
    "exclude-active-session-from-live-index-baseline": "claude-hooks-replay-harness,replay-baseline-recapture-after-memory-add",
    "replay-baseline-recapture-after-memory-add": "claude-hooks-replay-harness,exclude-active-session-from-live-index-baseline",
    "prefer-commit-rag-over-git":          "claude-hooks-code-rag-scope-decision",
    "claude-hooks-tool-tracking":          "claude-hooks-mcp-hooks-boundary,tool-hints-saturation-problem,claude-hooks-hook-payload-shape",
    "claude-hooks-mcp-hooks-boundary":     "claude-hooks-tool-tracking,claude-hooks-databases,claude-hooks-db-ownership",
    "tool-hints-saturation-problem":       "claude-hooks-tool-tracking,claude-hooks-load-memories-node",
    "claude-hooks-hook-payload-shape":     "claude-hooks-tool-tracking,claude-hooks-hook-delivery",
    "claude-hooks-mission":                "claude-hooks-constraints,claude-hooks-observability-principle,claude-hooks-recency-pull,langchain-vs-claude-code-inversion",
    "claude-hooks-constraints":            "claude-hooks-mission,claude-hooks-observability-principle,hook-dev-mode",
    "claude-hooks-observability-principle":"claude-hooks-mission,claude-hooks-constraints,logs-read-tool",
    "claude-hooks-recency-pull":           "claude-hooks-mission",
    "langchain-vs-claude-code-inversion":  "claude-hooks-mission,claude-hooks-hybrid-model-strategy,langchain-subprocess-runnable",
    "claude-hooks-hybrid-model-strategy":  "langchain-vs-claude-code-inversion,domain-classifier-llm-when,classify-domain-node-design",
    "protocol-as-pipe-joint":              "gate-policy-boundary-in-node,claude-hooks-node-design,protocol-extraction-reduces-node-code",
    "checkpoint-is-ipc":                   "langgraph-checkpointer-pattern,claude-hooks-session-lifecycle,session-id-how-to",
    "claude-hooks-test-isolation":         "hard-to-test-means-bad-structure,mock-cfg-must-patch-retriever-config",
    "hard-to-test-means-bad-structure":    "claude-hooks-test-isolation,protocol-extraction-reduces-node-code",
    "retrievers-py-import-safety-rule":    "retrievers-lazy-import-pattern",
    "retrievers-lazy-import-pattern":      "retrievers-py-import-safety-rule",
    "mock-cfg-must-patch-retriever-config":"claude-hooks-test-isolation,retriever-top-n-none-not-hardcoded",
    "retriever-top-n-none-not-hardcoded":  "mock-cfg-must-patch-retriever-config",
    "protocol-extraction-reduces-node-code":"protocol-as-pipe-joint,hard-to-test-means-bad-structure",
    "claude-hooks-stop-vs-sessionend":     "claude-hooks-session-lifecycle,claude-hooks-reliable-hook-placement",
    "claude-hooks-reliable-hook-placement":"claude-hooks-stop-vs-sessionend,claude-hooks-hook-delivery",
    "session-id-how-to":                   "checkpoint-is-ipc,langgraph-checkpointer-pattern",
    "domain-classifier-llm-when":          "claude-hooks-domain-detection,classify-domain-node-design,claude-hooks-hybrid-model-strategy",
    "langchain-subprocess-runnable":       "langchain-vs-claude-code-inversion",
    "docstrings-improve-rag-chunks":       "claude-hooks-code-rag-scope-decision",
    "claude-hooks-skill-mcp-pattern":      "claude-hooks-mcp-hooks-boundary",
    "claude-hooks-wiki-pattern":           "claude-hooks-docs-in-repo,claude-hooks-readme-style",
    "claude-hooks-docs-in-repo":           "claude-hooks-wiki-pattern,claude-hooks-code-rag-scope-decision",
    "claude-hooks-readme-style":           "claude-hooks-wiki-pattern",
    "claude-hooks-type-vocabulary":        "claude-hooks-task-state-machine",
    "claude-hooks-db-ownership":           "claude-hooks-mcp-hooks-boundary,claude-hooks-databases",
    "claude-hooks-review-system":          "claude-hooks-task-state-machine",
    "gc-skill-task-id-in-commit":          "claude-hooks-commit-gate",
    "claude-hooks-commit-gate":            "gc-skill-task-id-in-commit,claude-hooks-gate-framework",
    "hook-dev-mode":                       "claude-hooks-constraints,claude-hooks-hook-delivery",
    "server-memory-no-assistant-turns":    "claude-hooks-server-identity",
    "logs-read-tool":                      "claude-hooks-observability-principle",
    "invisible-process-principle":         "memory-atomicity-audit-pattern",
    "claude-hooks-load-vault-context":     "claude-hooks-ups-pipeline,claude-hooks-load-memories-node",
    "claude-hooks-jinja2-ui-reference":    "tasks-ui-live-session-gate,claude-hooks-server-identity",
    "memory-add-batch-preserve-body":      "memory-atomicity-audit-pattern,memory-add-batch-prefer-over-serial,sqlite-idempotent-column-migration",
}


def main() -> None:
    con = sqlite3.connect(str(MEMORY_DB))
    updated = 0
    for name, related in RELATED.items():
        cur = con.execute(
            "UPDATE memories SET related=? WHERE name=? AND domain='claude-hooks'",
            (related, name),
        )
        if cur.rowcount:
            updated += 1
    con.commit()
    con.close()
    print(f"✓ populated related on {updated}/{len(RELATED)} memories")


if __name__ == "__main__":
    main()
