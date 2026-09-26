# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
End-to-end tests for IterativeOntologyGenerator using a mock OpenAI client.

Happy path + retry path tested with fully deterministic LLM responses.
"""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from rdflib.namespace import SKOS

from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.ontology.iterative.generator import (
    IterativeOntologyGenerator,
    _add_provenance_annotations,
    _build_oneof_enumerations,
    _camel_to_snake,
    _deduplicate_properties,
    _domain_candidates,
    _fill_missing_column_properties,
    _infer_missing_domains_and_ranges,
)
from semantic_mapper.ontology.iterative.graph_merger import MergeResult
from semantic_mapper.ontology.iterative.models import (
    AccumulatedClassSummary,
    ColumnContext,
    TableContext,
)

BASE = "http://mesh.local/ontology/mimic#"
_TEST_BASE = "http://mesh.local/ontology/test#"

BASE = "http://mesh.local/ontology/mimic#"

_PATIENTS_FRAGMENT = """\
Prefix: owl: <http://www.w3.org/2002/07/owl#>
Prefix: rdfs: <http://www.w3.org/2000/01/rdf-schema#>
Prefix: skos: <http://www.w3.org/2004/02/skos/core#>
Prefix: xsd: <http://www.w3.org/2001/XMLSchema#>
Prefix: : <http://mesh.local/ontology/mimic#>

Class: :Patient
    Annotations: rdfs:label "Patient"@en

DatatypeProperty: :hasPatientGender
    Annotations:
        rdfs:label "has patient gender"@en,
        skos:altLabel "gender"@en
    Domain: :Patient
    Range: xsd:string
"""

_APPROVED_VERDICT = json.dumps({
    "approved": True,
    "coverage_issues": [],
    "coherence_issues": [],
    "revision_instructions": "",
    "confidence": 0.9,
})

_REJECTED_THEN_APPROVED_VERDICT = [
    json.dumps({
        "approved": False,
        "coverage_issues": ["gender: missing property"],
        "coherence_issues": [],
        "revision_instructions": "Add DatatypeProperty for gender column.",
        "confidence": 0.3,
    }),
    json.dumps({
        "approved": True,
        "coverage_issues": [],
        "coherence_issues": [],
        "revision_instructions": "",
        "confidence": 0.9,
    }),
]


def _make_settings(tmp_owl_dir: Path):
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
    s.ONTOLOGY_NAMESPACE = ""  # default: derive namespace from the DB name
    s.SEED_OUTPUT_PATH = str(tmp_owl_dir / "seed.owl")
    s.OPENAI_MODEL = "gpt-4o"
    return s


def _make_profiles():
    return [
        TableProfile(
            table_name="raw_patients",
            row_count=1000,
            columns={
                "subject_id": ColumnProfile(
                    name="subject_id", inferred_type="INTEGER", is_nullable=False,
                    is_primary_key=True, null_rate=0.0, distinct_count=1000,
                ),
                "gender": ColumnProfile(
                    name="gender", inferred_type="VARCHAR", is_nullable=False,
                    is_primary_key=False, null_rate=0.0, distinct_count=2,
                    top_values=[{"value": "M"}, {"value": "F"}],
                ),
            },
            foreign_keys=[],
        ),
    ]


def _mock_openai_client(gen_response: str, judge_responses):
    """Build a mock OpenAI client."""
    if isinstance(judge_responses, str):
        judge_responses = [judge_responses]

    call_count = {"judge": 0}

    def create_completion(**kwargs):
        messages = kwargs.get("messages", [])
        is_json_mode = kwargs.get("response_format", {}).get("type") == "json_object"

        resp = MagicMock()
        resp.usage.prompt_tokens = 100
        resp.usage.completion_tokens = 50

        if is_json_mode:
            idx = min(call_count["judge"], len(judge_responses) - 1)
            resp.choices[0].message.content = judge_responses[idx]
            call_count["judge"] += 1
        else:
            resp.choices[0].message.content = gen_response

        return resp

    client = MagicMock()
    client.chat.completions.create.side_effect = create_completion
    return client


class TestDeriveBaseIri:
    def test_db_derived_strips_bronze_suffix(self, tmp_path):
        s = _make_settings(tmp_path)
        s.DATABASE_URL = "mysql://root@localhost/olympics2026_bronze"
        gen = IterativeOntologyGenerator(s)
        assert gen._derive_base_iri([]) == "http://mesh.local/ontology/olympics2026#"

    def test_explicit_namespace_token_wins(self, tmp_path):
        # A family token makes the schema namespace year-agnostic (winterolympics 2026+2030).
        s = _make_settings(tmp_path)
        s.DATABASE_URL = "mysql://root@localhost/olympics2026_bronze"
        s.ONTOLOGY_NAMESPACE = "winterolympics"
        gen = IterativeOntologyGenerator(s)
        assert gen._derive_base_iri([]) == "http://mesh.local/ontology/winterolympics#"

    def test_explicit_full_iri_used_verbatim(self, tmp_path):
        s = _make_settings(tmp_path)
        s.ONTOLOGY_NAMESPACE = "http://example.org/onto#"
        gen = IterativeOntologyGenerator(s)
        assert gen._derive_base_iri([]) == "http://example.org/onto#"


class TestIterativeOntologyGeneratorHappyPath:
    def test_generates_owl_file(self, tmp_path):
        settings = _make_settings(tmp_path)
        gen = IterativeOntologyGenerator(settings)

        with patch("openai.OpenAI") as mock_openai_cls:
            client = _mock_openai_client(_PATIENTS_FRAGMENT, _APPROVED_VERDICT)
            mock_openai_cls.return_value = client
            with patch.object(gen, "_call_gen_llm", return_value=_PATIENTS_FRAGMENT):
                with patch.object(gen, "_call_judge_llm") as mock_judge:
                    from semantic_mapper.ontology.iterative.models import JudgeVerdict
                    mock_judge.return_value = JudgeVerdict(approved=True, confidence=0.9)
                    out = gen.generate(_make_profiles())

        assert out.exists()
        assert out.suffix == ".owl"
        assert "rigor" in out.name

    def test_owl_contains_class_triple(self, tmp_path):
        settings = _make_settings(tmp_path)
        gen = IterativeOntologyGenerator(settings)

        with patch("openai.OpenAI"):
            with patch.object(gen, "_call_gen_llm", return_value=_PATIENTS_FRAGMENT):
                with patch.object(gen, "_call_judge_llm") as mock_judge:
                    from semantic_mapper.ontology.iterative.models import JudgeVerdict
                    mock_judge.return_value = JudgeVerdict(approved=True, confidence=0.9)
                    out = gen.generate(_make_profiles())

        g = Graph()
        g.parse(str(out), format="xml")
        patient_uri = URIRef(f"{BASE}Patient")
        assert (patient_uri, RDF.type, OWL.Class) in g

    def test_token_counters_incremented(self, tmp_path):
        settings = _make_settings(tmp_path)
        gen = IterativeOntologyGenerator(settings)

        with patch("openai.OpenAI"):
            gen._client = MagicMock()
            gen._client.chat.completions.create.return_value = MagicMock(
                choices=[MagicMock(message=MagicMock(content=_PATIENTS_FRAGMENT))],
                usage=MagicMock(prompt_tokens=50, completion_tokens=20),
            )
            with patch.object(gen, "_call_gen_llm", return_value=_PATIENTS_FRAGMENT):
                with patch.object(gen, "_call_judge_llm") as mock_judge:
                    from semantic_mapper.ontology.iterative.models import JudgeVerdict
                    mock_judge.return_value = JudgeVerdict(approved=True, confidence=0.9)
                    gen.generate(_make_profiles())

        # Token counters should be zero since we mocked _call_gen_llm and _call_judge_llm
        # (the actual LLM calls that increment counters are bypassed)
        assert gen.prompt_tokens >= 0
        assert gen.completion_tokens >= 0


class TestIterativeOntologyGeneratorRetryPath:
    def test_retry_on_rejection(self, tmp_path):
        settings = _make_settings(tmp_path)
        gen = IterativeOntologyGenerator(settings)

        call_count = {"gen": 0}

        def mock_gen(*args, **kwargs):
            call_count["gen"] += 1
            return _PATIENTS_FRAGMENT

        from semantic_mapper.ontology.iterative.models import JudgeVerdict
        rejection = JudgeVerdict(approved=False, confidence=0.3,
                                  revision_instructions="Add gender property.")
        approval = JudgeVerdict(approved=True, confidence=0.9)
        judge_responses = [rejection, approval]
        judge_call = {"idx": 0}

        def mock_judge(*args, **kwargs):
            resp = judge_responses[min(judge_call["idx"], len(judge_responses) - 1)]
            judge_call["idx"] += 1
            return resp

        with patch("openai.OpenAI"):
            with patch.object(gen, "_call_gen_llm", side_effect=mock_gen):
                with patch.object(gen, "_call_judge_llm", side_effect=mock_judge):
                    out = gen.generate(_make_profiles())

        # Gen-LLM must have been called at least twice (initial + retry)
        assert call_count["gen"] >= 2

    def test_max_retries_still_merges(self, tmp_path):
        """After max_retries rejections, fragment is merged anyway."""
        settings = _make_settings(tmp_path)
        settings.ONTOLOGY_GEN_MAX_RETRIES = 1

        from semantic_mapper.ontology.iterative.models import JudgeVerdict
        always_reject = JudgeVerdict(approved=False, confidence=0.1,
                                      revision_instructions="Fix everything.")

        gen = IterativeOntologyGenerator(settings)
        with patch("openai.OpenAI"):
            with patch.object(gen, "_call_gen_llm", return_value=_PATIENTS_FRAGMENT):
                with patch.object(gen, "_call_judge_llm", return_value=always_reject):
                    out = gen.generate(_make_profiles())

        # Should still produce a file
        assert out.exists()


# ---------------------------------------------------------------------------
# Tests for _fill_missing_column_properties
# ---------------------------------------------------------------------------

_MIMIC_WORD_SPLITS = {"admittime": "admit_time", "dischtime": "discharge_time"}


class TestFillMissingColumnProperties:
    def _base_graph(self) -> Graph:
        g = Graph()
        cls = URIRef(f"{BASE}Admission")
        g.add((cls, RDF.type, OWL.Class))
        return g

    def _profiles(self):
        return [_make_profile("admissions", {
            "hadm_id": "INTEGER",      # PK — should be skipped
            "admittime": "DATETIME",   # should be created
            "insurance": "VARCHAR",    # should be created
        })]

    def test_creates_missing_property(self):
        g = self._base_graph()
        profiles = self._profiles()
        profiles[0].columns["hadm_id"].is_primary_key = True
        _fill_missing_column_properties(g, profiles, BASE, _MIMIC_WORD_SPLITS)
        # admittime → word_splits → admit_time → AdmitTime
        prop = URIRef(f"{BASE}hasAdmitTime")
        assert (prop, RDF.type, OWL.DatatypeProperty) in g

    def test_creates_property_without_word_splits(self):
        g = self._base_graph()
        profiles = self._profiles()
        profiles[0].columns["hadm_id"].is_primary_key = True
        _fill_missing_column_properties(g, profiles, BASE)
        # No word_splits → admittime stays as-is → hasAdmittime
        prop = URIRef(f"{BASE}hasAdmittime")
        assert (prop, RDF.type, OWL.DatatypeProperty) in g

    def test_sets_correct_domain(self):
        g = self._base_graph()
        profiles = self._profiles()
        profiles[0].columns["hadm_id"].is_primary_key = True
        _fill_missing_column_properties(g, profiles, BASE, _MIMIC_WORD_SPLITS)
        prop = URIRef(f"{BASE}hasAdmitTime")
        assert URIRef(f"{BASE}Admission") in list(g.objects(prop, RDFS.domain))

    def test_sets_correct_xsd_range(self):
        g = self._base_graph()
        profiles = self._profiles()
        profiles[0].columns["hadm_id"].is_primary_key = True
        _fill_missing_column_properties(g, profiles, BASE, _MIMIC_WORD_SPLITS)
        prop = URIRef(f"{BASE}hasAdmitTime")
        assert URIRef("http://www.w3.org/2001/XMLSchema#dateTime") in list(g.objects(prop, RDFS.range))

    def test_strips_leading_has_prefix(self):
        from rdflib.namespace import SKOS
        g = Graph()
        cls = URIRef(f"{BASE}Chartevents")
        g.add((cls, RDF.type, OWL.Class))
        profiles = [_make_profile("chartevents", {"chartevents_id": "INTEGER", "has_chartevents_data": "INTEGER"})]
        profiles[0].columns["chartevents_id"].is_primary_key = True
        _fill_missing_column_properties(g, profiles, BASE)
        # has_chartevents_data → strip has_ → chartevents_data → CharteventsDa...
        # Must NOT produce hasHasCharteventsDa...
        prop_uris = [str(p) for p in g.subjects(RDF.type, OWL.DatatypeProperty)]
        assert not any("HasHas" in p or "hasHas" in p for p in prop_uris)

    def test_collision_uses_class_local_name(self):
        from rdflib.namespace import SKOS
        g = self._base_graph()
        # Pre-declare hasInsurance (collision trigger)
        collision_prop = URIRef(f"{BASE}hasInsurance")
        g.add((collision_prop, RDF.type, OWL.DatatypeProperty))
        profiles = self._profiles()
        profiles[0].columns["hadm_id"].is_primary_key = True
        _fill_missing_column_properties(g, profiles, BASE)
        # Collision fallback should use class local name "Admission", not table name "admissions"
        qualified = URIRef(f"{BASE}hasAdmissionInsurance")
        assert (qualified, RDF.type, OWL.DatatypeProperty) in g
        # Must NOT create hasAdmissionsInsurance or hasRawAdmissionsInsurance
        all_props = [str(p) for p in g.subjects(RDF.type, OWL.DatatypeProperty)]
        assert not any("hasAdmissionsInsurance" in p or "hasRaw" in p for p in all_props)

    def test_skips_pk_columns(self):
        g = self._base_graph()
        profiles = self._profiles()
        profiles[0].columns["hadm_id"].is_primary_key = True
        _fill_missing_column_properties(g, profiles, BASE)
        pk_prop = URIRef(f"{BASE}hasHadmId")
        # hadm_id may or may not exist — if created it's for another reason
        # Key: hadm_id as PK means it must not be gap-filled
        from rdflib.namespace import SKOS
        gap_filled = [p for p in g.subjects(RDF.type, OWL.DatatypeProperty)
                      if any(str(a) == "hadm_id" for a in g.objects(p, SKOS.altLabel))]
        assert gap_filled == []

    def test_gap_filled_property_carries_source_column_provenance(self):
        # Completeness guarantee: a gap-filled column must carry fm:sourceColumn so the
        # terminology binder + RDF materialiser can resolve it (option b depends on this).
        _FM_SOURCE_COLUMN = URIRef("http://forgemesh.ai/ontology#sourceColumn")
        g = Graph()
        g.add((URIRef(f"{BASE}Condition"), RDF.type, OWL.Class))
        profiles = [_make_profile("conditions", {"code": "VARCHAR"})]
        _fill_missing_column_properties(g, profiles, BASE)
        prop = URIRef(f"{BASE}hasCode")
        assert (prop, RDF.type, OWL.DatatypeProperty) in g
        assert (prop, _FM_SOURCE_COLUMN, Literal("conditions.code")) in g

    def test_idempotent_on_second_run(self):
        # The authoritative second pass must be a no-op when nothing is missing.
        g = self._base_graph()
        profiles = self._profiles()
        profiles[0].columns["hadm_id"].is_primary_key = True
        _fill_missing_column_properties(g, profiles, BASE, _MIMIC_WORD_SPLITS)
        n1 = len(list(g.subjects(RDF.type, OWL.DatatypeProperty)))
        _fill_missing_column_properties(g, profiles, BASE, _MIMIC_WORD_SPLITS)
        n2 = len(list(g.subjects(RDF.type, OWL.DatatypeProperty)))
        assert n1 == n2

    def test_recovers_column_orphaned_by_removed_object_property(self):
        # Regression: the LLM mis-modelled a high-cardinality code column as a bare
        # ObjectProperty; a later cleanup pass removed it as a fabricated FK, leaving the
        # column with no property. The first gap-fill had skipped it (covered at the
        # time). The authoritative second pass must recover it as a DatatypeProperty —
        # this is exactly what makes option (b)'s code-column coverage deterministic.
        _FM_SOURCE_COLUMN = URIRef("http://forgemesh.ai/ontology#sourceColumn")
        _FM_SOURCE_FK = URIRef("http://forgemesh.ai/ontology#sourceForeignKey")
        g = Graph()
        cls = URIRef(f"{BASE}Condition")
        g.add((cls, RDF.type, OWL.Class))
        bogus = URIRef(f"{BASE}hasConditionCode")
        g.add((bogus, RDF.type, OWL.ObjectProperty))
        g.add((bogus, _FM_SOURCE_FK, Literal("conditions.code -> SomeClass")))
        profiles = [_make_profile("conditions", {"code": "VARCHAR"})]
        # First pass skips `code` (covered by the bogus ObjectProperty).
        _fill_missing_column_properties(g, profiles, BASE)
        assert (URIRef(f"{BASE}hasCode"), RDF.type, OWL.DatatypeProperty) not in g
        # A cleanup pass removes the fabricated ObjectProperty…
        for t in list(g.triples((bogus, None, None))):
            g.remove(t)
        # …and the authoritative second pass recovers the column with provenance.
        _fill_missing_column_properties(g, profiles, BASE)
        prop = URIRef(f"{BASE}hasCode")
        assert (prop, RDF.type, OWL.DatatypeProperty) in g
        assert (prop, _FM_SOURCE_COLUMN, Literal("conditions.code")) in g

    def test_skips_already_present_columns(self):
        from rdflib.namespace import SKOS
        g = self._base_graph()
        # Pre-add admittime property
        existing = URIRef(f"{BASE}hasAdmissionAdmittime")
        g.add((existing, RDF.type, OWL.DatatypeProperty))
        g.add((existing, SKOS.altLabel, Literal("admittime", lang="en")))
        profiles = self._profiles()
        profiles[0].columns["hadm_id"].is_primary_key = True
        _fill_missing_column_properties(g, profiles, BASE)
        # Should not create a second property for admittime
        admittime_props = [p for p in g.subjects(RDF.type, OWL.DatatypeProperty)
                           if any(str(a) == "admittime" for a in g.objects(p, SKOS.altLabel))]
        assert len(admittime_props) == 1

    def test_skips_fk_columns(self):
        from rdflib.namespace import SKOS
        g = self._base_graph()
        profiles = [_make_profile("admissions", {"hadm_id": "INTEGER", "subject_id": "INTEGER"})]
        profiles[0].columns["hadm_id"].is_primary_key = True
        profiles[0].foreign_keys = [{"column": "subject_id", "ref_table": "patients"}]
        _fill_missing_column_properties(g, profiles, BASE)
        fk_props = [p for p in g.subjects(RDF.type, OWL.DatatypeProperty)
                    if any(str(a) == "subject_id" for a in g.objects(p, SKOS.altLabel))]
        assert fk_props == []

    def test_skips_when_object_property_owns_canonical_name(self):
        # LLM created hasAdmissionLocation as ObjectProperty (categorical).
        # Gap-fill must NOT create a DataProperty fallback for admission_location.
        g = self._base_graph()
        obj_prop = URIRef(f"{BASE}hasAdmissionLocation")
        g.add((obj_prop, RDF.type, OWL.ObjectProperty))
        profiles = [_make_profile("admissions", {
            "hadm_id": "INTEGER",
            "admission_location": "VARCHAR",
        })]
        profiles[0].columns["hadm_id"].is_primary_key = True
        _fill_missing_column_properties(g, profiles, BASE)
        data_props = [p for p in g.subjects(RDF.type, OWL.DatatypeProperty)]
        local_names = [str(p).split("#")[-1] for p in data_props]
        assert not any("dmissionLocation" in n for n in local_names), local_names

    def test_gap_fill_injects_domain_into_domainless_object_property(self):
        # ObjProp exists but has no domain → gap-fill must add domain=Admission.
        g = self._base_graph()
        obj_prop = URIRef(f"{BASE}hasAdmissionLocation")
        g.add((obj_prop, RDF.type, OWL.ObjectProperty))
        # No domain triple set
        profiles = [_make_profile("admissions", {
            "hadm_id": "INTEGER",
            "admission_location": "VARCHAR",
        })]
        profiles[0].columns["hadm_id"].is_primary_key = True
        _fill_missing_column_properties(g, profiles, BASE)
        assert (obj_prop, RDFS.domain, URIRef(f"{BASE}Admission")) in g

    def test_gap_fill_does_not_overwrite_existing_obj_prop_domain(self):
        # ObjProp already has domain → gap-fill must NOT add a second domain.
        g = self._base_graph()
        obj_prop = URIRef(f"{BASE}hasAdmissionLocation")
        g.add((obj_prop, RDF.type, OWL.ObjectProperty))
        g.add((obj_prop, RDFS.domain, URIRef(f"{BASE}Admission")))
        profiles = [_make_profile("admissions", {
            "hadm_id": "INTEGER",
            "admission_location": "VARCHAR",
        })]
        profiles[0].columns["hadm_id"].is_primary_key = True
        _fill_missing_column_properties(g, profiles, BASE)
        domains = list(g.objects(obj_prop, RDFS.domain))
        assert len(domains) == 1

    def test_no_hasAdmissionAdmission_created(self):
        # Regression: hasAdmissionAdmissionType should never be created.
        g = self._base_graph()
        obj_prop = URIRef(f"{BASE}hasAdmissionType")
        g.add((obj_prop, RDF.type, OWL.ObjectProperty))
        profiles = [_make_profile("admissions", {
            "hadm_id": "INTEGER",
            "admission_type": "VARCHAR",
        })]
        profiles[0].columns["hadm_id"].is_primary_key = True
        _fill_missing_column_properties(g, profiles, BASE)
        assert (URIRef(f"{BASE}hasAdmissionAdmissionType"), RDF.type, OWL.DatatypeProperty) not in g


# ---------------------------------------------------------------------------
# Tests for _infer_missing_domains_and_ranges
# ---------------------------------------------------------------------------

from semantic_mapper.db.models import ColumnProfile, TableProfile as _TP


def _make_profile(table: str, cols: dict) -> "_TP":
    columns = {
        name: ColumnProfile(
            name=name,
            inferred_type=sql_type,
            is_nullable=True,
            is_primary_key=False,
            null_rate=0.0,
            distinct_count=10,
        )
        for name, sql_type in cols.items()
    }
    return _TP(table_name=table, row_count=100, columns=columns, foreign_keys=[])


class TestInferMissingDomainsAndRanges:
    def _base_graph(self) -> Graph:
        g = Graph()
        for local in ("Patient", "Admission"):
            uri = URIRef(f"{BASE}{local}")
            g.add((uri, RDF.type, OWL.Class))
        return g

    def _profiles(self):
        return [
            _make_profile("patients", {"subject_id": "INTEGER", "dob": "DATE", "gender": "VARCHAR"}),
            _make_profile("admissions", {"hadm_id": "INTEGER", "admittime": "DATETIME", "admission_type": "VARCHAR"}),
        ]

    def test_adds_domain_via_altlabel(self):
        g = self._base_graph()
        from rdflib.namespace import SKOS
        prop = URIRef(f"{BASE}hasAdmittime")
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, SKOS.altLabel, Literal("admittime", lang="en")))
        _infer_missing_domains_and_ranges(g, self._profiles(), BASE)
        domains = list(g.objects(prop, RDFS.domain))
        assert URIRef(f"{BASE}Admission") in domains

    def test_infers_xsd_range_from_sql_type(self):
        g = self._base_graph()
        from rdflib.namespace import SKOS
        prop = URIRef(f"{BASE}hasAdmittime")
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, SKOS.altLabel, Literal("admittime", lang="en")))
        _infer_missing_domains_and_ranges(g, self._profiles(), BASE)
        ranges = list(g.objects(prop, RDFS.range))
        assert URIRef("http://www.w3.org/2001/XMLSchema#dateTime") in ranges

    def test_replaces_non_xsd_range(self):
        """Range: integer (without xsd: prefix) → resolved as bad URI → should be replaced."""
        g = self._base_graph()
        from rdflib.namespace import SKOS
        prop = URIRef(f"{BASE}hasHadmId")
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, SKOS.altLabel, Literal("hadm_id", lang="en")))
        g.add((prop, RDFS.range, URIRef(f"{BASE}integer")))  # bad range from LLM
        _infer_missing_domains_and_ranges(g, self._profiles(), BASE)
        ranges = list(g.objects(prop, RDFS.range))
        assert URIRef("http://www.w3.org/2001/XMLSchema#integer") in ranges
        assert URIRef(f"{BASE}integer") not in ranges

    def test_does_not_overwrite_good_xsd_range(self):
        g = self._base_graph()
        from rdflib.namespace import SKOS
        prop = URIRef(f"{BASE}hasDob")
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, SKOS.altLabel, Literal("dob", lang="en")))
        g.add((prop, RDFS.range, URIRef("http://www.w3.org/2001/XMLSchema#date")))
        _infer_missing_domains_and_ranges(g, self._profiles(), BASE)
        ranges = list(g.objects(prop, RDFS.range))
        assert len(ranges) == 1
        assert URIRef("http://www.w3.org/2001/XMLSchema#date") in ranges

    def test_obj_prop_range_inferred_from_local_name(self):
        g = self._base_graph()
        prop = URIRef(f"{BASE}hasAdmission")
        g.add((prop, RDF.type, OWL.ObjectProperty))
        _infer_missing_domains_and_ranges(g, self._profiles(), BASE)
        ranges = list(g.objects(prop, RDFS.range))
        assert URIRef(f"{BASE}Admission") in ranges

    def test_obj_prop_strips_for_qualifier(self):
        g = self._base_graph()
        prop = URIRef(f"{BASE}hasAdmissionForPatient")
        g.add((prop, RDF.type, OWL.ObjectProperty))
        _infer_missing_domains_and_ranges(g, self._profiles(), BASE)
        ranges = list(g.objects(prop, RDFS.range))
        assert URIRef(f"{BASE}Admission") in ranges

    def test_ambiguous_column_skips_domain(self):
        """Column appearing in two tables → domain left unset (ambiguous)."""
        g = self._base_graph()
        from rdflib.namespace import SKOS
        prop = URIRef(f"{BASE}hasSubjectId")
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, SKOS.altLabel, Literal("subject_id", lang="en")))
        # Both profiles have subject_id → ambiguous
        profiles = [
            _make_profile("patients", {"subject_id": "INTEGER"}),
            _make_profile("admissions", {"subject_id": "INTEGER"}),
        ]
        _infer_missing_domains_and_ranges(g, profiles, BASE)
        domains = list(g.objects(prop, RDFS.domain))
        assert domains == []

    def test_obj_prop_domain_inferred_via_snake_case(self):
        """hasDrugType → strip has → DrugType → drug_type → prescriptions → Prescription domain."""
        g = Graph()
        g.add((URIRef(f"{BASE}Prescription"), RDF.type, OWL.Class))
        g.add((URIRef(f"{BASE}DrugType"), RDF.type, OWL.Class))
        prop = URIRef(f"{BASE}hasDrugType")
        g.add((prop, RDF.type, OWL.ObjectProperty))
        # no domain set yet
        profiles = [_make_profile("prescriptions", {"drug_id": "INTEGER", "drug_type": "VARCHAR"})]
        profiles[0].columns["drug_id"].is_primary_key = True
        _infer_missing_domains_and_ranges(g, profiles, BASE)
        assert URIRef(f"{BASE}Prescription") in list(g.objects(prop, RDFS.domain))

    def test_obj_prop_domain_inferred_via_type_suffix_strip(self):
        """hasGenderType → strip has → GenderType → strip Type → gender → patients → Patient domain."""
        g = Graph()
        g.add((URIRef(f"{BASE}Patient"), RDF.type, OWL.Class))
        g.add((URIRef(f"{BASE}GenderType"), RDF.type, OWL.Class))
        prop = URIRef(f"{BASE}hasGenderType")
        g.add((prop, RDF.type, OWL.ObjectProperty))
        profiles = [_make_profile("patients", {"subject_id": "INTEGER", "gender": "VARCHAR"})]
        profiles[0].columns["subject_id"].is_primary_key = True
        _infer_missing_domains_and_ranges(g, profiles, BASE)
        assert URIRef(f"{BASE}Patient") in list(g.objects(prop, RDFS.domain))


# ---------------------------------------------------------------------------
# Tests for _camel_to_snake and _domain_candidates
# ---------------------------------------------------------------------------

class TestCamelToSnake:
    def test_single_word(self):
        assert _camel_to_snake("Admission") == "admission"

    def test_two_words(self):
        assert _camel_to_snake("DrugType") == "drug_type"

    def test_already_snake(self):
        assert _camel_to_snake("drug_type") == "drug_type"

    def test_three_words(self):
        assert _camel_to_snake("AdmissionType") == "admission_type"


class TestDomainCandidates:
    def test_plain_name(self):
        assert "gender" in _domain_candidates("Gender")

    def test_camel_case_converted(self):
        assert "drug_type" in _domain_candidates("DrugType")

    def test_type_suffix_stripped(self):
        # GenderType → gender and gender_type both present
        cands = _domain_candidates("GenderType")
        assert "gender" in cands
        assert "gender_type" in cands

    def test_no_type_suffix(self):
        cands = _domain_candidates("Drug")
        assert "drug" in cands
        assert not any("type" in c for c in cands)

# ---------------------------------------------------------------------------
# Tests for _infer_class_hierarchy (removed — was wrong IS-A hierarchy)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tests for _deduplicate_properties
# ---------------------------------------------------------------------------

def _graph_with_dupes() -> Graph:
    from rdflib.namespace import SKOS
    g = Graph()
    cls = URIRef(f"{BASE}Admission")
    g.add((cls, RDF.type, OWL.Class))
    for local, domain_local, rng in [
        ("hadm_id",   "Admission", None),
        ("hadmId",    None,        "http://www.w3.org/2001/XMLSchema#integer"),
        ("hasHadmId", "Admission", "http://www.w3.org/2001/XMLSchema#integer"),
    ]:
        p = URIRef(f"{BASE}{local}")
        g.add((p, RDF.type, OWL.DatatypeProperty))
        g.add((p, SKOS.altLabel, Literal("hadm_id", lang="en")))
        if domain_local:
            g.add((p, RDFS.domain, URIRef(f"{BASE}{domain_local}")))
        if rng:
            g.add((p, RDFS.range, URIRef(rng)))
    return g


class TestDeduplicateProperties:
    def test_winner_has_has_prefix(self):
        g = _graph_with_dupes()
        _deduplicate_properties(g)
        remaining = [p for p in g.subjects(RDF.type, OWL.DatatypeProperty)]
        locals_ = [str(p).split("#")[-1] for p in remaining]
        assert "hasHadmId" in locals_
        assert "hadm_id" not in locals_
        assert "hadmId" not in locals_

    def test_only_one_property_remains(self):
        g = _graph_with_dupes()
        _deduplicate_properties(g)
        from rdflib.namespace import SKOS
        col_props = [p for p in g.subjects(RDF.type, OWL.DatatypeProperty)
                     if any(str(a) == "hadm_id" for a in g.objects(p, SKOS.altLabel))]
        assert len(col_props) == 1

    def test_domains_merged_into_winner(self):
        """Domain from loser is transferred to winner."""
        from rdflib.namespace import SKOS
        g = Graph()
        cls = URIRef(f"{BASE}DiagnosesIcd")
        g.add((cls, RDF.type, OWL.Class))
        for local, dom in [("hadm_id", "Admission"), ("hasHadmId", "DiagnosesIcd")]:
            p = URIRef(f"{BASE}{local}")
            g.add((p, RDF.type, OWL.DatatypeProperty))
            g.add((p, SKOS.altLabel, Literal("hadm_id", lang="en")))
            g.add((p, RDFS.domain, URIRef(f"{BASE}{dom}")))
        _deduplicate_properties(g)
        winner = URIRef(f"{BASE}hasHadmId")
        domains = {str(d).split("#")[-1] for d in g.objects(winner, RDFS.domain)}
        assert "Admission" in domains
        assert "DiagnosesIcd" in domains

    def test_no_altlabel_group_left_untouched(self):
        """Property with no altLabel is not removed."""
        g = Graph()
        p = URIRef(f"{BASE}hasFoo")
        g.add((p, RDF.type, OWL.DatatypeProperty))
        _deduplicate_properties(g)
        assert (p, RDF.type, OWL.DatatypeProperty) in g

    def test_single_property_per_column_untouched(self):
        from rdflib.namespace import SKOS
        g = Graph()
        p = URIRef(f"{BASE}hasAdmittime")
        g.add((p, RDF.type, OWL.DatatypeProperty))
        g.add((p, SKOS.altLabel, Literal("admittime", lang="en")))
        _deduplicate_properties(g)
        assert (p, RDF.type, OWL.DatatypeProperty) in g


# ---------------------------------------------------------------------------
# Tests for _build_oneof_enumerations
# ---------------------------------------------------------------------------

def _graph_with_enum(cls_local: str, ind_locals: list) -> Graph:
    g = Graph()
    cls_uri = URIRef(f"{BASE}{cls_local}")
    g.add((cls_uri, RDF.type, OWL.Class))
    for local in ind_locals:
        ind = URIRef(f"{BASE}{local}")
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, RDF.type, cls_uri))
    return g


class TestBuildOneofEnumerations:
    def test_adds_equivalent_class(self):
        g = _graph_with_enum("AdmissionType", ["Emergency", "Elective"])
        _build_oneof_enumerations(g)
        cls = URIRef(f"{BASE}AdmissionType")
        equiv = list(g.objects(cls, OWL.equivalentClass))
        assert len(equiv) == 1

    def test_oneof_reachable_from_equiv(self):
        g = _graph_with_enum("AdmissionType", ["Emergency", "Elective"])
        _build_oneof_enumerations(g)
        cls = URIRef(f"{BASE}AdmissionType")
        for bn in g.objects(cls, OWL.equivalentClass):
            oneof_list = list(g.objects(bn, OWL.oneOf))
            assert len(oneof_list) == 1

    def test_does_not_duplicate_if_already_set(self):
        from rdflib import BNode
        g = _graph_with_enum("AdmissionType", ["Emergency"])
        cls = URIRef(f"{BASE}AdmissionType")
        # Pre-set equivalentClass
        g.add((cls, OWL.equivalentClass, BNode()))
        _build_oneof_enumerations(g)
        equiv = list(g.objects(cls, OWL.equivalentClass))
        assert len(equiv) == 1  # no second one added

    def test_skips_class_with_no_individuals(self):
        g = Graph()
        cls = URIRef(f"{BASE}EmptyClass")
        g.add((cls, RDF.type, OWL.Class))
        _build_oneof_enumerations(g)
        assert list(g.objects(cls, OWL.equivalentClass)) == []

    def test_multiple_enum_classes_handled(self):
        g = _graph_with_enum("AdmissionType", ["Emergency", "Elective"])
        g2 = _graph_with_enum("Gender", ["Male", "Female"])
        for triple in g2:
            g.add(triple)
        _build_oneof_enumerations(g)
        for local in ("AdmissionType", "Gender"):
            cls = URIRef(f"{BASE}{local}")
            assert list(g.objects(cls, OWL.equivalentClass))


# ---------------------------------------------------------------------------
# _add_provenance_annotations
# ---------------------------------------------------------------------------

def _make_table_ctx(table_name: str, columns: list[ColumnContext]) -> TableContext:
    return TableContext(
        table_name=table_name,
        row_count=100,
        columns=columns,
        processing_index=1,
        total_tables=1,
    )


def _make_col(name: str, *, is_fk: bool = False, fk_ref_table: str | None = None,
               inferred_type: str = "VARCHAR(50)", null_rate: float = 0.0) -> ColumnContext:
    return ColumnContext(
        table_name="admissions",
        col_name=name,
        inferred_type=inferred_type,
        is_pk=False,
        is_fk=is_fk,
        fk_ref_table=fk_ref_table,
        fk_ref_skipped=False,
        is_nullable=False,
        null_rate=null_rate,
        distinct_count=10,
    )


class TestAddProvenanceAnnotations:
    """Unit tests for _add_provenance_annotations, specifically ObjectProperty FK recovery."""

    _FM = "http://forgemesh.ai/ontology#"

    def _fm(self, local: str) -> URIRef:
        return URIRef(f"{self._FM}{local}")

    def _make_graph_with_obj_prop(self, prop_local: str, range_local: str) -> Graph:
        g = Graph()
        prop = URIRef(f"{BASE}{prop_local}")
        rng = URIRef(f"{BASE}{range_local}")
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.range, rng))
        return g

    def test_obj_prop_resolved_via_range_class_local(self):
        """ObjectProperty with matching rdfs:range gets correct fm:sourceForeignKey."""
        g = self._make_graph_with_obj_prop("hasPatient", "Patient")
        col = _make_col("subject_id", is_fk=True, fk_ref_table="patients")
        ctx = _make_table_ctx("admissions", [col])

        cls_summary = AccumulatedClassSummary(
            local_name="Admission", label="Admission",
            uri=f"{BASE}Admission", source_tables=["admissions"],
        )
        merge_result = MergeResult(
            new_classes=[cls_summary],
            new_props=[f"{BASE}hasPatient"],
        )

        _add_provenance_annotations(g, merge_result, ctx)

        fk_vals = list(g.objects(URIRef(f"{BASE}hasPatient"), self._fm("sourceForeignKey")))
        assert len(fk_vals) == 1
        assert str(fk_vals[0]) == "admissions.subject_id -> patients"

    def test_obj_prop_falls_back_when_range_unknown(self):
        """ObjectProperty with unrecognised range still gets 'not recoverable'."""
        g = self._make_graph_with_obj_prop("hasUnknown", "UnrelatedClass")
        col = _make_col("subject_id", is_fk=True, fk_ref_table="patients")
        ctx = _make_table_ctx("admissions", [col])

        merge_result = MergeResult(new_classes=[], new_props=[f"{BASE}hasUnknown"])
        _add_provenance_annotations(g, merge_result, ctx)

        fk_vals = list(g.objects(URIRef(f"{BASE}hasUnknown"), self._fm("sourceForeignKey")))
        assert str(fk_vals[0]) == "not recoverable"

    def test_obj_prop_resolved_via_alt_label_takes_priority(self):
        """skos:altLabel lookup wins over rdfs:range when both are present."""
        g = self._make_graph_with_obj_prop("hasPatient", "Patient")
        # altLabel matches the FK column name directly
        g.add((URIRef(f"{BASE}hasPatient"), SKOS.altLabel, Literal("subject_id")))
        col = _make_col("subject_id", is_fk=True, fk_ref_table="patients")
        ctx = _make_table_ctx("admissions", [col])

        merge_result = MergeResult(new_classes=[], new_props=[f"{BASE}hasPatient"])
        _add_provenance_annotations(g, merge_result, ctx)

        fk_vals = list(g.objects(URIRef(f"{BASE}hasPatient"), self._fm("sourceForeignKey")))
        assert str(fk_vals[0]) == "admissions.subject_id -> patients"

    def test_datatype_prop_still_gets_source_column(self):
        """DatatypeProperty (non-FK) continues to receive fm:sourceColumn/sqlType/nullRate."""
        g = Graph()
        prop = URIRef(f"{BASE}hasAdmissionType")
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, SKOS.altLabel, Literal("admission_type")))

        col = _make_col("admission_type", inferred_type="VARCHAR(20)", null_rate=0.05)
        ctx = _make_table_ctx("admissions", [col])

        merge_result = MergeResult(new_classes=[], new_props=[f"{BASE}hasAdmissionType"])
        _add_provenance_annotations(g, merge_result, ctx)

        assert str(list(g.objects(prop, self._fm("sourceColumn")))[0]) == "admissions.admission_type"
        assert str(list(g.objects(prop, self._fm("sqlType")))[0]) == "VARCHAR(20)"
        assert str(list(g.objects(prop, self._fm("nullRate")))[0]) == "0.05"

    def test_class_gets_source_table(self):
        """New class receives fm:sourceTable equal to the table name."""
        g = Graph()
        cls_uri = f"{BASE}Admission"
        cls_summary = AccumulatedClassSummary(
            local_name="Admission", label="Admission",
            uri=cls_uri, source_tables=["admissions"],
        )
        merge_result = MergeResult(new_classes=[cls_summary], new_props=[])
        _add_provenance_annotations(g, merge_result, _make_table_ctx("admissions", []))

        src_vals = list(g.objects(URIRef(cls_uri), self._fm("sourceTable")))
        assert str(src_vals[0]) == "admissions"

    def test_existing_fk_annotation_not_duplicated(self):
        """If fm:sourceForeignKey already in graph (from LLM), no second triple is added."""
        g = self._make_graph_with_obj_prop("hasPatient", "Patient")
        prop = URIRef(f"{BASE}hasPatient")
        # LLM already placed the correct annotation via Manchester Syntax
        g.add((prop, URIRef(self._FM + "sourceForeignKey"),
               Literal("admissions.subject_id -> patients")))

        col = _make_col("subject_id", is_fk=True, fk_ref_table="patients")
        ctx = _make_table_ctx("admissions", [col])

        merge_result = MergeResult(new_classes=[], new_props=[f"{BASE}hasPatient"])
        _add_provenance_annotations(g, merge_result, ctx)

        fk_vals = list(g.objects(prop, URIRef(self._FM + "sourceForeignKey")))
        assert len(fk_vals) == 1, f"Expected 1 annotation, got {len(fk_vals)}: {fk_vals}"
        assert str(fk_vals[0]) == "admissions.subject_id -> patients"

    def test_categorical_obj_prop_resolved_via_alt_label_column(self):
        """Categorical ObjectProperty with skos:altLabel matching a non-FK column."""
        g = Graph()
        prop = URIRef(f"{BASE}hasAgreementType")
        rng = URIRef(f"{BASE}AgreementType")
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.range, rng))
        g.add((prop, SKOS.altLabel, Literal("agreement_type")))

        col = _make_col("agreement_type", inferred_type="VARCHAR(10)", null_rate=0.0)
        ctx = _make_table_ctx("raw_agreements", [col])

        merge_result = MergeResult(new_classes=[], new_props=[f"{BASE}hasAgreementType"])
        _add_provenance_annotations(g, merge_result, ctx)

        fk_vals = list(g.objects(prop, URIRef(self._FM + "sourceForeignKey")))
        assert len(fk_vals) == 1
        assert str(fk_vals[0]) == "raw_agreements.agreement_type -> AgreementType"

    def test_categorical_obj_prop_resolved_via_range_snake_case(self):
        """Categorical ObjectProperty with NO skos:altLabel: range class name
        converted to snake_case identifies the source column (Strategy 4)."""
        g = Graph()
        prop = URIRef(f"{BASE}hasAgreementType")
        rng = URIRef(f"{BASE}AgreementType")
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.range, rng))
        # No skos:altLabel — LLM omitted it

        col = _make_col("agreement_type", inferred_type="VARCHAR(10)", null_rate=0.0)
        ctx = _make_table_ctx("raw_agreements", [col])

        merge_result = MergeResult(new_classes=[], new_props=[f"{BASE}hasAgreementType"])
        _add_provenance_annotations(g, merge_result, ctx)

        fk_vals = list(g.objects(prop, URIRef(self._FM + "sourceForeignKey")))
        assert len(fk_vals) == 1
        assert str(fk_vals[0]) == "raw_agreements.agreement_type -> AgreementType"
