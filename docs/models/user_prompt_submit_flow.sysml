package UserPromptSubmitFlow {
    doc /* Sequence-diagram-equivalent for the pre-prompt hook flow.
         * SysML v2 textual notation has no distinct sequence-diagram
         * keyword; ordered actions + succession is the closest fit.
         * Ground truth: hooks/dispatcher.py:_handle_user_prompt_submit()
         * and langchain_learning/session_graph.py:build_session_graph().
         * Correction: the epic named hooks/memory_loader_lc.py (an "LCEL
         * pipeline") but that file does not exist in this repo or on
         * this machine — it belongs to a separate global ~/.claude/
         * memory system. Modeled the real mechanism instead: a LangGraph
         * StateGraph, not LCEL. */

    action def UserPromptSubmitFlow {
        action clientForward {
            doc /* hooks/client.py forwards the hook payload to the FastAPI server. */
        }
        action serverRoute {
            doc /* hooks/server.py routes the POST to the dispatcher. */
        }
        action dispatcherCheckState {
            doc /* dispatcher.py reads existing checkpoint state before invoking the graph. */
        }
        action graphInvoke {
            doc /* session_graph.py:run_session() runs the StateGraph: load_turn
                 * -> load_active_task/load_related_tasks -> fan-out loaders ->
                 * summarize_task_context -> fan-out (cwd_domain_detect,
                 * load_memories, score_tools) -> set_prompt_id ->
                 * log_task_events -> END. */
        }
        action loadVaultContext {
            doc /* dispatcher.py adds vault_context after the graph returns. */
        }
        action enforceContextBudget {
            doc /* dispatcher.py trims context to fit the token/char budget. */
        }
        action formatSystemPrompt {
            doc /* dispatcher.py assembles additionalSystemPrompt text from ctx. */
        }
        action injectResponse {
            doc /* Response flows server.py -> client.py -> Claude Code, injecting the prompt. */
        }

        first clientForward then serverRoute;
        first serverRoute then dispatcherCheckState;
        first dispatcherCheckState then graphInvoke;
        first graphInvoke then loadVaultContext;
        first loadVaultContext then enforceContextBudget;
        first enforceContextBudget then formatSystemPrompt;
        first formatSystemPrompt then injectResponse;
    }
}
