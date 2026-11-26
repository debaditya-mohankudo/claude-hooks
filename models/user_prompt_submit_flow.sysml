package UserPromptSubmitFlow {
    doc /* Sequence-diagram-equivalent for the pre-prompt hook flow.
         * SysML v2 textual notation has no distinct sequence-diagram
         * keyword; ordered actions + succession is the closest fit.
         * Ground truth: hooks/dispatcher.py:_handle_user_prompt_submit()
         * and hooks/session_store.py:begin_turn().
         * Correction: the epic named hooks/memory_loader_lc.py (an "LCEL
         * pipeline") but that file does not exist in this repo or on
         * this machine — it belongs to a separate global ~/.claude/
         * memory system. There is no graph either: a LangGraph StateGraph
         * stood here until task:31fb711d; the dispatcher calls the steps
         * directly. */

    action def UserPromptSubmitFlow {
        action clientForward {
            doc /* hooks/client.py forwards the hook payload to the FastAPI server. */
        }
        action serverRoute {
            doc /* hooks/server.py routes the POST to the dispatcher. */
        }
        action beginTurn {
            doc /* dispatcher._run_user_prompt_turn opens the turn in
                 * session_store (turn counter, prompt_id, Stop flags reset). */
        }
        action loadMemories {
            doc /* LoadMemoriesNode scores MEMORY.sqlite against the prompt, except
                 * prompts of three words or fewer and machine messages
                 * (load_memories.skip_reason, task:beaee05c). The
                 * task-context pipeline that once sat beside it
                 * (load_active_task, load_related_tasks, summarize_task_context,
                 * cwd_domain_detect, log_task_events) is gone (task:882d67fa
                 * and follow-ups) — task-framework owns active-task context
                 * and is asked via tasks__context. */
        }
        action loadVaultContext {
            doc /* dispatcher.py adds vault_context once memories are loaded:
                 * _load_vault_context() reads the fixed set of LIFE_OS files
                 * (_LIFE_OS_FILES — currently just dev_personality.md) verbatim,
                 * unscored, falling back to the last cached copy on a transient
                 * read failure. See requirements.sysml
                 * VaultContextTransientReadFailureRequirement. */
        }
        action enforceContextBudget {
            doc /* dispatcher.py trims context to fit the token/char budget. */
        }
        action formatSystemPrompt {
            doc /* dispatcher.py assembles additionalContext text from ctx:
                 * _format_system_prompt() renders the "## Dev personality" block
                 * (from vault_context) above "## Injected memories", then the
                 * scored memories and tool hints. */
        }
        action injectResponse {
            doc /* Response flows server.py -> client.py -> Claude Code, injecting the prompt. */
        }

        first clientForward then serverRoute;
        first serverRoute then beginTurn;
        first beginTurn then loadMemories;
        first loadMemories then loadVaultContext;
        first loadVaultContext then enforceContextBudget;
        first enforceContextBudget then formatSystemPrompt;
        first formatSystemPrompt then injectResponse;
    }
}
