"""Ontology evidence test — ontology/claude-hooks-domain.json checked against the code.

The ontology is prose about the domain, and prose rots silently: a term cites
a symbol, the symbol is deleted, and nothing notices until someone acts on the
term. The 2026-11-23 architecture review found exactly that — a term citing a
removed ToolScorer, two terms citing a line past the end of dispatcher.py, and
a relation pointing at an undefined SourceModule (task:e353061f).

What this pins, per term and relation:
- every evidence file exists, and every cited symbol is *defined* in it
  (def / class / assignment — a mention in a tombstone comment does not count,
  which is why this is stricter than task-framework's substring check);
- no evidence pointer is a bare line number (line numbers drift on every edit);
- every term's bounded context is declared;
- every relation endpoint is a defined term, and every predicate is in the
  closed vocabulary ontology/README.md lists.

The checks are plain functions returning a list of problems, so the tests at
the bottom can prove each one fails on a bad ontology rather than trusting
that a green run means anything.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ONTOLOGY = ROOT / "ontology" / "claude-hooks-domain.json"

#: The predicate vocabulary ontology/README.md declares.
PREDICATES = {"is-a", "part-of", "relates", "persists", "references", "describes", "determines"}


def parse_evidence(evidence: str) -> list[tuple[str, str]]:
    """Split "path:symbol, path2:symbol2, path3" into (file, symbol) pairs.

    A chunk without ':' is a file cited as a whole (symbol "")."""
    pairs = []
    for chunk in evidence.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        path, _, symbol = chunk.partition(":")
        pairs.append((path.strip(), symbol.strip()))
    return pairs


def _defines(source: str, symbol: str) -> bool:
    name = re.escape(symbol)
    return re.search(
        rf"^\s*(?:async\s+def|def|class)\s+{name}\b|^\s*{name}\s*(?::[^=\n]*)?=",
        source, re.M) is not None


def evidence_problems(ontology: dict, root: Path = ROOT) -> list[str]:
    problems = []
    for name, term in ontology["terms"].items():
        pairs = parse_evidence(term.get("evidence", ""))
        if not pairs:
            problems.append(f"{name}: no evidence")
        for path, symbol in pairs:
            if symbol.isdigit():
                problems.append(f"{name}: {path}:{symbol} is a bare line number")
                continue
            file = root / path
            if not file.is_file():
                problems.append(f"{name}: cites missing file {path}")
                continue
            if symbol and not _defines(file.read_text(), symbol):
                problems.append(f"{name}: {path} does not define {symbol}")
    return problems


def structure_problems(ontology: dict) -> list[str]:
    problems = []
    contexts = set(ontology["bounded_contexts"])
    terms = set(ontology["terms"])
    for name, term in ontology["terms"].items():
        if term.get("bounded_context") not in contexts:
            problems.append(f"{name}: undeclared bounded context {term.get('bounded_context')!r}")
    for rel in ontology["relations"]:
        label = f"{rel.get('subject')} {rel.get('predicate')} {rel.get('object')}"
        for end in ("subject", "object"):
            if rel.get(end) not in terms:
                problems.append(f"relation {label}: {end} {rel.get(end)!r} is not a defined term")
        if rel.get("predicate") not in PREDICATES:
            problems.append(f"relation {label}: predicate not in {sorted(PREDICATES)}")
        if not rel.get("note"):
            problems.append(f"relation {label}: no note")
    return problems


# --- the real ontology --------------------------------------------------------

def _load() -> dict:
    return json.loads(ONTOLOGY.read_text())


def test_ontology_evidence_resolves_to_defined_symbols():
    assert evidence_problems(_load()) == []


def test_ontology_relations_and_contexts_are_defined():
    assert structure_problems(_load()) == []


# --- the checks themselves fail on bad input ---------------------------------

def _mini(evidence: str, relations: list[dict] | None = None, context: str = "C") -> dict:
    return {
        "bounded_contexts": {"C": "a context"},
        "terms": {"T": {"bounded_context": context, "definition": "d", "evidence": evidence}},
        "relations": relations or [],
    }


def test_missing_evidence_file_is_reported(tmp_path):
    assert evidence_problems(_mini("nope.py:f"), tmp_path) == ["T: cites missing file nope.py"]


def test_symbol_only_mentioned_in_a_comment_is_reported(tmp_path):
    (tmp_path / "m.py").write_text("# Gone was removed (task:x)\ndef other(): pass\n")
    assert evidence_problems(_mini("m.py:Gone"), tmp_path) == ["T: m.py does not define Gone"]


def test_defined_symbols_pass(tmp_path):
    (tmp_path / "m.py").write_text(
        "class A:\n    pass\nasync def b(): pass\n    def c(self): pass\nD = 1\nE: dict = {}\n")
    assert evidence_problems(_mini("m.py:A, m.py:b, m.py:c, m.py:D, m.py:E, m.py"), tmp_path) == []


def test_bare_line_number_is_reported(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    assert evidence_problems(_mini("m.py:669"), tmp_path) == ["T: m.py:669 is a bare line number"]


def test_undefined_relation_endpoint_is_reported():
    rel = {"subject": "T", "predicate": "describes", "object": "SourceModule", "note": "n"}
    assert structure_problems(_mini("m.py", [rel])) == [
        "relation T describes SourceModule: object 'SourceModule' is not a defined term"]


def test_unknown_predicate_and_missing_note_are_reported():
    rel = {"subject": "T", "predicate": "owns", "object": "T"}
    problems = structure_problems(_mini("m.py", [rel]))
    assert len(problems) == 2
    assert "predicate not in" in problems[0] and "no note" in problems[1]


def test_undeclared_bounded_context_is_reported():
    assert structure_problems(_mini("m.py", context="Nope")) == ["T: undeclared bounded context 'Nope'"]
