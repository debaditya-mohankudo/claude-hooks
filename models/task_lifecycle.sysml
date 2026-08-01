package TaskLifecycle {
    doc /* State machine for the task graph subsystem's lifecycle
         * (src/tools/tasks.py: _VALID_STATUSES, _TRANSITIONS,
         * is_valid_transition). Ground truth, not inferred: transcribed
         * directly from the transitions table in code, not guessed.
         *
         * New tasks always start in openState (handle_create). done and
         * abandonedState are terminal (empty transition sets in
         * _TRANSITIONS). Any state can transition to abandonedState —
         * special-cased in is_valid_transition() rather than listed
         * per-state, modeled here as explicit transitions from every
         * non-terminal state for clarity.
         *
         * Not modeled: parent auto-close-on-all-subtasks-done is a
         * cross-task side effect (handle_finish), not a same-task
         * transition, so it doesn't belong in this state machine. */

    state def TaskLifecycle {
        entry;

        state openState;
        state blockedState;
        state doneState;
        state abandonedState;

        transition openToDone
            first openState
            then doneState;

        transition openToBlocked
            first openState
            then blockedState;

        transition blockedToOpen
            first blockedState
            then openState;

        transition openToAbandoned
            first openState
            then abandonedState;

        transition blockedToAbandoned
            first blockedState
            then abandonedState;
    }
}
