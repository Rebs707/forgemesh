# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""TERM-5 — human-in-the-loop + warm reuse.

Proposed bindings default to PENDING_REVIEW; a confirmed binding persists and is
replayed at $0 on a second re-materialise without re-derivation.
"""
from __future__ import annotations

from rdflib import URIRef
from rdflib.namespace import SKOS

from semantic_mapper.db.binding_store import BindingStore
from semantic_mapper.ontology.terminology import bind_terminology

from .builders import (
    BASE,
    FM,
    build_vocab_graph,
    make_profile,
    make_settings,
    offline_source_config,
)

_METHOD = URIRef(FM + "terminologyBindingMethod")
_CODES = {"A1": "Alpha", "B2": "Beta"}


def _settings():
    return make_settings(sources=offline_source_config(codes=_CODES), overlap=0.6, floor=0.5)


class TestWarmReuse:
    def test_auto_proposals_default_to_pending_and_persist(self, tmp_path) -> None:
        store = BindingStore(tmp_path / "b.db")
        g, cls, inds = build_vocab_graph("raw_events", "status_code", "StatusCode",
                                         ["A1", "B2"])
        result = bind_terminology(g, [make_profile("raw_events", "status_code", ["A1", "B2"])],
                                  BASE, _settings(), store=store,
                                  ontology_source="proj", db_source="db")
        assert all(b.status == "PENDING_REVIEW" for b in result.asserted)
        pending = [r for r in store.fetch_all() if r["status"] == "PENDING_REVIEW"]
        assert len(pending) == 2

    def test_confirmed_binding_replayed_as_human_confirmed(self, tmp_path) -> None:
        store = BindingStore(tmp_path / "b.db")
        # First run discovers the individual's URI.
        g1, cls, inds = build_vocab_graph("raw_events", "status_code", "StatusCode",
                                          ["A1", "B2"])
        ind_a1 = str(inds["A1"])
        # A human confirms A1 → concept IRI.
        store.confirm(ind_a1, "http://example.org/concept/A1", "proj", "db",
                      source_id="testsys", system_label="TestSys")

        # Second re-materialise: fresh graph, same store.
        g2, _, inds2 = build_vocab_graph("raw_events", "status_code", "StatusCode",
                                         ["A1", "B2"])
        result = bind_terminology(g2, [make_profile("raw_events", "status_code", ["A1", "B2"])],
                                  BASE, _settings(), store=store,
                                  ontology_source="proj", db_source="db")
        a1 = next(b for b in result.asserted if b.individual_uri == ind_a1)
        assert a1.status == "CONFIRMED"
        assert a1.method == "human-confirmed"
        assert str(next(g2.objects(inds2["A1"], _METHOD))) == "human-confirmed"

    def test_confirmed_replayed_even_without_sources(self, tmp_path) -> None:
        # Warm reuse must not depend on re-deriving via the source: with no sources
        # configured, detection finds nothing, yet the confirmed binding is asserted.
        store = BindingStore(tmp_path / "b.db")
        g1, cls, inds = build_vocab_graph("raw_events", "status_code", "StatusCode",
                                          ["A1", "B2"])
        ind_a1 = str(inds["A1"])
        store.confirm(ind_a1, "http://example.org/concept/A1", "proj", "db",
                      source_id="testsys", system_label="TestSys", confidence=1.0)

        g2, _, inds2 = build_vocab_graph("raw_events", "status_code", "StatusCode",
                                         ["A1", "B2"])
        result = bind_terminology(
            g2, [make_profile("raw_events", "status_code", ["A1", "B2"])],
            BASE, make_settings(sources={}, floor=0.5), store=store,
            ontology_source="proj", db_source="db",
        )
        assert any(b.individual_uri == ind_a1 and b.status == "CONFIRMED"
                   for b in result.asserted)
        assert (inds2["A1"], SKOS.exactMatch,
                URIRef("http://example.org/concept/A1")) in g2
