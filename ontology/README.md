# Ontology

This directory holds `claude-hooks-domain.json`, the domain's ubiquitous
language: the nouns of claude-hooks (SessionStore, GateContext, GateCheck,
...) and the explicit, typed relations between them.

It exists to answer one question: **what is this thing, and what is it to the
others?** Not how it's implemented — what it *is*, in the vocabulary anyone
working on this project should share. That question is worth answering
separately from the code because the terms cut across files (a term like
SessionStore is read by several dispatcher handlers), and
across the project's other memory stores, which answer different questions:

- `concept_store/concepts.json` answers "what does this *module* promise" —
  architecture per file. Nothing drift-checks it since the drift hook was
  retired (task:85e6001f).
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

## What is checked

`tests/test_ontology.py` (task:e353061f) fails when a term's evidence file is
missing, when a cited symbol is not *defined* in its file (a mention in a
tombstone comment does not count), when a pointer is a bare line number, when
a term names an undeclared bounded context, or when a relation's endpoint is
not a defined term or its predicate is outside the list above.

Evidence format: `path:symbol`, comma-separated for several, or a bare `path`
to cite a whole file. Never `path:<line number>`.

What the test cannot check is whether a definition's *prose* is still true.
Re-read the cited code before trusting a term's wording.

See the root `CLAUDE.md` ("Where truth actually lives") for how this file
relates to `concept_store/concepts.json` and `models/*.sysml`.
