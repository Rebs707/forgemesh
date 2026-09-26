# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Integration test: verifies rdfs:comment coverage in the most recent
iterative-generated OWL file.

Skips when no rigor/iterative OWL file is found in the owl/ directory.
Fails with a detailed diagnostic when any class or property is missing
rdfs:comment, documenting the gap so it can be fixed in the generator.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import rdflib
from rdflib import OWL, RDF, RDFS, URIRef

# Project root is three levels up from this file (tests/ontology/test_owl_provenance.py)
_OWL_DIR = Path(__file__).resolve().parent.parent.parent / "owl"


def _find_latest_iterative_owl() -> Path | None:
    """Return the lexicographically latest *rigor* OWL file, or None."""
    if not _OWL_DIR.exists():
        return None
    candidates = sorted(
        _OWL_DIR.glob("*rigor*.owl"),
        key=lambda p: p.name,
    )
    # Exclude backup / before-mapping artefacts
    candidates = [
        p for p in candidates
        if not re.search(r"(bckup|backup|before)", p.name, re.IGNORECASE)
        and " " not in p.name
    ]
    return candidates[-1] if candidates else None


@pytest.fixture(scope="module")
def iterative_owl_graph():
    owl_path = _find_latest_iterative_owl()
    if owl_path is None:
        pytest.skip("No iterative (rigor) OWL file found in owl/ — skipping provenance check")
    g = rdflib.Graph()
    g.parse(str(owl_path), format="xml")
    return g, owl_path


class TestIterativeOwlProvenance:
    """Assert that every class and property in the latest iterative OWL has rdfs:comment."""

    def test_all_classes_have_rdfs_comment(self, iterative_owl_graph):
        g, owl_path = iterative_owl_graph
        classes = [s for s in g.subjects(RDF.type, OWL.Class) if isinstance(s, URIRef)]
        missing = [str(c) for c in classes if not list(g.objects(c, RDFS.comment))]
        assert not missing, (
            f"OWL file: {owl_path.name}\n"
            f"{len(missing)} class(es) missing rdfs:comment "
            f"(Issue 85 may be incomplete):\n"
            + "\n".join(f"  {u.split('#')[-1]}" for u in sorted(missing))
        )

    def test_all_properties_have_rdfs_comment(self, iterative_owl_graph):
        g, owl_path = iterative_owl_graph
        props = [
            s for ptype in (OWL.DatatypeProperty, OWL.ObjectProperty)
            for s in g.subjects(RDF.type, ptype)
            if isinstance(s, URIRef)
        ]
        missing = [str(p) for p in props if not list(g.objects(p, RDFS.comment))]
        assert not missing, (
            f"OWL file: {owl_path.name}\n"
            f"{len(missing)} property(ies) missing rdfs:comment "
            f"(Issue 85 may be incomplete):\n"
            + "\n".join(f"  {u.split('#')[-1]}" for u in sorted(missing))
        )

    def test_owl_file_is_non_empty(self, iterative_owl_graph):
        g, owl_path = iterative_owl_graph
        classes = list(g.subjects(RDF.type, OWL.Class))
        assert classes, f"OWL file {owl_path.name} contains no OWL classes"

    def test_owl_file_path_reported_on_failure(self, iterative_owl_graph):
        """Meta-test: confirm the fixture returns the expected file structure."""
        g, owl_path = iterative_owl_graph
        assert owl_path.suffix == ".owl"
        assert "rigor" in owl_path.name.lower()
