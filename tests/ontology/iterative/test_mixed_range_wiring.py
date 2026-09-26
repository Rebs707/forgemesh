# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
End-to-end wiring test for the MIXED_RANGE resolver.

This module proves the missing link: that IterativeOntologyGenerator.generate() actually
*invokes* the resolver in its post-loop and that a property generated with a
MIXED_RANGE defect comes out resolved — and that the coherence-queue sidecar is
written.
"""
import json
from unittest.mock import MagicMock, patch

from rdflib import OWL, RDF, RDFS, Graph, URIRef
from rdflib.namespace import XSD

from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.ontology.iterative.generator import (
    IterativeOntologyGenerator,
    _write_coherence_queue,
)
from semantic_mapper.ontology.iterative.graph_merger import MixedRangeResolution
from semantic_mapper.ontology.iterative.models import JudgeVerdict
from semantic_mapper.ontology.range_coherence_check import check_range_coherence, summarise

BASE = "http://mesh.local/ontology/mimic#"

# raw_patients is a dimension → topo-sorted first → declares :Patient.
_PATIENTS_FRAGMENT = """\
Prefix: owl: <http://www.w3.org/2002/07/owl#>
Prefix: rdfs: <http://www.w3.org/2000/01/rdf-schema#>
Prefix: xsd: <http://www.w3.org/2001/XMLSchema#>
Prefix: : <http://mesh.local/ontology/mimic#>

Class: :Patient
    Annotations: rdfs:label "Patient"@en
"""

# raw_events.patient_id is a real FK, but the model (deliberately, for the test)
# emits hasPatient with BOTH a class range (:Patient) and an xsd range (integer) —
# the MIXED_RANGE defect the resolver must repair (tier R1: keep class, drop scalar).
_EVENTS_FRAGMENT = """\
Prefix: owl: <http://www.w3.org/2002/07/owl#>
Prefix: rdfs: <http://www.w3.org/2000/01/rdf-schema#>
Prefix: xsd: <http://www.w3.org/2001/XMLSchema#>
Prefix: : <http://mesh.local/ontology/mimic#>

Class: :Event
    Annotations: rdfs:label "Event"@en

ObjectProperty: :hasPatient
    Annotations: rdfs:label "has patient"@en
    Domain: :Event
    Range: :Patient
    Range: xsd:integer
"""


def _make_settings(tmp_owl_dir):
    s = MagicMock()
    s.OPENAI_API_KEY = "test-key"
    s.LLM_REQUEST_TIMEOUT = 30
    s.LLM_STREAM = False
    s.ONTOLOGY_GENERATOR = "iterative"
    s.ONTOLOGY_GEN_MODEL = "gpt-4o"
    s.ONTOLOGY_JUDGE_MODEL = "gpt-4o-mini"
    s.ONTOLOGY_GEN_MAX_RETRIES = 2
    s.ONTOLOGY_GEN_TOP_K_CLASSES = 10
    s.ONTOLOGY_GEN_TEMPERATURE = 0.1
    s.ONTOLOGY_GEN_JUDGE_TEMPERATURE = 0.0
    s.ONTOLOGY_JUDGE_CONFIDENCE_FLOOR = 0.6
    s.ONTOLOGY_MAX_ROWS_FOR_ENUMERATION = 1000
    s.ONTOLOGY_MAX_DISTINCT_FOR_ENUMERATION = 20
    s.ONTOLOGY_MAX_DISTINCT_FOR_OPEN_CLASS = 100
    s.ONTOLOGY_CATEGORICAL_MAX_NULL_RATE = 0.90
    s.ONTOLOGY_CATEGORICAL_MIN_SUPPORT = 0
    s.ONTOLOGY_CATEGORICAL_MAX_UNIQUENESS = 1.0
    s.ONTOLOGY_MIN_ROWS_FOR_GENERATION = 0
    s.ONTOLOGY_COL_WORD_SPLITS = {}
    s.SEMANTIC_MATCHER_MODEL = "all-MiniLM-L6-v2"
    s.DATABASE_URL = "mysql://root@localhost/mimic"
    s.ONTOLOGY_NAMESPACE = ""
    s.SEED_OUTPUT_PATH = str(tmp_owl_dir / "seed.owl")
    s.OPENAI_MODEL = "gpt-4o"
    return s


def _make_profiles():
    return [
        TableProfile(
            table_name="raw_patients", row_count=1000,
            columns={
                "subject_id": ColumnProfile(
                    name="subject_id", inferred_type="INTEGER", is_nullable=False,
                    is_primary_key=True, null_rate=0.0, distinct_count=1000),
            },
            foreign_keys=[],
        ),
        TableProfile(
            table_name="raw_events", row_count=5000,
            columns={
                "event_id": ColumnProfile(
                    name="event_id", inferred_type="INTEGER", is_nullable=False,
                    is_primary_key=True, null_rate=0.0, distinct_count=5000),
                "patient_id": ColumnProfile(
                    name="patient_id", inferred_type="INTEGER", is_nullable=False,
                    is_primary_key=False, null_rate=0.0, distinct_count=900),
            },
            foreign_keys=[{"column": "patient_id", "ref_table": "raw_patients"}],
        ),
    ]


class TestMixedRangeWiredIntoGenerator:
    def test_generate_resolves_mixed_range_and_writes_queue(self, tmp_path):
        gen = IterativeOntologyGenerator(_make_settings(tmp_path))

        with patch("openai.OpenAI"):
            # fragments returned in topological order: raw_patients, then raw_events
            with patch.object(gen, "_call_gen_llm",
                              side_effect=[_PATIENTS_FRAGMENT, _EVENTS_FRAGMENT]):
                with patch.object(gen, "_call_judge_llm",
                                  return_value=JudgeVerdict(approved=True, confidence=0.9)):
                    out = gen.generate(_make_profiles())

        g = Graph()
        g.parse(str(out), format="xml")

        # 1. The finished ontology has ZERO mixed ranges.
        assert summarise(check_range_coherence(g, BASE)).get("MIXED_RANGE", 0) == 0

        # 2. hasPatient was resolved the R1 way: ObjectProperty, range :Patient only,
        #    the stray xsd:integer dropped.
        has_patient = URIRef(BASE + "hasPatient")
        types = set(g.objects(has_patient, RDF.type))
        ranges = set(g.objects(has_patient, RDFS.range))
        assert OWL.ObjectProperty in types
        assert OWL.DatatypeProperty not in types
        assert URIRef(BASE + "Patient") in ranges
        assert XSD.integer not in ranges

        # 3. The coherence-queue sidecar was written, proving the resolver fired in
        #    the post-loop (it is only written when a mixed range was encountered).
        queue_path = out.with_suffix(".coherence_queue.json")
        assert queue_path.exists()
        payload = json.loads(queue_path.read_text())
        fk_props = [row[0] for row in payload["mixed_range"]["auto_resolved"]["fk_to_class"]]
        assert "hasPatient" in fk_props
        # an R1 fix needs no human review
        assert payload["mixed_range"]["needs_human_review"] == []


class TestCoherenceQueueSerialisation:
    def test_routed_and_unkeyable_become_review_items(self, tmp_path):
        rep = MixedRangeResolution(
            resolved_fk=[("hasPatient", "raw_events.patient_id", "Patient")],
            routed_vocab=[("hasEthnicity", "raw_admissions.ethnicity", "Ethnicity")],
            unkeyable=[("hasMystery",)],
        )
        out = tmp_path / "mimic_rigor_x.owl"
        out.write_text("<rdf/>")  # placeholder OWL

        queue_path = _write_coherence_queue(out, rep)

        assert queue_path is not None and queue_path.name == "mimic_rigor_x.coherence_queue.json"
        payload = json.loads(queue_path.read_text())
        review = payload["mixed_range"]["needs_human_review"]
        issues = {item["property"]: item["issue"] for item in review}
        assert issues == {"hasEthnicity": "vocab_class_vs_literal", "hasMystery": "unkeyable"}
        assert payload["mixed_range"]["auto_resolved"]["fk_to_class"] == [
            ["hasPatient", "raw_events.patient_id", "Patient"]
        ]

    def test_clean_ontology_writes_no_queue(self, tmp_path):
        out = tmp_path / "clean.owl"
        out.write_text("<rdf/>")
        assert _write_coherence_queue(out, MixedRangeResolution()) is None
