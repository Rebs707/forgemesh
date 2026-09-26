# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""BindingStore — persistence CRUD for terminology bindings."""
from __future__ import annotations

from semantic_mapper.db.binding_store import BindingStore
from semantic_mapper.ontology.terminology.models import ProposedBinding


def _store(tmp_path):
    return BindingStore(tmp_path / "bindings.db")


def _proposal(ind, iri, conf=0.9, status="PENDING_REVIEW"):
    return ProposedBinding(
        individual_uri=ind, raw_value="A1", source_id="sys", system_label="Sys",
        concept_iri=iri, confidence=conf, status=status,
    )


class TestBindingStore:
    def test_confirm_then_load(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.confirm("http://x/ind1", "http://c/1", "proj", "db",
                  source_id="sys", system_label="Sys", confidence=1.0)
        rows = s.load_confirmed("proj", "db")
        assert len(rows) == 1
        assert rows[0]["individual_uri"] == "http://x/ind1"
        assert rows[0]["concept_iri"] == "http://c/1"
        assert rows[0]["method"] == "human-confirmed"

    def test_confirm_replaces_prior_for_same_individual(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.confirm("http://x/ind1", "http://c/1", "proj", "db")
        s.confirm("http://x/ind1", "http://c/2", "proj", "db")
        rows = s.load_confirmed("proj", "db")
        assert len(rows) == 1
        assert rows[0]["concept_iri"] == "http://c/2"

    def test_load_confirmed_scoped_by_ontology_and_db(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.confirm("http://x/ind1", "http://c/1", "projA", "db")
        assert s.load_confirmed("projB", "db") == []
        assert len(s.load_confirmed("projA", "db")) == 1

    def test_replace_proposals_keeps_confirmed(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.confirm("http://x/conf", "http://c/9", "proj", "db")
        s.replace_proposals([_proposal("http://x/p1", "http://c/1")], "proj", "db")
        s.replace_proposals([_proposal("http://x/p2", "http://c/2")], "proj", "db")
        all_rows = s.fetch_all()
        statuses = {(r["individual_uri"], r["status"]) for r in all_rows}
        # Confirmed survives; only the latest pending set remains.
        assert ("http://x/conf", "CONFIRMED") in statuses
        assert ("http://x/p2", "PENDING_REVIEW") in statuses
        assert ("http://x/p1", "PENDING_REVIEW") not in statuses

    def test_count_and_clear(self, tmp_path) -> None:
        s = _store(tmp_path)
        s.confirm("http://x/ind1", "http://c/1", "proj", "db")
        assert s.count() == 1
        s.clear()
        assert s.count() == 0
