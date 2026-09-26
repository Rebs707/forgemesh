# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import XSD

from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition
from semantic_mapper.rdf.triple_builder import TripleBuilder, _build_superclass_map, _normalise_literal, _xsd_type
from semantic_mapper.rdf.uri_factory import UriFactory, hash_identifier


def _ontology_with_range(prop_uri: str, range_uri: str) -> Graph:
    """Minimal ontology graph declaring one object property's rdfs:range."""
    g = Graph()
    prop = URIRef(prop_uri)
    g.add((prop, RDF.type, OWL.ObjectProperty))
    g.add((prop, RDFS.range, URIRef(range_uri)))
    return g


BASE = "http://mesh.local/data/"
PATIENT_CLASS = "http://ex.org/ontology#Patient"
ENCOUNTER_CLASS = "http://ex.org/ontology#Encounter"


@pytest.fixture
def uri_factory():
    return UriFactory(BASE)


@pytest.fixture
def patient_definition():
    return GoldTableDefinition(
        gold_table_name="gold_patient",
        primary_class_uri=PATIENT_CLASS,
        class_label="patient",
        source_silver_tables=["silver_raw_patient"],
        column_specs=[
            GoldColumnSpec(
                silver_table="silver_raw_patient",
                source_column="id",
                gold_column_name="id",
                source_data_type="VARCHAR(50)",
                property_uri="http://ex.org/ontology#patientId",
            ),
            GoldColumnSpec(
                silver_table="silver_raw_patient",
                source_column="date_of_birth",
                gold_column_name="date_of_birth",
                source_data_type="DATE",
                property_uri="http://ex.org/ontology#dateOfBirth",
            ),
        ],
    )


@pytest.fixture
def encounter_definition():
    return GoldTableDefinition(
        gold_table_name="gold_encounter",
        primary_class_uri=ENCOUNTER_CLASS,
        class_label="encounter",
        source_silver_tables=["silver_raw_encounter"],
        column_specs=[
            GoldColumnSpec(
                silver_table="silver_raw_encounter",
                source_column="patient",
                gold_column_name="patient",
                source_data_type="VARCHAR(50)",
                property_uri="http://ex.org/ontology#hasPatient",
            ),
        ],
    )


@pytest.fixture
def builder(uri_factory, patient_definition, encounter_definition):
    return TripleBuilder(
        definitions=[patient_definition, encounter_definition],
        uri_factory=uri_factory,
    )


class TestClassAssertion:
    def test_rdf_type_triple_emitted(self, builder, patient_definition):
        row = {
            "_ontology_class_uri": PATIENT_CLASS,
            "_bronze_source_ref": "raw_patient.id=P1",
            "id": "P1",
            "date_of_birth": "1978-03-14",
        }
        triples = builder.build(row, patient_definition)
        subjects = {s for s, p, o in triples}
        subject = URIRef(f"{BASE}patient/{hash_identifier('P1')}")
        assert (subject, RDF.type, URIRef(PATIENT_CLASS)) in triples

    def test_primary_class_emitted_even_when_row_uri_missing(self, builder, patient_definition):
        """Even without _ontology_class_uri in the row, the definition's primary_class_uri
        must be asserted — every row in gold_patient is a Patient."""
        row = {
            "_bronze_source_ref": "raw_patient.id=P1",
            "id": "P1",
            "date_of_birth": "1978-03-14",
        }
        triples = builder.build(row, patient_definition)
        rdf_type_triples = [(s, p, o) for s, p, o in triples if p == RDF.type]
        assert len(rdf_type_triples) == 1
        assert str(rdf_type_triples[0][2]) == PATIENT_CLASS


class TestSubjectUri:
    def test_uses_id_column_when_present(self, builder, patient_definition):
        row = {
            "_ontology_class_uri": PATIENT_CLASS,
            "_bronze_source_ref": "raw_patient.id=P1",
            "id": "P1",
        }
        triples = builder.build(row, patient_definition)
        subjects = {str(s) for s, p, o in triples}
        assert f"{BASE}patient/{hash_identifier('P1')}" in subjects

    def test_falls_back_to_bronze_source_ref_when_no_id(
        self, builder, patient_definition
    ):
        row = {
            "_ontology_class_uri": PATIENT_CLASS,
            "_bronze_source_ref": "raw_patient.id=P1",
            # no "id" column
        }
        triples = builder.build(row, patient_definition)
        subjects = {str(s) for s, p, o in triples}
        # Subject keys off the PK value extracted from bronze_source_ref ("…=P1" → "P1"),
        # giving a clean, stable URI rather than embedding the whole "{table}.{key}=" ref.
        assert f"{BASE}patient/{hash_identifier('P1')}" in subjects

    def test_id_column_not_in_row_falls_back_to_pk_value_from_bsr(self, builder, patient_definition):
        """athletes-style: id_column is the detected PK but that column was not projected
        into gold → subject still keys off the PK value carried in bronze_source_ref."""
        defn = patient_definition.model_copy(update={"id_column": "code"})
        row = {"_ontology_class_uri": PATIENT_CLASS, "_bronze_source_ref": "raw_patient.code=ZZ9"}
        subjects = {str(s) for s, p, o in builder.build(row, defn)}
        assert f"{BASE}patient/{hash_identifier('ZZ9')}" in subjects


class TestDatatypeProperty:
    def test_non_null_value_emits_triple(self, builder, patient_definition):
        row = {
            "_ontology_class_uri": PATIENT_CLASS,
            "_bronze_source_ref": "raw_patient.id=P1",
            "id": "P1",
            "date_of_birth": "1978-03-14",
        }
        triples = builder.build(row, patient_definition)
        prop = URIRef("http://ex.org/ontology#dateOfBirth")
        prop_triples = [(s, p, o) for s, p, o in triples if p == prop]
        assert len(prop_triples) == 1
        assert prop_triples[0][2] == Literal("1978-03-14", datatype=XSD.date)

    def test_null_value_produces_no_triple(self, builder, patient_definition):
        row = {
            "_ontology_class_uri": PATIENT_CLASS,
            "_bronze_source_ref": "raw_patient.id=P1",
            "id": "P1",
            "date_of_birth": None,
        }
        triples = builder.build(row, patient_definition)
        prop = URIRef("http://ex.org/ontology#dateOfBirth")
        assert not any(p == prop for s, p, o in triples)


class TestObjectProperty:
    def test_fk_column_emits_uri_object(
        self, builder, encounter_definition
    ):
        row = {
            "_ontology_class_uri": ENCOUNTER_CLASS,
            "_bronze_source_ref": "raw_encounter.id=E1",
            "patient": "P1",
        }
        triples = builder.build(row, encounter_definition)
        prop = URIRef("http://ex.org/ontology#hasPatient")
        obj_triples = [(s, p, o) for s, p, o in triples if p == prop]
        assert len(obj_triples) == 1
        # Object should be a URI (not a Literal)
        assert isinstance(obj_triples[0][2], URIRef)
        assert str(obj_triples[0][2]) == f"{BASE}patient/{hash_identifier('P1')}"


class TestObjectPropertyDirection:
    """Tests for domain/range-driven subject/object direction."""

    def test_no_flip_without_ontology_graph(
        self, uri_factory, patient_definition, encounter_definition
    ):
        """Without an ontology graph the original subject is always the row entity."""
        builder = TripleBuilder(
            definitions=[patient_definition, encounter_definition],
            uri_factory=uri_factory,
        )
        row = {
            "_ontology_class_uri": ENCOUNTER_CLASS,
            "_bronze_source_ref": "raw_encounter.id=E1",
            "id": "E1",
            "patient": "P1",
        }
        triples = builder.build(row, encounter_definition)
        prop = URIRef("http://ex.org/ontology#hasPatient")
        obj_triples = [(s, p, o) for s, p, o in triples if p == prop]
        assert len(obj_triples) == 1
        # No flip: encounter is subject, patient is object
        assert str(obj_triples[0][0]) == f"{BASE}encounter/{hash_identifier('E1')}"
        assert str(obj_triples[0][2]) == f"{BASE}patient/{hash_identifier('P1')}"

    def test_flip_when_entity_matches_range(
        self, uri_factory, patient_definition, encounter_definition
    ):
        """When the property's range == current entity's class, flip s/o.

        patientHasEncounter: range=Encounter.
        Processing an encounter row → current entity IS the range → flip.
        Result: <patient/P1> patientHasEncounter <encounter/E1>
        """
        # Use a property where range = Encounter (the entity being processed)
        PROP = "http://ex.org/ontology#patientHasEncounter"
        flipped_encounter_def = GoldTableDefinition(
            gold_table_name="gold_encounter",
            primary_class_uri=ENCOUNTER_CLASS,
            class_label="encounter",
            source_silver_tables=["silver_raw_encounter"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_encounter",
                    source_column="patient",
                    gold_column_name="patient",
                    source_data_type="VARCHAR(50)",
                    property_uri=PROP,
                ),
            ],
        )
        ontology = _ontology_with_range(PROP, ENCOUNTER_CLASS)
        builder = TripleBuilder(
            definitions=[patient_definition, flipped_encounter_def],
            uri_factory=uri_factory,
            ontology_graph=ontology,
        )
        row = {
            "_ontology_class_uri": ENCOUNTER_CLASS,
            "_bronze_source_ref": "raw_encounter.id=E1",
            "id": "E1",
            "patient": "P1",
        }
        triples = builder.build(row, flipped_encounter_def)
        prop = URIRef(PROP)
        obj_triples = [(s, p, o) for s, p, o in triples if p == prop]
        assert len(obj_triples) == 1
        # Flipped: patient URI is subject, encounter URI is object
        assert str(obj_triples[0][0]) == f"{BASE}patient/{hash_identifier('P1')}"
        assert str(obj_triples[0][2]) == f"{BASE}encounter/{hash_identifier('E1')}"

    def test_no_flip_when_range_does_not_match_entity(
        self, uri_factory, patient_definition, encounter_definition
    ):
        """When range != current entity's class, no flip — normal direction."""
        # hasPatient: range=Patient. Processing encounter (≠ Patient) → no flip.
        ontology = _ontology_with_range(
            "http://ex.org/ontology#hasPatient", PATIENT_CLASS
        )
        builder = TripleBuilder(
            definitions=[patient_definition, encounter_definition],
            uri_factory=uri_factory,
            ontology_graph=ontology,
        )
        row = {
            "_ontology_class_uri": ENCOUNTER_CLASS,
            "_bronze_source_ref": "raw_encounter.id=E1",
            "id": "E1",
            "patient": "P1",
        }
        triples = builder.build(row, encounter_definition)
        prop = URIRef("http://ex.org/ontology#hasPatient")
        obj_triples = [(s, p, o) for s, p, o in triples if p == prop]
        assert len(obj_triples) == 1
        # No flip: encounter is subject
        assert str(obj_triples[0][0]) == f"{BASE}encounter/{hash_identifier('E1')}"
        assert str(obj_triples[0][2]) == f"{BASE}patient/{hash_identifier('P1')}"

    def test_generalises_to_other_domains(self, uri_factory):
        """dogHasFleas (range=Flea) processing a flea row → flip."""
        DOG_CLASS = "http://ex.org/vet#Dog"
        FLEA_CLASS = "http://ex.org/vet#Flea"
        PROP = "http://ex.org/vet#dogHasFleas"

        dog_def = GoldTableDefinition(
            gold_table_name="gold_dog",
            primary_class_uri=DOG_CLASS,
            class_label="dog",
            source_silver_tables=["silver_dog"],
            column_specs=[],
        )
        flea_def = GoldTableDefinition(
            gold_table_name="gold_flea",
            primary_class_uri=FLEA_CLASS,
            class_label="flea",
            source_silver_tables=["silver_flea"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_flea",
                    source_column="dog",
                    gold_column_name="dog",
                    source_data_type="VARCHAR(50)",
                    property_uri=PROP,
                ),
            ],
        )
        ontology = _ontology_with_range(PROP, FLEA_CLASS)
        builder = TripleBuilder(
            definitions=[dog_def, flea_def],
            uri_factory=uri_factory,
            ontology_graph=ontology,
        )
        row = {
            "_ontology_class_uri": FLEA_CLASS,
            "_bronze_source_ref": "raw_flea.id=F1",
            "id": "F1",
            "dog": "D1",
        }
        triples = builder.build(row, flea_def)
        prop = URIRef(PROP)
        obj_triples = [(s, p, o) for s, p, o in triples if p == prop]
        assert len(obj_triples) == 1
        # Flipped: dog is subject, flea is object
        assert str(obj_triples[0][0]) == f"{BASE}dog/{hash_identifier('D1')}"
        assert str(obj_triples[0][2]) == f"{BASE}flea/{hash_identifier('F1')}"


class TestXsdTypeMapping:
    @pytest.mark.parametrize(
        "source_type,value,expected",
        [
            ("INT", 42, XSD.integer),
            ("BIGINT", 1000, XSD.integer),
            ("SMALLINT", 5, XSD.integer),
            ("TINYINT", 1, XSD.integer),
            ("DECIMAL(10,2)", "9.99", XSD.decimal),
            ("NUMERIC(8,4)", "1.5000", XSD.decimal),
            ("FLOAT", 3.14, XSD.double),
            ("DOUBLE", 2.71, XSD.double),
            ("BOOLEAN", True, XSD.boolean),
            ("BIT", 0, XSD.boolean),
            ("DATE", "2023-01-15", XSD.date),
            ("DATE", "not-a-date", XSD.string),
            ("DATETIME", "2023-01-15T10:30:00", XSD.dateTime),
            ("DATETIME", "2023-01-15 10:30:00", XSD.dateTime),   # MySQL space format
            ("DATETIME", "not-a-datetime", XSD.string),
            ("TIMESTAMP", "2023-01-15T10:30:00", XSD.dateTime),
            ("TIMESTAMP", "2023-01-15 10:30:00", XSD.dateTime),  # MySQL space format
            ("VARCHAR(50)", "hello", XSD.string),
            ("TEXT", "world", XSD.string),
            ("CHAR(10)", "abc", XSD.string),
            # Numeric sniffing for string column types
            ("TEXT", "142.58", XSD.decimal),
            ("TEXT", "0.00", XSD.decimal),
            ("TEXT", "-99.5", XSD.decimal),
            ("VARCHAR(50)", "12345", XSD.integer),
            ("VARCHAR(50)", "-7", XSD.integer),
            ("TEXT", "0", XSD.integer),
            # Non-numeric strings stay as xsd:string
            ("TEXT", "1.2.3", XSD.string),
            ("TEXT", "12e4", XSD.string),
            ("TEXT", "", XSD.string),
            ("VARCHAR(50)", "N/A", XSD.string),
            # None value: no sniffing, falls back to xsd:string
            ("TEXT", None, XSD.string),
        ],
    )
    def test_xsd_type(self, source_type, value, expected):
        assert _xsd_type(source_type, value) == expected


class TestNormaliseLiteral:
    def test_mysql_datetime_normalised_to_iso(self):
        assert _normalise_literal("2150-08-22 17:33:00", XSD.dateTime) == "2150-08-22T17:33:00"

    def test_already_iso_unchanged(self):
        assert _normalise_literal("2150-08-22T17:33:00", XSD.dateTime) == "2150-08-22T17:33:00"

    def test_non_datetime_type_unchanged(self):
        assert _normalise_literal("2150-08-22 17:33:00", XSD.string) == "2150-08-22 17:33:00"

    def test_non_string_value_unchanged(self):
        assert _normalise_literal(42, XSD.dateTime) == 42


class TestSuperclassMap:
    def _make_graph(self) -> Graph:
        g = Graph()
        emergency = URIRef("http://ex.org/AdmissionTypeEmergency")
        admission  = URIRef("http://ex.org/Admission")
        entity     = URIRef("http://ex.org/Entity")
        g.add((emergency, RDFS.subClassOf, admission))
        g.add((admission,  RDFS.subClassOf, entity))
        return g

    def test_direct_parent_found(self):
        g = self._make_graph()
        m = _build_superclass_map(g)
        assert "http://ex.org/Admission" in m["http://ex.org/AdmissionTypeEmergency"]

    def test_transitive_ancestor_found(self):
        """Two-level hierarchy: EmergencyAdmission → Admission → Entity; Entity must appear."""
        g = self._make_graph()
        m = _build_superclass_map(g)
        assert "http://ex.org/Entity" in m["http://ex.org/AdmissionTypeEmergency"]

    def test_own_level_parent_found(self):
        g = self._make_graph()
        m = _build_superclass_map(g)
        assert "http://ex.org/Entity" in m["http://ex.org/Admission"]

    def test_owl_thing_excluded(self):
        g = self._make_graph()
        g.add((URIRef("http://ex.org/Admission"), RDFS.subClassOf, OWL.Thing))
        m = _build_superclass_map(g)
        assert str(OWL.Thing) not in m.get("http://ex.org/Admission", [])

    def test_superclass_triples_emitted(self):
        """TripleBuilder emits discriminated type plus all transitive ancestor types."""
        g = self._make_graph()
        g.add((URIRef("http://ex.org/AdmissionTypeEmergency"), RDF.type, OWL.Class))

        uri_factory = UriFactory("http://mesh.local/data/", source_name="")
        defn = GoldTableDefinition(
            gold_table_name="gold_admission",
            primary_class_uri="http://ex.org/Admission",
            class_label="admission",
            source_silver_tables=[],
            column_specs=[],
        )
        builder = TripleBuilder([defn], uri_factory, ontology_graph=g)
        triples = builder.build(
            {"_ontology_class_uri": "http://ex.org/AdmissionTypeEmergency", "id": "42"},
            defn,
        )
        type_objects = {str(o) for s, p, o in triples if p == RDF.type}
        assert "http://ex.org/AdmissionTypeEmergency" in type_objects
        assert "http://ex.org/Admission" in type_objects
        # Transitive: Entity is two levels up from AdmissionTypeEmergency.
        assert "http://ex.org/Entity" in type_objects


class TestPrimaryClassAlwaysEmitted:
    """Primary class URI must appear as rdf:type regardless of discriminated type shape."""

    def test_named_individual_discriminator_emits_primary_class(self):
        """When _ontology_class_uri is a NamedIndividual (no rdfs:subClassOf),
        definition.primary_class_uri is still emitted as rdf:type."""
        ADMISSION_CLASS = "http://ex.org/Admission"
        ADM_TYPE_EMERG = "http://ex.org/AdmissionTypeEmergency"

        g = Graph()
        # NamedIndividual: no rdfs:subClassOf declared — superclass_map has no path to Admission.
        g.add((URIRef(ADM_TYPE_EMERG), RDF.type, OWL.NamedIndividual))

        defn = GoldTableDefinition(
            gold_table_name="gold_admission",
            primary_class_uri=ADMISSION_CLASS,
            class_label="admission",
            source_silver_tables=[],
            column_specs=[],
        )
        builder = TripleBuilder([defn], UriFactory("http://mesh.local/data/"), ontology_graph=g)
        triples = builder.build(
            {"_ontology_class_uri": ADM_TYPE_EMERG, "id": "42"},
            defn,
        )
        type_objects = {str(o) for s, p, o in triples if p == RDF.type}
        assert ADM_TYPE_EMERG in type_objects
        assert ADMISSION_CLASS in type_objects

    def test_no_class_uri_still_emits_primary_class(self):
        """When _ontology_class_uri is absent, primary_class_uri is emitted."""
        PATIENT_CLASS = "http://ex.org/Patient"
        defn = GoldTableDefinition(
            gold_table_name="gold_patient",
            primary_class_uri=PATIENT_CLASS,
            class_label="patient",
            source_silver_tables=[],
            column_specs=[],
        )
        builder = TripleBuilder([defn], UriFactory("http://mesh.local/data/"))
        triples = builder.build({"id": "P1"}, defn)
        type_objects = {str(o) for s, p, o in triples if p == RDF.type}
        assert PATIENT_CLASS in type_objects

    def test_class_uri_equals_primary_no_duplicate(self):
        """When _ontology_class_uri == primary_class_uri, exactly one rdf:type is emitted."""
        PATIENT_CLASS = "http://ex.org/Patient"
        defn = GoldTableDefinition(
            gold_table_name="gold_patient",
            primary_class_uri=PATIENT_CLASS,
            class_label="patient",
            source_silver_tables=[],
            column_specs=[],
        )
        builder = TripleBuilder([defn], UriFactory("http://mesh.local/data/"))
        triples = builder.build({"_ontology_class_uri": PATIENT_CLASS, "id": "P1"}, defn)
        type_objects = [str(o) for s, p, o in triples if p == RDF.type]
        assert type_objects.count(PATIENT_CLASS) == 1


class TestEntityIdentifierColumn:
    """fm:entityIdentifierColumn annotation drives subject URI when id_column is None."""

    def _make_ontology(self, class_uri: str, source_col: str) -> Graph:
        g = Graph()
        FM_ENTITY_ID = URIRef("http://forgemesh.ai/ontology#entityIdentifierColumn")
        g.add((URIRef(class_uri), FM_ENTITY_ID, Literal(source_col)))
        return g

    def test_entity_id_col_used_for_subject_uri(self):
        """When id_column=None and OWL has fm:entityIdentifierColumn, the annotated column is used."""
        CLS = "http://ex.org/Caregiver"
        defn = GoldTableDefinition(
            gold_table_name="gold_caregiver",
            primary_class_uri=CLS,
            class_label="caregiver",
            source_silver_tables=["silver_raw_caregivers"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_caregivers",
                    source_column="cgid",
                    gold_column_name="cgid",
                    source_data_type="INTEGER",
                    property_uri="http://ex.org/hasCaregiverCgid",
                ),
            ],
            id_column=None,
        )
        ontology = self._make_ontology(CLS, "cgid")
        builder = TripleBuilder([defn], UriFactory("http://mesh.local/data/"), ontology)
        triples = builder.build({"cgid": 22, "_gold_id": 1}, defn)
        subjects = {str(s) for s, _, _ in triples}
        assert f"http://mesh.local/data/caregiver/{hash_identifier('22')}" in subjects
        assert not any("caregiver/1" in str(s) for s, _, _ in triples)

    def test_entity_id_col_missing_from_row_falls_back_to_gold_id(self):
        """If the annotated column is absent from the gold row, _gold_id fallback is used."""
        CLS = "http://ex.org/Caregiver"
        defn = GoldTableDefinition(
            gold_table_name="gold_caregiver",
            primary_class_uri=CLS,
            class_label="caregiver",
            source_silver_tables=["silver_raw_caregivers"],
            column_specs=[],
            id_column=None,
        )
        ontology = self._make_ontology(CLS, "cgid")
        builder = TripleBuilder([defn], UriFactory("http://mesh.local/data/"), ontology)
        triples = builder.build({"_gold_id": 99}, defn)
        subjects = {str(s) for s, _, _ in triples}
        assert f"http://mesh.local/data/caregiver/{hash_identifier('99')}" in subjects

    def test_explicit_id_column_takes_precedence_over_annotation(self):
        """When definition.id_column is set it wins over fm:entityIdentifierColumn."""
        CLS = "http://ex.org/Item"
        defn = GoldTableDefinition(
            gold_table_name="gold_item",
            primary_class_uri=CLS,
            class_label="item",
            source_silver_tables=["silver_raw_d_items"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_d_items",
                    source_column="itemid",
                    gold_column_name="itemid",
                    source_data_type="INTEGER",
                    property_uri="http://ex.org/hasItemItemid",
                ),
            ],
            id_column="itemid",
        )
        ontology = self._make_ontology(CLS, "should_not_be_used")
        builder = TripleBuilder([defn], UriFactory("http://mesh.local/data/"), ontology)
        triples = builder.build({"itemid": 99, "_gold_id": 1}, defn)
        subjects = {str(s) for s, _, _ in triples}
        assert f"http://mesh.local/data/item/{hash_identifier('99')}" in subjects


_FM_NS = "http://forgemesh.ai/ontology#"


class TestProvenanceTriples:
    """Provenance triples must be emitted for every row that has audit columns.

    prove_answer walks triple → gold row via fm:bronzeSourceRef; without this
    predicate every lineage query returns proof_failed with an empty trace.
    """

    def test_bronze_source_ref_emitted(self, builder, patient_definition):
        row = {
            "_ontology_class_uri": PATIENT_CLASS,
            "_bronze_source_ref": "raw_patient.id=P1",
            "_gold_run_id": "run-42",
            "id": "P1",
        }
        triples = builder.build(row, patient_definition)
        preds = {str(p): str(o) for _, p, o in triples}
        assert preds.get(f"{_FM_NS}bronzeSourceRef") == "raw_patient.id=P1"

    def test_gold_run_id_emitted(self, builder, patient_definition):
        row = {
            "_ontology_class_uri": PATIENT_CLASS,
            "_bronze_source_ref": "raw_patient.id=P1",
            "_gold_run_id": "run-42",
            "id": "P1",
        }
        triples = builder.build(row, patient_definition)
        preds = {str(p): str(o) for _, p, o in triples}
        assert preds.get(f"{_FM_NS}goldRunId") == "run-42"

    def test_provenance_omitted_when_columns_absent(self, builder, patient_definition):
        row = {"_ontology_class_uri": PATIENT_CLASS, "id": "P1"}
        triples = builder.build(row, patient_definition)
        pred_uris = {str(p) for _, p, _ in triples}
        assert f"{_FM_NS}bronzeSourceRef" not in pred_uris
        assert f"{_FM_NS}goldRunId" not in pred_uris

    def test_provenance_subject_matches_entity_subject(self, builder, patient_definition):
        row = {
            "_ontology_class_uri": PATIENT_CLASS,
            "_bronze_source_ref": "raw_patient.id=P99",
            "_gold_run_id": "run-7",
            "id": "P99",
        }
        triples = builder.build(row, patient_definition)
        subject = URIRef(f"{BASE}patient/{hash_identifier('P99')}")
        prov_subjects = {s for s, p, _ in triples if str(p).startswith(_FM_NS)}
        assert prov_subjects == {subject}


class TestFKPrimaryKeyGuard:
    """FK path 1 and 2 must only mint object URIs when the column carries the
    target entity's PK value.  A column that merely shares an entity's name
    (path 1) or whose property has range=Entity (path 2) but holds an
    alternative identifier (e.g. IATA code vs numeric ID) must emit a literal,
    not a dangling URI.
    """

    AIRLINE_CLASS = "http://ex.org/ontology#Airline"
    ROUTE_CLASS = "http://ex.org/ontology#Route"
    HAS_ROUTE_AIRLINE = "http://ex.org/ontology#hasRouteAirline"
    HAS_ROUTE_AIRLINE_ID = "http://ex.org/ontology#hasRouteAirlineId"

    def _make_defs(self):
        airline_def = GoldTableDefinition(
            gold_table_name="gold_airline",
            primary_class_uri=self.AIRLINE_CLASS,
            class_label="airline",
            source_silver_tables=["silver_raw_airlines"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_airlines",
                    source_column="airline_id",
                    gold_column_name="airline_id",
                    source_data_type="INTEGER",
                    property_uri="http://ex.org/ontology#hasAirlineId",
                ),
            ],
            id_column="airline_id",
        )
        route_def = GoldTableDefinition(
            gold_table_name="gold_route",
            primary_class_uri=self.ROUTE_CLASS,
            class_label="route",
            source_silver_tables=["silver_raw_routes"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_routes",
                    source_column="airline",        # IATA code — NOT the airline PK
                    gold_column_name="airline",
                    source_data_type="VARCHAR(3)",
                    property_uri=self.HAS_ROUTE_AIRLINE,
                ),
                GoldColumnSpec(
                    silver_table="silver_raw_routes",
                    source_column="airline_id",     # numeric PK — matches gold_airline.id_column
                    gold_column_name="airline_id",
                    source_data_type="INTEGER",
                    property_uri=self.HAS_ROUTE_AIRLINE_ID,
                ),
            ],
        )
        return airline_def, route_def

    def _make_ontology(self):
        g = Graph()
        for prop in (self.HAS_ROUTE_AIRLINE, self.HAS_ROUTE_AIRLINE_ID):
            g.add((URIRef(prop), RDF.type, OWL.ObjectProperty))
            g.add((URIRef(prop), RDFS.range, URIRef(self.AIRLINE_CLASS)))
        return g

    def test_iata_column_with_object_property_not_emitted(self):
        """IATA column whose property is an OWL ObjectProperty (rdfs:range=entity class)
        must emit no triple at all — the column holds an alternative identifier, not a PK.
        Emitting a literal for an ObjectProperty is a semantic error; the *_id sibling
        column handles the correct URI triple."""
        airline_def, route_def = self._make_defs()
        builder = TripleBuilder(
            [airline_def, route_def],
            UriFactory(BASE),
            self._make_ontology(),
        )
        row = {"_ontology_class_uri": self.ROUTE_CLASS, "id": "1", "airline": "2B", "airline_id": 410}
        triples = builder.build(row, route_def)
        airline_triples = [(s, p, o) for s, p, o in triples if str(p) == self.HAS_ROUTE_AIRLINE]
        assert len(airline_triples) == 0, (
            "IATA column must not emit any triple when the property is an ObjectProperty with entity range"
        )

    def test_shared_property_uri_emits_single_uri(self):
        """When IATA and numeric-ID columns share the same property URI (OpenFlights mapping
        artefact where both 'airline' and 'airline_id' map to hasRouteAirlineId), only the
        numeric URI triple is emitted — the IATA literal is suppressed."""
        airline_def, _ = self._make_defs()
        route_def_shared = GoldTableDefinition(
            gold_table_name="gold_route",
            primary_class_uri=self.ROUTE_CLASS,
            class_label="route",
            source_silver_tables=["silver_raw_routes"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_routes",
                    source_column="airline",
                    gold_column_name="airline",
                    source_data_type="VARCHAR(3)",
                    property_uri=self.HAS_ROUTE_AIRLINE_ID,  # same property as numeric col
                ),
                GoldColumnSpec(
                    silver_table="silver_raw_routes",
                    source_column="airline_id",
                    gold_column_name="airline_id",
                    source_data_type="INTEGER",
                    property_uri=self.HAS_ROUTE_AIRLINE_ID,
                ),
            ],
        )
        builder = TripleBuilder(
            [airline_def, route_def_shared],
            UriFactory(BASE),
            self._make_ontology(),
        )
        row = {"_ontology_class_uri": self.ROUTE_CLASS, "id": "1", "airline": "2B", "airline_id": 410}
        triples = builder.build(row, route_def_shared)
        id_triples = [(s, p, o) for s, p, o in triples if str(p) == self.HAS_ROUTE_AIRLINE_ID]
        assert len(id_triples) == 1, f"Expected 1 triple for shared property, got {len(id_triples)}"
        assert isinstance(id_triples[0][2], URIRef), "Shared-property triple must be a URI, not a literal"
        assert str(id_triples[0][2]) == f"{BASE}airline/{hash_identifier('410')}"

    def test_numeric_id_column_emits_object_uri(self):
        airline_def, route_def = self._make_defs()
        builder = TripleBuilder(
            [airline_def, route_def],
            UriFactory(BASE),
            self._make_ontology(),
        )
        row = {"_ontology_class_uri": self.ROUTE_CLASS, "id": "1", "airline": "2B", "airline_id": 410}
        triples = builder.build(row, route_def)
        id_triples = [(s, p, o) for s, p, o in triples if str(p) == self.HAS_ROUTE_AIRLINE_ID]
        assert len(id_triples) == 1
        assert isinstance(id_triples[0][2], URIRef)
        assert str(id_triples[0][2]) == f"{BASE}airline/{hash_identifier('410')}"

    def test_exactly_one_airline_uri_per_route(self):
        airline_def, route_def = self._make_defs()
        builder = TripleBuilder(
            [airline_def, route_def],
            UriFactory(BASE),
            self._make_ontology(),
        )
        row = {"_ontology_class_uri": self.ROUTE_CLASS, "id": "1", "airline": "2B", "airline_id": 410}
        triples = builder.build(row, route_def)
        airline_uri_triples = [
            (s, p, o) for s, p, o in triples
            if isinstance(o, URIRef) and str(o).startswith(f"{BASE}airline/")
        ]
        assert len(airline_uri_triples) == 1, f"Expected 1 airline URI triple, got {len(airline_uri_triples)}"

    def test_entity_without_id_column_still_uses_path1(self):
        """Entities with id_column=None must not be gated — existing path 1 behaviour preserved."""
        airline_def, route_def = self._make_defs()
        # Replace airline_def with one that has no id_column
        airline_no_id = airline_def.model_copy(update={"id_column": None})
        builder = TripleBuilder(
            [airline_no_id, route_def],
            UriFactory(BASE),
        )
        row = {"_ontology_class_uri": self.ROUTE_CLASS, "id": "1", "airline": "2B", "airline_id": 410}
        triples = builder.build(row, route_def)
        # Without id_column guard, path 1 fires for "airline" column → object URI
        airline_triples = [(s, p, o) for s, p, o in triples if str(p) == self.HAS_ROUTE_AIRLINE]
        assert len(airline_triples) == 1
        assert isinstance(airline_triples[0][2], URIRef)


_FM_NS = "http://forgemesh.ai/ontology#"


class TestIncompleteReference:
    """Null FK columns on object properties must emit fm:incompleteReference triples."""

    AIRLINE_CLASS = "http://ex.org/ontology#Airline"
    ROUTE_CLASS = "http://ex.org/ontology#Route"
    HAS_ROUTE_AIRLINE_ID = "http://ex.org/ontology#hasRouteAirlineId"
    FM_INCOMPLETE = f"{_FM_NS}incompleteReference"

    def _make_defs(self):
        airline_def = GoldTableDefinition(
            gold_table_name="gold_airline",
            primary_class_uri=self.AIRLINE_CLASS,
            class_label="airline",
            source_silver_tables=["silver_raw_airlines"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_airlines",
                    source_column="airline_id",
                    gold_column_name="airline_id",
                    source_data_type="INTEGER",
                    property_uri="http://ex.org/ontology#hasAirlineId",
                ),
            ],
            id_column="airline_id",
        )
        route_def = GoldTableDefinition(
            gold_table_name="gold_route",
            primary_class_uri=self.ROUTE_CLASS,
            class_label="route",
            source_silver_tables=["silver_raw_routes"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_routes",
                    source_column="airline_id",
                    gold_column_name="airline_id",
                    source_data_type="INTEGER",
                    property_uri=self.HAS_ROUTE_AIRLINE_ID,
                ),
            ],
        )
        return airline_def, route_def

    def _make_ontology(self):
        g = Graph()
        g.add((URIRef(self.HAS_ROUTE_AIRLINE_ID), RDF.type, OWL.ObjectProperty))
        g.add((URIRef(self.HAS_ROUTE_AIRLINE_ID), RDFS.range, URIRef(self.AIRLINE_CLASS)))
        return g

    def test_null_fk_emits_incomplete_reference(self):
        """Null airline_id → fm:incompleteReference with the property URI as value."""
        airline_def, route_def = self._make_defs()
        builder = TripleBuilder(
            [airline_def, route_def],
            UriFactory(BASE),
            self._make_ontology(),
        )
        row = {"_ontology_class_uri": self.ROUTE_CLASS, "id": "1", "airline_id": None}
        triples = builder.build(row, route_def)
        incomplete = [(s, p, o) for s, p, o in triples if str(p) == self.FM_INCOMPLETE]
        assert len(incomplete) == 1
        assert str(incomplete[0][2]) == self.HAS_ROUTE_AIRLINE_ID

    def test_non_null_fk_emits_no_incomplete_reference(self):
        """Present airline_id → no fm:incompleteReference triple."""
        airline_def, route_def = self._make_defs()
        builder = TripleBuilder(
            [airline_def, route_def],
            UriFactory(BASE),
            self._make_ontology(),
        )
        row = {"_ontology_class_uri": self.ROUTE_CLASS, "id": "1", "airline_id": 410}
        triples = builder.build(row, route_def)
        incomplete = [(s, p, o) for s, p, o in triples if str(p) == self.FM_INCOMPLETE]
        assert len(incomplete) == 0

    def test_null_non_fk_column_emits_no_incomplete_reference(self):
        """Null value on a plain data property (no entity range) must not emit fm:incompleteReference."""
        _, route_def = self._make_defs()
        route_with_stops = route_def.model_copy(update={
            "column_specs": [
                GoldColumnSpec(
                    silver_table="silver_raw_routes",
                    source_column="stops",
                    gold_column_name="stops",
                    source_data_type="INTEGER",
                    property_uri="http://ex.org/ontology#hasRouteStops",
                ),
            ]
        })
        builder = TripleBuilder([route_with_stops], UriFactory(BASE))
        row = {"_ontology_class_uri": self.ROUTE_CLASS, "id": "1", "stops": None}
        triples = builder.build(row, route_with_stops)
        incomplete = [(s, p, o) for s, p, o in triples if str(p) == self.FM_INCOMPLETE]
        assert len(incomplete) == 0

    def test_incomplete_reference_subject_matches_entity(self):
        """fm:incompleteReference must be on the route subject, not some other node."""
        airline_def, route_def = self._make_defs()
        builder = TripleBuilder(
            [airline_def, route_def],
            UriFactory(BASE),
            self._make_ontology(),
        )
        row = {
            "_ontology_class_uri": self.ROUTE_CLASS,
            "_bronze_source_ref": "raw_route.id=99",
            "id": "99",
            "airline_id": None,
        }
        triples = builder.build(row, route_def)
        expected_subject = UriFactory(BASE).individual("route", "99")
        incomplete = [(s, p, o) for s, p, o in triples if str(p) == self.FM_INCOMPLETE]
        assert len(incomplete) == 1
        assert incomplete[0][0] == expected_subject


# ---------------------------------------------------------------------------
# BUG-01 — UUID FK columns must mint object-property URIs
# ---------------------------------------------------------------------------

class TestUUIDForeignKey:
    """UUID-valued FK columns must be recognised as object-property references
    even when the column name has no '_id' suffix and the target entity has a
    known id_column (BUG-01).

    The guard condition was:
        target_id_col is None OR col.endswith("_id")
    This blocked UUID schemas (Synthea-style) where the PK column is bare "id"
    and FK columns like "patient", "organization" carry UUID values.

    The fix: also pass the guard when the value matches _UUID_RE.
    """

    PATIENT_CLASS  = "http://ex.org/ontology#Patient"
    ENCOUNTER_CLASS = "http://ex.org/ontology#Encounter"
    HAS_ENCOUNTER_PATIENT = "http://ex.org/ontology#hasEncounterPatient"

    SAMPLE_UUID = "a123b456-c789-d012-e345-f67890abcdef"

    def _make_defs_uuid_keyed(self):
        """Patient entity keyed by a bare 'id' UUID column (Synthea-style).
        id_column is explicitly set to "id" — as refine_id_columns would produce.
        """
        patient_def = GoldTableDefinition(
            gold_table_name="gold_patient",
            primary_class_uri=self.PATIENT_CLASS,
            class_label="patient",
            source_silver_tables=["silver_raw_patients"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_patients",
                    source_column="id",
                    gold_column_name="id",
                    source_data_type="VARCHAR(50)",
                    property_uri="http://ex.org/ontology#patientId",
                ),
            ],
            id_column="id",          # <-- explicitly set; triggers the old bug
        )
        encounter_def = GoldTableDefinition(
            gold_table_name="gold_encounter",
            primary_class_uri=self.ENCOUNTER_CLASS,
            class_label="encounter",
            source_silver_tables=["silver_raw_encounters"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_encounters",
                    source_column="patient",      # no _id suffix — Synthea style
                    gold_column_name="patient",
                    source_data_type="VARCHAR(50)",
                    property_uri=self.HAS_ENCOUNTER_PATIENT,
                ),
            ],
        )
        return patient_def, encounter_def

    def _make_ontology(self):
        g = Graph()
        g.add((URIRef(self.HAS_ENCOUNTER_PATIENT), RDF.type, OWL.ObjectProperty))
        g.add((URIRef(self.HAS_ENCOUNTER_PATIENT), RDFS.range, URIRef(self.PATIENT_CLASS)))
        return g

    def test_uuid_fk_mints_object_uri(self):
        """A UUID value in a column that matches an entity name (path 1) must produce
        an object-property URI triple, not a literal."""
        patient_def, encounter_def = self._make_defs_uuid_keyed()
        builder = TripleBuilder(
            [patient_def, encounter_def],
            UriFactory(BASE),
            self._make_ontology(),
        )
        row = {
            "_ontology_class_uri": self.ENCOUNTER_CLASS,
            "_bronze_source_ref": "raw_encounters.id=E1",
            "id": "E1",
            "patient": self.SAMPLE_UUID,
        }
        triples = builder.build(row, encounter_def)
        prop = URIRef(self.HAS_ENCOUNTER_PATIENT)
        fk_triples = [(s, p, o) for s, p, o in triples if p == prop]
        assert len(fk_triples) == 1, (
            f"Expected 1 FK triple for UUID patient column, got {len(fk_triples)}"
        )
        assert isinstance(fk_triples[0][2], URIRef), "FK value must be a URI, not a Literal"
        assert str(fk_triples[0][2]) == f"{BASE}patient/{hash_identifier(self.SAMPLE_UUID)}"

    def test_uuid_fk_via_range_path(self):
        """UUID FK via path 2 (range-based): property range identifies the target entity
        even when the column name doesn't match the entity name directly."""
        patient_def, _ = self._make_defs_uuid_keyed()
        # Use a column named "assigned_to" that maps via rdfs:range to Patient
        PROP = "http://ex.org/ontology#hasAssignedPatient"
        encounter_def_range = GoldTableDefinition(
            gold_table_name="gold_encounter",
            primary_class_uri=self.ENCOUNTER_CLASS,
            class_label="encounter",
            source_silver_tables=["silver_raw_encounters"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_encounters",
                    source_column="assigned_to",  # name does NOT match entity
                    gold_column_name="assigned_to",
                    source_data_type="VARCHAR(50)",
                    property_uri=PROP,
                ),
            ],
        )
        g = Graph()
        g.add((URIRef(PROP), RDF.type, OWL.ObjectProperty))
        g.add((URIRef(PROP), RDFS.range, URIRef(self.PATIENT_CLASS)))
        builder = TripleBuilder(
            [patient_def, encounter_def_range],
            UriFactory(BASE),
            g,
        )
        row = {
            "_ontology_class_uri": self.ENCOUNTER_CLASS,
            "id": "E1",
            "assigned_to": self.SAMPLE_UUID,
        }
        triples = builder.build(row, encounter_def_range)
        prop_triples = [(s, p, o) for s, p, o in triples if p == URIRef(PROP)]
        assert len(prop_triples) == 1
        assert isinstance(prop_triples[0][2], URIRef)

    def test_short_code_column_still_blocked_when_id_col_set(self):
        """A non-UUID short-code value (e.g. IATA '2B') in a column matching an entity
        name must still be blocked when id_column is set — UUID guard must not fire."""
        patient_def, _ = self._make_defs_uuid_keyed()
        # Replace encounter's patient column with an IATA-style short code
        AIRLINE_CLASS = "http://ex.org/ontology#Airline"
        airline_def = GoldTableDefinition(
            gold_table_name="gold_airline",
            primary_class_uri=AIRLINE_CLASS,
            class_label="airline",
            source_silver_tables=["silver_raw_airlines"],
            column_specs=[],
            id_column="airline_id",   # id_column explicitly set
        )
        route_def = GoldTableDefinition(
            gold_table_name="gold_route",
            primary_class_uri="http://ex.org/ontology#Route",
            class_label="route",
            source_silver_tables=["silver_raw_routes"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_routes",
                    source_column="airline",  # IATA code like "2B" — NOT a UUID
                    gold_column_name="airline",
                    source_data_type="VARCHAR(3)",
                    property_uri="http://ex.org/ontology#hasRouteAirline",
                ),
            ],
        )
        builder = TripleBuilder([airline_def, route_def], UriFactory(BASE))
        row = {"_ontology_class_uri": "http://ex.org/ontology#Route", "id": "1", "airline": "2B"}
        triples = builder.build(row, route_def)
        # "2B" is not a UUID → guard must block URI minting
        fk_triples = [
            (s, p, o) for s, p, o in triples
            if str(p) == "http://ex.org/ontology#hasRouteAirline"
        ]
        # Path 1 fires because 'airline' matches entity 'airline',
        # but guard blocks because '2B' is not a UUID and col has no _id suffix.
        # No airline URI triple should be emitted.
        for _, _, o in fk_triples:
            assert not isinstance(o, URIRef) or not str(o).startswith(f"{BASE}airline/"), (
                "IATA code '2B' must not produce an airline object URI"
            )

    def test_null_uuid_fk_emits_quality_triple(self):
        """Null value on a UUID FK column (with ObjectProperty declaration) must emit
        fm:incompleteReference — BUG-03 fix covers this via Strategy A."""
        patient_def, encounter_def = self._make_defs_uuid_keyed()
        builder = TripleBuilder(
            [patient_def, encounter_def],
            UriFactory(BASE),
            self._make_ontology(),
        )
        row = {
            "_ontology_class_uri": self.ENCOUNTER_CLASS,
            "id": "E1",
            "patient": None,
        }
        triples = builder.build(row, encounter_def)
        incomplete = [
            (s, p, o) for s, p, o in triples
            if str(p) == f"{_FM_NS}incompleteReference"
        ]
        assert len(incomplete) == 1
        assert str(incomplete[0][2]) == self.HAS_ENCOUNTER_PATIENT


# ---------------------------------------------------------------------------
# Bare-PK FK guard — cgid / itemid pattern (MIMIC-style schemas)
# ---------------------------------------------------------------------------

class TestBarePkForeignKey:
    """Path 2 (range-based) must produce URI objects for FK columns whose names
    exactly match the target entity's id_column, even without an '_id' suffix.

    MIMIC-III example: raw_chartevents.cgid → raw_caregivers.cgid.
    'cgid' does not end in '_id' and is not a UUID, but it IS the caregiver's PK.
    refine_id_columns() detects cgid as unique → id_column='cgid'.
    The guard must fire via 'target_id_col == spec.source_column'.
    """

    CAREGIVER_CLASS = "http://ex.org/ontology#Caregiver"
    CHARTEVENT_CLASS = "http://ex.org/ontology#Chartevent"
    HAS_CGID = "http://ex.org/ontology#hasCharteventCgid"

    def _make_defs(self):
        caregiver_def = GoldTableDefinition(
            gold_table_name="gold_caregiver",
            primary_class_uri=self.CAREGIVER_CLASS,
            class_label="caregiver",
            source_silver_tables=["silver_raw_caregivers"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_caregivers",
                    source_column="cgid",
                    gold_column_name="cgid",
                    source_data_type="INT",
                    property_uri="http://ex.org/ontology#hasCaregiverCgid",
                ),
            ],
            id_column="cgid",  # set by refine_id_columns — bare PK, no _id suffix
        )
        chartevent_def = GoldTableDefinition(
            gold_table_name="gold_chartevent",
            primary_class_uri=self.CHARTEVENT_CLASS,
            class_label="chartevent",
            source_silver_tables=["silver_raw_chartevents"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_chartevents",
                    source_column="cgid",
                    gold_column_name="cgid",
                    source_data_type="INT",
                    property_uri=self.HAS_CGID,
                ),
            ],
        )
        g = Graph()
        g.add((URIRef(self.HAS_CGID), RDF.type, OWL.ObjectProperty))
        g.add((URIRef(self.HAS_CGID), RDFS.range, URIRef(self.CAREGIVER_CLASS)))
        return caregiver_def, chartevent_def, g

    def test_bare_pk_fk_mints_uri_not_literal(self):
        """cgid (no _id suffix) must produce a URI object when target entity's
        id_column is also 'cgid' — target_id_col == spec.source_column fires."""
        caregiver_def, chartevent_def, g = self._make_defs()
        builder = TripleBuilder([caregiver_def, chartevent_def], UriFactory(BASE), g)
        row = {
            "_ontology_class_uri": self.CHARTEVENT_CLASS,
            "_bronze_source_ref": "raw_chartevents.row_id=1",
            "cgid": 16027,
        }
        triples = builder.build(row, chartevent_def)
        prop = URIRef(self.HAS_CGID)
        fk_triples = [(s, p, o) for s, p, o in triples if p == prop]
        assert len(fk_triples) == 1, f"Expected 1 FK triple, got {len(fk_triples)}"
        assert isinstance(fk_triples[0][2], URIRef), (
            f"cgid FK must be a URIRef, got {type(fk_triples[0][2]).__name__}: {fk_triples[0][2]}"
        )
        assert str(fk_triples[0][2]) == f"{BASE}caregiver/{hash_identifier('16027')}"

    def test_bare_pk_fk_uri_matches_entity_subject(self):
        """The URI minted for the FK must equal the caregiver's own subject URI."""
        caregiver_def, _, g = self._make_defs()
        builder = TripleBuilder([caregiver_def], UriFactory(BASE), g)
        caregiver_row = {
            "_ontology_class_uri": self.CAREGIVER_CLASS,
            "_bronze_source_ref": "raw_caregivers._idx=100",
            "cgid": 16027,
        }
        triples = builder.build(caregiver_row, caregiver_def)
        subjects = {str(s) for s, _, _ in triples}
        # The caregiver subject must be caregiver/16027
        assert f"{BASE}caregiver/{hash_identifier('16027')}" in subjects, (
            f"Caregiver subject not found. Subjects: {subjects}"
        )

    def test_non_pk_column_still_guarded(self):
        """A column whose name does NOT match the target entity's id_column must NOT
        mint a URI — it carries an alternative identifier (like an IATA code)."""
        AIRLINE_CLASS = "http://ex.org/ontology#Airline"
        ROUTE_CLASS = "http://ex.org/ontology#Route"
        HAS_AIRLINE = "http://ex.org/ontology#hasRouteAirline"
        airline_def = GoldTableDefinition(
            gold_table_name="gold_airline",
            primary_class_uri=AIRLINE_CLASS,
            class_label="airline",
            source_silver_tables=["silver_raw_airlines"],
            column_specs=[],
            id_column="airline_id",
        )
        route_def = GoldTableDefinition(
            gold_table_name="gold_route",
            primary_class_uri=ROUTE_CLASS,
            class_label="route",
            source_silver_tables=["silver_raw_routes"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_routes",
                    source_column="airline",   # IATA code, NOT the numeric PK
                    gold_column_name="airline",
                    source_data_type="VARCHAR(3)",
                    property_uri=HAS_AIRLINE,
                ),
            ],
        )
        g = Graph()
        g.add((URIRef(HAS_AIRLINE), RDF.type, OWL.ObjectProperty))
        g.add((URIRef(HAS_AIRLINE), RDFS.range, URIRef(AIRLINE_CLASS)))
        builder = TripleBuilder([airline_def, route_def], UriFactory(BASE), g)
        row = {
            "_ontology_class_uri": ROUTE_CLASS,
            "_bronze_source_ref": "raw_routes.id=1",
            "airline": "AA",  # IATA code — should NOT become a URI
        }
        triples = builder.build(row, route_def)
        prop = URIRef(HAS_AIRLINE)
        fk_triples = [(s, p, o) for s, p, o in triples if p == prop]
        # "airline" (IATA code) with id_column="airline_id" → guard fails → skipped
        assert len(fk_triples) == 0, (
            "IATA code in 'airline' column must not be emitted as a URI object property"
        )


# ---------------------------------------------------------------------------
# BUG-02 — Class URI emitted as ObjectProperty object must be guarded
# ---------------------------------------------------------------------------

class TestClassUriAsObjectGuard:
    """When a value_map or a pre-materialised gold column value is the property's
    rdfs:range class URI (not an individual URI), emitting it as an object property
    target corrupts join queries.  The triple builder must emit fm:invalidMapping
    instead (BUG-02).

    fm:invalidMapping is distinct from fm:incompleteReference (null FK).  The source
    value is present but the mapping was wrong — so C5 incompleteness counts are not
    affected.

    Root cause: the gold pipeline's value_mapper resolved payer UUIDs to the Payer
    class URI (the LLM mapped them that way).  The gold column stores a http:// URI
    equal to the range class.  The triple builder's existing 'startswith http://'
    guard emitted it as a URIRef — pointing every triple at the class node itself.
    """

    PAYER_CLASS  = "http://ex.org/ontology#Payer"
    ENCOUNTER_CLASS = "http://ex.org/ontology#Encounter"
    HAS_PAYER = "http://ex.org/ontology#hasEncounterPayer"
    FM_INVALID_MAPPING = f"{_FM_NS}invalidMapping"

    def _make_defs(self):
        """Encounter has a 'payer' column whose property range is Payer,
        but Payer has no gold table (hence no entry in class_to_entity)."""
        encounter_def = GoldTableDefinition(
            gold_table_name="gold_encounter",
            primary_class_uri=self.ENCOUNTER_CLASS,
            class_label="encounter",
            source_silver_tables=["silver_raw_encounters"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_encounters",
                    source_column="payer",
                    gold_column_name="payer",
                    source_data_type="VARCHAR(200)",
                    property_uri=self.HAS_PAYER,
                ),
            ],
        )
        return [encounter_def]   # Payer has NO gold table — not in definitions

    def _make_ontology(self):
        g = Graph()
        g.add((URIRef(self.HAS_PAYER), RDF.type, OWL.ObjectProperty))
        g.add((URIRef(self.HAS_PAYER), RDFS.range, URIRef(self.PAYER_CLASS)))
        return g

    def test_gold_column_stores_range_class_uri_emits_quality_triple(self):
        """Gold column value IS the range class URI (pre-materialised by gold pipeline).
        Must not emit (encounter, hasEncounterPayer, PayerClass) — must emit
        fm:invalidMapping instead (BUG-02, mapping error not missing value)."""
        defs = self._make_defs()
        builder = TripleBuilder(defs, UriFactory(BASE), self._make_ontology())
        row = {
            "_ontology_class_uri": self.ENCOUNTER_CLASS,
            "id": "E1",
            "payer": self.PAYER_CLASS,  # the class URI itself stored as the column value
        }
        triples = builder.build(row, defs[0])
        payer_triples = [(s, p, o) for s, p, o in triples if p == URIRef(self.HAS_PAYER)]
        assert len(payer_triples) == 0, (
            "hasEncounterPayer triple with class URI as object must not be emitted"
        )
        invalid = [(s, p, o) for s, p, o in triples if str(p) == self.FM_INVALID_MAPPING]
        assert len(invalid) == 1
        assert str(invalid[0][2]) == self.HAS_PAYER
        # Must NOT appear as fm:incompleteReference (that predicate is for null FKs only)
        FM_INCOMPLETE = f"{_FM_NS}incompleteReference"
        assert not any(str(p) == FM_INCOMPLETE for _, p, _ in triples)

    def test_value_map_points_at_range_class_no_gold_table_emits_incomplete(self):
        """value_map resolves to range class URI but that class has no gold table.
        This is an FK-to-missing-entity situation (e.g. Synthea payer with no
        gold_payer), NOT a bad mapping → must emit fm:incompleteReference."""
        defs = self._make_defs()  # gold_encounter only; no gold_payer
        bad_spec = defs[0].column_specs[0].model_copy(update={
            "value_map": {"some-payer-uuid": self.PAYER_CLASS}
        })
        bad_def = defs[0].model_copy(update={"column_specs": [bad_spec]})
        builder = TripleBuilder([bad_def], UriFactory(BASE), self._make_ontology())
        row = {"_ontology_class_uri": self.ENCOUNTER_CLASS, "id": "E1", "payer": "some-payer-uuid"}
        triples = builder.build(row, bad_def)
        payer_triples = [(s, p, o) for s, p, o in triples if p == URIRef(self.HAS_PAYER)]
        assert len(payer_triples) == 0
        FM_INCOMPLETE = f"{_FM_NS}incompleteReference"
        assert any(str(p) == FM_INCOMPLETE for _, p, _ in triples), \
            "expected incompleteReference when range class has no gold table"
        assert not any(str(p) == self.FM_INVALID_MAPPING for _, p, _ in triples)

    def test_value_map_with_gold_table_takes_path1_mints_object_uri(self):
        """When range class HAS a gold table, path-1 entity_map match fires before the
        BUG-02 guard.  The raw payer UUID is minted as an object URI directly; the
        value_map and quality-triple guard are never reached."""
        payer_def = GoldTableDefinition(
            gold_table_name="gold_payer",
            primary_class_uri=self.PAYER_CLASS,
            class_label="payer",
            source_silver_tables=["silver_raw_payers"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_payers",
                    gold_column_name="id", source_column="id",
                    source_data_type="TEXT", property_uri="", is_primary_key=True,
                )
            ],
        )
        base_def = self._make_defs()[0]
        bad_spec = base_def.column_specs[0].model_copy(update={
            "value_map": {"some-payer-uuid": self.PAYER_CLASS}
        })
        bad_def = base_def.model_copy(update={"column_specs": [bad_spec]})
        builder = TripleBuilder([bad_def, payer_def], UriFactory(BASE), self._make_ontology())
        row = {"_ontology_class_uri": self.ENCOUNTER_CLASS, "id": "E1", "payer": "some-payer-uuid"}
        triples = builder.build(row, bad_def)
        # Path 1 fires → object URI is minted; no quality triple expected
        payer_triples = [(s, p, o) for s, p, o in triples if p == URIRef(self.HAS_PAYER)]
        assert len(payer_triples) == 1
        assert isinstance(payer_triples[0][2], URIRef)
        FM_INCOMPLETE = f"{_FM_NS}incompleteReference"
        assert not any(str(p) in (self.FM_INVALID_MAPPING, FM_INCOMPLETE) for _, p, _ in triples)

    def test_individual_uri_not_blocked(self):
        """A http:// URI in the gold column that is NOT the range class URI must still be
        emitted as a URIRef — only the class URI itself is blocked."""
        INDIVIDUAL_URI = "http://ex.org/ontology#PayerBlueCross"  # an individual, not the class
        defs = self._make_defs()
        builder = TripleBuilder(defs, UriFactory(BASE), self._make_ontology())
        row = {
            "_ontology_class_uri": self.ENCOUNTER_CLASS,
            "id": "E1",
            "payer": INDIVIDUAL_URI,
        }
        triples = builder.build(row, defs[0])
        payer_triples = [(s, p, o) for s, p, o in triples if p == URIRef(self.HAS_PAYER)]
        # Individual URI should be emitted as-is
        assert len(payer_triples) == 1
        assert isinstance(payer_triples[0][2], URIRef)
        assert str(payer_triples[0][2]) == INDIVIDUAL_URI

    def test_no_ontology_no_blocking(self):
        """Without an ontology (no _prop_range), the guard cannot fire — column value
        passes through as a URIRef.  No regression from BUG-02 fix."""
        defs = self._make_defs()
        builder = TripleBuilder(defs, UriFactory(BASE))  # no ontology_graph
        row = {
            "_ontology_class_uri": self.ENCOUNTER_CLASS,
            "id": "E1",
            "payer": self.PAYER_CLASS,
        }
        triples = builder.build(row, defs[0])
        # Without ontology the guard is inactive; URI passes through
        payer_triples = [(s, p, o) for s, p, o in triples if p == URIRef(self.HAS_PAYER)]
        assert len(payer_triples) == 1
        assert isinstance(payer_triples[0][2], URIRef)


# ---------------------------------------------------------------------------
# BUG-03 — ObjectProperty null FK must emit quality triple via Strategy A
# ---------------------------------------------------------------------------

class TestObjectPropertyNullQualityTriple:
    """Any null value on an OWL ObjectProperty must emit fm:incompleteReference,
    even when the column name has no '_id' suffix (BUG-03 Strategy A fix).

    Previously: the detection relied on column-name heuristics and only fired for
    *_id columns or columns whose name matched an entity short name with the
    correct _id suffix guard.  For UUID-keyed schemas and for ObjectProperty
    columns with unusual naming, zero quality triples were emitted despite 100s
    of null FK values in the gold table.
    """

    PATIENT_CLASS = "http://ex.org/ontology#Patient"
    ENCOUNTER_CLASS = "http://ex.org/ontology#Encounter"
    HAS_PROVIDER = "http://ex.org/ontology#hasProvider"
    PROVIDER_CLASS = "http://ex.org/ontology#Provider"
    FM_INCOMPLETE = f"{_FM_NS}incompleteReference"

    def _make_setup(self):
        """Provider entity with UUID keying.  Encounter has 'provider' FK column
        (no _id suffix) whose property is an OWL ObjectProperty with range Provider."""
        provider_def = GoldTableDefinition(
            gold_table_name="gold_provider",
            primary_class_uri=self.PROVIDER_CLASS,
            class_label="provider",
            source_silver_tables=["silver_raw_providers"],
            column_specs=[],
            id_column="id",
        )
        encounter_def = GoldTableDefinition(
            gold_table_name="gold_encounter",
            primary_class_uri=self.ENCOUNTER_CLASS,
            class_label="encounter",
            source_silver_tables=["silver_raw_encounters"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_encounters",
                    source_column="provider",
                    gold_column_name="provider",
                    source_data_type="VARCHAR(50)",
                    property_uri=self.HAS_PROVIDER,
                ),
            ],
        )
        g = Graph()
        g.add((URIRef(self.HAS_PROVIDER), RDF.type, OWL.ObjectProperty))
        g.add((URIRef(self.HAS_PROVIDER), RDFS.range, URIRef(self.PROVIDER_CLASS)))
        return provider_def, encounter_def, g

    def test_null_object_property_emits_quality_triple(self):
        """Null 'provider' column (ObjectProperty, no _id suffix) must emit
        fm:incompleteReference — Strategy A fires because prop is in _prop_range."""
        provider_def, encounter_def, g = self._make_setup()
        builder = TripleBuilder([provider_def, encounter_def], UriFactory(BASE), g)
        row = {
            "_ontology_class_uri": self.ENCOUNTER_CLASS,
            "id": "E1",
            "provider": None,
        }
        triples = builder.build(row, encounter_def)
        incomplete = [(s, p, o) for s, p, o in triples if str(p) == self.FM_INCOMPLETE]
        assert len(incomplete) == 1
        assert str(incomplete[0][2]) == self.HAS_PROVIDER

    def test_null_datatype_property_no_quality_triple(self):
        """Null on a plain DatatypeProperty (no ObjectProperty declaration) must NOT
        emit fm:incompleteReference — Strategy A must not fire for data properties."""
        encounter_def = GoldTableDefinition(
            gold_table_name="gold_encounter",
            primary_class_uri=self.ENCOUNTER_CLASS,
            class_label="encounter",
            source_silver_tables=["silver_raw_encounters"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_encounters",
                    source_column="description",
                    gold_column_name="description",
                    source_data_type="TEXT",
                    property_uri="http://ex.org/ontology#hasDescription",
                ),
            ],
        )
        # No OWL ObjectProperty declaration for hasDescription
        builder = TripleBuilder([encounter_def], UriFactory(BASE))
        row = {"_ontology_class_uri": self.ENCOUNTER_CLASS, "id": "E1", "description": None}
        triples = builder.build(row, encounter_def)
        incomplete = [(s, p, o) for s, p, o in triples if str(p) == self.FM_INCOMPLETE]
        assert len(incomplete) == 0

    def test_multiple_null_fks_each_emit_quality_triple(self):
        """Each null ObjectProperty column produces its own fm:incompleteReference triple."""
        provider_def, _, g = self._make_setup()
        ORGANIZATION_CLASS = "http://ex.org/ontology#Organization"
        HAS_ORG = "http://ex.org/ontology#hasOrganization"
        g.add((URIRef(HAS_ORG), RDF.type, OWL.ObjectProperty))
        g.add((URIRef(HAS_ORG), RDFS.range, URIRef(ORGANIZATION_CLASS)))
        org_def = GoldTableDefinition(
            gold_table_name="gold_organization",
            primary_class_uri=ORGANIZATION_CLASS,
            class_label="organization",
            source_silver_tables=["silver_raw_organizations"],
            column_specs=[],
            id_column="id",
        )
        encounter_def = GoldTableDefinition(
            gold_table_name="gold_encounter",
            primary_class_uri=self.ENCOUNTER_CLASS,
            class_label="encounter",
            source_silver_tables=["silver_raw_encounters"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_encounters",
                    source_column="provider",
                    gold_column_name="provider",
                    source_data_type="VARCHAR(50)",
                    property_uri=self.HAS_PROVIDER,
                ),
                GoldColumnSpec(
                    silver_table="silver_raw_encounters",
                    source_column="organization",
                    gold_column_name="organization",
                    source_data_type="VARCHAR(50)",
                    property_uri=HAS_ORG,
                ),
            ],
        )
        builder = TripleBuilder(
            [provider_def, org_def, encounter_def], UriFactory(BASE), g
        )
        row = {
            "_ontology_class_uri": self.ENCOUNTER_CLASS,
            "id": "E1",
            "provider": None,
            "organization": None,
        }
        triples = builder.build(row, encounter_def)
        incomplete = [(s, p, o) for s, p, o in triples if str(p) == self.FM_INCOMPLETE]
        assert len(incomplete) == 2
        incomplete_values = {str(o) for _, _, o in incomplete}
        assert self.HAS_PROVIDER in incomplete_values
        assert HAS_ORG in incomplete_values


# ---------------------------------------------------------------------------
# FK-to-missing-class fallback — class URI in value_map / column → incompleteReference
# ---------------------------------------------------------------------------

class TestFkToMissingClass:
    """FK-to-missing-class fallback: when a value_map entry or a pre-materialised
    column value resolves to an OWL CLASS URI (because the target entity has no gold
    table), the TripleBuilder must emit fm:incompleteReference instead of emitting
    the class URI as an object value.

    This is DISTINCT from BUG-02 (fm:invalidMapping):
    - BUG-02: property IS an ObjectProperty whose range IS the class URI
      → BUG-02 guard fires first → fm:invalidMapping.
    - FK-to-missing-class: property range is unrelated (e.g. DatatypeProperty) OR
      the class URI is in the ontology but is not the property's declared range
      → new guard fires → fm:incompleteReference.

    Real-world trigger: Synthea 'payer' column mapped to hasPayerCoverage
    (a DatatypeProperty); the LLM value_map mapped all payer UUIDs to
    OrgHealth.owl#Payer (the class URI, not individual URIs) because no gold_payer
    table was ever built.
    """

    PAYER_CLASS       = "http://ex.org/ontology#Payer"
    ENCOUNTER_CLASS   = "http://ex.org/ontology#Encounter"
    HAS_PAYER_COV     = "http://ex.org/ontology#hasPayerCoverage"  # DatatypeProperty
    HAS_PAYER_OBJ     = "http://ex.org/ontology#hasEncounterPayer"  # ObjectProperty
    FM_INCOMPLETE     = f"{_FM_NS}incompleteReference"
    FM_INVALID        = f"{_FM_NS}invalidMapping"

    def _make_def(self, prop_uri: str, value_map=None, id_column=None):
        return GoldTableDefinition(
            gold_table_name="gold_encounter",
            primary_class_uri=self.ENCOUNTER_CLASS,
            class_label="encounter",
            source_silver_tables=["silver_raw_encounters"],
            id_column=id_column,
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_encounters",
                    source_column="payer",
                    gold_column_name="payer",
                    source_data_type="VARCHAR(200)",
                    property_uri=prop_uri,
                    value_map=value_map or {},
                ),
            ],
        )

    def _ontology_datatype_prop(self):
        """Payer is an OWL Class; hasPayerCoverage is a DatatypeProperty."""
        g = Graph()
        g.add((URIRef(self.HAS_PAYER_COV), RDF.type, OWL.DatatypeProperty))
        g.add((URIRef(self.PAYER_CLASS), RDF.type, OWL.Class))
        return g

    def test_value_map_to_class_uri_emits_incomplete_reference(self):
        """value_map maps UUID → Payer class URI (not an individual).
        Must emit fm:incompleteReference, not the class URI as object."""
        def_ = self._make_def(
            self.HAS_PAYER_COV,
            value_map={"some-payer-uuid": self.PAYER_CLASS},
        )
        builder = TripleBuilder([def_], UriFactory(BASE), self._ontology_datatype_prop())
        row = {"_ontology_class_uri": self.ENCOUNTER_CLASS, "id": "E1", "payer": "some-payer-uuid"}
        triples = builder.build(row, def_)

        # No normal property triple
        payer_triples = [(s, p, o) for s, p, o in triples if p == URIRef(self.HAS_PAYER_COV)]
        assert len(payer_triples) == 0, "Class URI must not appear as property object"
        # Must emit fm:incompleteReference
        incomplete = [(s, p, o) for s, p, o in triples if str(p) == self.FM_INCOMPLETE]
        assert len(incomplete) == 1
        assert str(incomplete[0][2]) == self.HAS_PAYER_COV
        # Must NOT emit fm:invalidMapping (that's BUG-02 only)
        assert not any(str(p) == self.FM_INVALID for _, p, _ in triples)

    def test_prematerialised_class_uri_emits_incomplete_reference(self):
        """Gold column stores the Payer class URI directly (no value_map lookup needed).
        Must emit fm:incompleteReference."""
        def_ = self._make_def(self.HAS_PAYER_COV)  # no value_map
        builder = TripleBuilder([def_], UriFactory(BASE), self._ontology_datatype_prop())
        row = {"_ontology_class_uri": self.ENCOUNTER_CLASS, "id": "E1", "payer": self.PAYER_CLASS}
        triples = builder.build(row, def_)

        payer_triples = [(s, p, o) for s, p, o in triples if p == URIRef(self.HAS_PAYER_COV)]
        assert len(payer_triples) == 0
        incomplete = [(s, p, o) for s, p, o in triples if str(p) == self.FM_INCOMPLETE]
        assert len(incomplete) == 1
        assert str(incomplete[0][2]) == self.HAS_PAYER_COV

    def test_object_property_range_class_no_gold_table_emits_incomplete(self):
        """ObjectProperty range == mapped_uri but range class has NO gold table.
        This is an FK-to-missing-entity (e.g. Synthea payer with no gold_payer) →
        fm:incompleteReference, NOT fm:invalidMapping."""
        g = Graph()
        g.add((URIRef(self.HAS_PAYER_OBJ), RDF.type, OWL.ObjectProperty))
        g.add((URIRef(self.HAS_PAYER_OBJ), RDFS.range, URIRef(self.PAYER_CLASS)))
        g.add((URIRef(self.PAYER_CLASS), RDF.type, OWL.Class))
        def_ = self._make_def(
            self.HAS_PAYER_OBJ,
            value_map={"payer-uuid": self.PAYER_CLASS},
        )
        # Only gold_encounter in definitions; no gold_payer → Payer has no gold table
        builder = TripleBuilder([def_], UriFactory(BASE), g)
        row = {"_ontology_class_uri": self.ENCOUNTER_CLASS, "id": "E1", "payer": "payer-uuid"}
        triples = builder.build(row, def_)

        assert any(str(p) == self.FM_INCOMPLETE for _, p, _ in triples), \
            "must emit incompleteReference when range class has no gold table"
        assert not any(str(p) == self.FM_INVALID for _, p, _ in triples), \
            "must NOT emit invalidMapping — this is a missing entity, not a bad mapping"

    def test_object_property_range_class_with_gold_table_takes_path1(self):
        """When range class HAS a gold table, path-1 entity_map match fires before the
        BUG-02 guard.  Result is a normal object-property triple, not a quality triple."""
        g = Graph()
        g.add((URIRef(self.HAS_PAYER_OBJ), RDF.type, OWL.ObjectProperty))
        g.add((URIRef(self.HAS_PAYER_OBJ), RDFS.range, URIRef(self.PAYER_CLASS)))
        g.add((URIRef(self.PAYER_CLASS), RDF.type, OWL.Class))
        enc_def = self._make_def(self.HAS_PAYER_OBJ, value_map={"payer-uuid": self.PAYER_CLASS})
        payer_def = GoldTableDefinition(
            gold_table_name="gold_payer",
            primary_class_uri=self.PAYER_CLASS,
            class_label="payer",
            source_silver_tables=["silver_raw_payers"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_payers",
                    gold_column_name="id", source_column="id",
                    source_data_type="TEXT", property_uri="", is_primary_key=True,
                )
            ],
        )
        builder = TripleBuilder([enc_def, payer_def], UriFactory(BASE), g)
        row = {"_ontology_class_uri": self.ENCOUNTER_CLASS, "id": "E1", "payer": "payer-uuid"}
        triples = builder.build(row, enc_def)
        # Path 1 fires → object URI minted; no quality triple
        payer_triples = [(s, p, o) for s, p, o in triples if p == URIRef(self.HAS_PAYER_OBJ)]
        assert len(payer_triples) == 1
        assert isinstance(payer_triples[0][2], URIRef)
        assert not any(str(p) in (self.FM_INVALID, self.FM_INCOMPLETE) for _, p, _ in triples)

    def test_individual_uri_in_value_map_passes_through(self):
        """A value_map entry mapping to an INDIVIDUAL URI (not an OWL class) passes
        through as a normal triple — the class URI guard must not interfere."""
        INDIVIDUAL_URI = "http://ex.org/ontology#BlueCross"  # an individual, NOT OWL.Class
        def_ = self._make_def(self.HAS_PAYER_COV, value_map={"uuid-bc": INDIVIDUAL_URI})
        g = self._ontology_datatype_prop()
        # BlueCross is NOT declared as OWL.Class in the graph
        builder = TripleBuilder([def_], UriFactory(BASE), g)
        row = {"_ontology_class_uri": self.ENCOUNTER_CLASS, "id": "E1", "payer": "uuid-bc"}
        triples = builder.build(row, def_)

        payer_triples = [(s, p, o) for s, p, o in triples if p == URIRef(self.HAS_PAYER_COV)]
        assert len(payer_triples) == 1
        assert str(payer_triples[0][2]) == INDIVIDUAL_URI
        assert not any(str(p) in (self.FM_INCOMPLETE, self.FM_INVALID) for _, p, _ in triples)

    def test_no_ontology_no_blocking(self):
        """Without an ontology _class_uris is empty — no class URI guard fires.
        Column value passes through as a URIRef."""
        def_ = self._make_def(self.HAS_PAYER_COV, value_map={"uuid": self.PAYER_CLASS})
        builder = TripleBuilder([def_], UriFactory(BASE))  # no ontology_graph
        row = {"_ontology_class_uri": self.ENCOUNTER_CLASS, "id": "E1", "payer": "uuid"}
        triples = builder.build(row, def_)

        # Without ontology, _class_uris is empty → URIRef emitted
        payer_triples = [(s, p, o) for s, p, o in triples if p == URIRef(self.HAS_PAYER_COV)]
        assert len(payer_triples) == 1
        assert isinstance(payer_triples[0][2], URIRef)
        assert str(payer_triples[0][2]) == self.PAYER_CLASS

    def test_cross_namespace_class_uri_detected_via_fragment_match(self):
        """value_map maps payer UUID → a class URI from a DIFFERENT namespace than the rigor OWL.

        Rigor OWL declares synthea#Payer as OWL.Class.
        value_map contains OrgHealth.owl#Payer (different namespace, same fragment 'Payer').
        TripleBuilder must detect this as a class URI via fragment matching and emit
        fm:incompleteReference rather than emitting OrgHealth.owl#Payer as an object value.

        This mirrors the Synthea case: the OrgHealth base ontology and the rigor OWL both
        define a 'Payer' class but under different namespace prefixes.
        """
        RIGOR_PAYER    = "http://mesh.local/ontology/synthea#Payer"    # in rigor OWL
        ORG_PAYER      = "http://localhost:8080/owl/OrgHealth.owl#Payer"  # in value_map

        g = Graph()
        g.add((URIRef(URIRef(self.HAS_PAYER_COV)), RDF.type, OWL.DatatypeProperty))
        g.add((URIRef(RIGOR_PAYER), RDF.type, OWL.Class))   # rigor NS class
        # OrgHealth.owl#Payer is NOT declared in this ontology graph — only rigor NS class is.

        def_ = self._make_def(
            self.HAS_PAYER_COV,
            value_map={"payer-uuid-123": ORG_PAYER},   # value_map uses base NS
        )
        builder = TripleBuilder([def_], UriFactory(BASE), g)
        row = {"_ontology_class_uri": self.ENCOUNTER_CLASS, "id": "E1", "payer": "payer-uuid-123"}
        triples = builder.build(row, def_)

        # OrgHealth.owl#Payer must NOT appear as an object value
        payer_obj_triples = [
            (s, p, o) for s, p, o in triples
            if p == URIRef(self.HAS_PAYER_COV)
        ]
        assert len(payer_obj_triples) == 0, (
            "Cross-namespace class URI must not be emitted as object value"
        )
        # fm:incompleteReference must be emitted instead
        incomplete = [(s, p, o) for s, p, o in triples if str(p) == self.FM_INCOMPLETE]
        assert len(incomplete) == 1, (
            "fm:incompleteReference must be emitted for cross-namespace class URI"
        )
        assert str(incomplete[0][2]) == self.HAS_PAYER_COV
