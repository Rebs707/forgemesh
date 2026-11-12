# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Graph subject → gold row resolution via fm:bronzeSourceRef.

Covers entities whose subject URI is keyed on something other than the gold
id_column (e.g. no id column resolved, so the materialiser hashed the source
primary key out of _bronze_source_ref). The walker must still reach bronze.
"""
from __future__ import annotations

from rdflib import Graph, Literal, URIRef

from semantic_mapper.prove.lineage_walker import LineageWalker
from semantic_mapper.prove.subject_refs import BRONZE_SOURCE_REF, bronze_source_refs
from semantic_mapper.rdf.uri_factory import hash_identifier
from tests.prove.test_lineage_walker import BASE_URI, _make_bronze, _make_gold, _make_silver

# A subject URI keyed on something other than the gold id_column (here: a hash of
# a value no gold column holds), as when the materialiser falls back to a source key.
SUBJECT = f"{BASE_URI}admission/{hash_identifier('source-key-not-in-gold')}"


def _walker() -> LineageWalker:
    return LineageWalker(
        gold_engine=_make_gold(), silver_engine=_make_silver(),
        bronze_engine=_make_bronze(), base_uri=BASE_URI,
    )


def _sparql_over(graph: Graph):
    def run(query: str):
        return [{str(k): str(v) for k, v in row.asdict().items()} for row in graph.query(query)]
    return run


def test_uri_lookup_alone_breaks_when_uri_is_not_keyed_on_id_column():
    chains = _walker().walk_uris([SUBJECT], "gold_admission", "_gold_id")
    assert chains[0].broken


def test_bronze_ref_resolves_subject_to_bronze():
    refs = {SUBJECT: "raw_admissions.row_id=1"}
    chains = _walker().walk_uris([SUBJECT], "gold_admission", "_gold_id", bronze_refs=refs)
    ch = chains[0]
    assert not ch.broken
    assert ch.subject_uri == SUBJECT
    assert ch.gold.id == 1
    assert [s.table for s in ch.silver] == ["silver_admissions"]
    assert ch.bronze and ch.bronze[0].table == "raw_admissions"


def test_subjects_without_a_ref_fall_back_to_uri_parsing():
    plain = f"{BASE_URI}admission/42"
    chains = _walker().walk_uris(
        [SUBJECT, plain], "gold_admission", "subject_id",
        bronze_refs={SUBJECT: "raw_admissions.row_id=1"},
    )
    assert [c.broken for c in chains] == [False, False]


def test_unknown_ref_is_reported_broken():
    chains = _walker().walk_uris(
        [SUBJECT], "gold_admission", "_gold_id",
        bronze_refs={SUBJECT: "raw_admissions.row_id=999"},
    )
    assert chains[0].broken


def test_bronze_source_refs_reads_default_and_named_graphs():
    from rdflib import Dataset
    ds = Dataset()
    a, b, c = (URIRef(f"{BASE_URI}e/{n}") for n in "abc")
    ds.default_graph.add((a, URIRef(BRONZE_SOURCE_REF), Literal("raw_t.id=1")))
    ds.graph(URIRef("http://example.org/g")).add((b, URIRef(BRONZE_SOURCE_REF), Literal("raw_t.id=2")))
    refs = bronze_source_refs([str(a), str(b), str(c)], _sparql_over(ds), batch_size=2)
    assert refs == {str(a): "raw_t.id=1", str(b): "raw_t.id=2"}


def test_bronze_source_refs_never_raises():
    def boom(_q):
        raise RuntimeError("fuseki down")
    assert bronze_source_refs([f"{BASE_URI}e/a"], boom) == {}
