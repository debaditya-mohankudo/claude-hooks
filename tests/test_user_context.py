"""Tests for hooks/user_context.py — the user-context ontology renderer."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import user_context as uc


_GRAPH = {
    "nodes": [
        {"id": "persona_dev", "label": "Dev persona", "type": "persona",
         "always_on": True, "render": "be terse", "definition": "working style"},
        {"id": "financial_profile", "label": "Financial profile", "type": "financial",
         "always_on": True, "definition": "retired, conservative"},
        {"id": "misc", "label": "Devices", "type": "hardware",
         "always_on": True, "definition": "macbook"},
        {"id": "domain_acme", "label": "ACME cert lifecycle", "type": "domain",
         "definition": "TLS cert lifecycle over ACME",
         "tags": "acme certificate tls renewal digicert"},
        {"id": "domain_taxonomy", "label": "Taxonomy", "type": "domain",
         "definition": "classification hierarchies",
         "tags": "taxonomy classification hierarchy vocabulary"},
    ]
}


# ── _render_always_on ────────────────────────────────────────────────────────

def test_always_on_renders_known_types_with_fixed_headings():
    out = uc._render_always_on(_GRAPH)
    assert "## Dev personality\nbe terse" in out
    assert "## Financial profile\nretired, conservative" in out
    # persona section precedes financial section
    assert out.index("## Dev personality") < out.index("## Financial profile")


def test_always_on_unknown_type_falls_back_to_label():
    out = uc._render_always_on(_GRAPH)
    assert "## Devices\nmacbook" in out


def test_always_on_uses_definition_when_no_render():
    node = {"nodes": [{"id": "x", "label": "X", "type": "financial",
                       "always_on": True, "definition": "def only"}]}
    assert uc._render_always_on(node) == "## Financial profile\ndef only"


def test_always_on_empty_when_no_flagged_nodes():
    assert uc._render_always_on({"nodes": [{"id": "a", "type": "domain"}]}) == ""


# ── _score_nodes ─────────────────────────────────────────────────────────────

def test_score_includes_node_at_or_above_min_tag_hits():
    out = uc._score_nodes(_GRAPH, {"acme", "renewal", "unrelated"})
    assert "## Relevant context" in out
    assert "**ACME cert lifecycle**: TLS cert lifecycle over ACME" in out
    assert "Taxonomy" not in out


def test_score_excludes_node_below_min_tag_hits():
    assert uc._score_nodes(_GRAPH, {"acme"}) == ""  # only 1 hit, need 2


def test_score_ranks_by_hit_count():
    out = uc._score_nodes(_GRAPH, {"acme", "certificate", "tls", "taxonomy", "classification"})
    assert out.index("ACME cert lifecycle") < out.index("Taxonomy")


def test_score_never_includes_always_on_nodes():
    g = {"nodes": [{"id": "p", "label": "P", "type": "persona", "always_on": True,
                    "definition": "d", "tags": "acme certificate tls"}]}
    assert uc._score_nodes(g, {"acme", "certificate", "tls"}) == ""


def test_score_empty_on_empty_prompt_tokens():
    assert uc._score_nodes(_GRAPH, set()) == ""


# ── render_user_context — composition + fallback ─────────────────────────────

def test_render_combines_always_on_and_scored(monkeypatch):
    monkeypatch.setattr(uc, "_load_graph", lambda: _GRAPH)
    out = uc.render_user_context({"acme", "certificate", "renewal"})
    assert out.index("## Dev personality") < out.index("## Relevant context")
    assert "ACME cert lifecycle" in out


def test_render_falls_back_to_dev_personality_md_when_graph_missing(monkeypatch, tmp_path):
    md = tmp_path / "dev_personality.md"
    md.write_text("# Dev personality\n\nbe terse\n")
    monkeypatch.setattr(uc, "_load_graph", lambda: None)
    monkeypatch.setattr(uc, "_FALLBACK_MD", md)
    out = uc.render_user_context(set())
    assert out == "## Dev personality\n\nbe terse"


def test_render_empty_when_graph_missing_and_no_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(uc, "_load_graph", lambda: None)
    monkeypatch.setattr(uc, "_FALLBACK_MD", tmp_path / "absent.md")
    assert uc.render_user_context(set()) == ""


# ── _load_graph — EDEADLK fallback to the persistent cache ───────────────────

class _BoomPath:
    """Stand-in for the ontology Path whose read_text raises EDEADLK."""
    def read_text(self, *a, **k):
        raise OSError(11, "Resource deadlock avoided")


def test_load_graph_uses_cached_json_on_read_error(monkeypatch):
    monkeypatch.setattr(uc, "_ONTOLOGY_PATH", _BoomPath())
    monkeypatch.setattr(uc, "_cache", {"json": json.dumps(_GRAPH)})
    assert uc._load_graph() == _GRAPH


def test_load_graph_returns_none_on_read_error_with_empty_cache(monkeypatch):
    monkeypatch.setattr(uc, "_ONTOLOGY_PATH", _BoomPath())
    monkeypatch.setattr(uc, "_cache", {})
    assert uc._load_graph() is None


def test_load_graph_returns_none_when_file_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(uc, "_ONTOLOGY_PATH", tmp_path / "nope.json")
    assert uc._load_graph() is None
