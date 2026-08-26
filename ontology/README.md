# Ontology

This directory holds `claude-hooks-domain.json`, the domain's ubiquitous
language: the nouns of claude-hooks (SessionState, GateContext, NodeRegistry,
...) and the explicit, typed relations between them.

It exists to answer one question: **what is this thing, and what is it to the
others?** Not how it's implemented — what it *is*, in the vocabulary anyone
working on this project should share. That question is worth answering
separately from the code because the terms cut across files (a term like
SessionState is written and read by nearly every node in the graph), and
across the project's other memory stores, which answer different questions:

- `concept_store/concepts.json` answers "what does this *module* promise" —
  architecture per file, drift-checked on every edit.
- `ontology/claude-hooks-domain.json` answers "what is this *term*, and how
  does it relate to the rest" — vocabulary per domain concept.

## Shape

- **`bounded_contexts`** — the few distinct sub-domains the terms fall into
  (e.g. Hook Dispatch vs. Gates/Enforcement), each with a one-line description
  of what separates it from the others.
- **`terms`** — one entry per domain noun: which bounded context it belongs
  to, and a plain-language definition of what it is (including things that
  are *not* distinct things — e.g. an Epic in task-framework is just a Task
  with a certain field set, not a separate class; the equivalent discipline
  applies here).
- **`relations`** — typed, directional statements connecting two terms, using
  a small fixed vocabulary of predicates: `is-a`, `part-of`, `relates`,
  `persists`, `references`, `describes`, `determines`. Each relation carries a
  note explaining *why* the relationship has the shape it does, not just that
  it exists.

## What this file is not

It is not a schema, an API reference, or a data model — no function
signatures, no formulas, no storage internals. Those live in the code and in
`concept_store/concepts.json`. This file only ever answers "what is the
concept, and how does it relate to the other concepts" — technical detail
belongs elsewhere, and a term's `evidence` field is a single pointer for
verification, not an implementation trail to restate in prose.

It is also a map, not a checked claim: nothing currently tests it against the
code (unlike `concept_store/concepts.json`, which is drift-checked). Treat it
as a durable but driftable snapshot of the domain vocabulary, kept only as
accurate as whoever last updated it made it — re-verify evidence against
source before trusting a term, per the root `CLAUDE.md`.

See the root `CLAUDE.md` ("Where truth actually lives") for how this file
relates to `concept_store/concepts.json` and `models/*.sysml`.
