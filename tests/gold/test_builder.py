# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, URIRef

from semantic_mapper.gold.builder import GoldDefinitionBuilder
from semantic_mapper.schema.mapping_output import (
    ClassCandidate,
    MappingStatus,
    PropertyMapping,
    SemanticMappingReport,
    TableMapping,
)


def _make_report(*table_mappings):
    return SemanticMappingReport(
        ontology_uri="http://ex.org/ontology",
        database_name="bronze_db",
        settings_used={},
        tables=list(table_mappings),
        total_pending_review=0,
    )


def _make_table(source_table, class_uri, class_label, *column_mappings):
    return TableMapping(
        source_table=source_table,
        class_candidate_set=[
            ClassCandidate(
                uri=class_uri,
                label=class_label,
                rationale="test",
                confidence_score=0.99,
            )
        ],
        column_mappings=list(column_mappings),
    )


def _confirmed(col, dtype="VARCHAR(50)", uri=None):
    return PropertyMapping(
        source_column=col,
        source_data_type=dtype,
        is_nullable=False,
        mapped_property_uri=uri or f"http://ex.org#{col}",
        status=MappingStatus.CONFIRMED,
        confidence_score=0.95,
        rationale="test",
    )


def _pending(col):
    return PropertyMapping(
        source_column=col,
        source_data_type="VARCHAR(50)",
        is_nullable=True,
        status=MappingStatus.PENDING_REVIEW,
        confidence_score=0.50,
        rationale="test",
    )


class TestBuildSingleTable:
    def test_build_single_table(self):
        report = _make_report(
            _make_table(
                "raw_party", "http://ex.org#Party", "Party",
                _confirmed("party_id"),
            )
        )
        defs = GoldDefinitionBuilder().build(report)
        assert len(defs) == 1
        d = defs[0]
        assert d.gold_table_name == "gold_party"
        assert d.primary_class_uri == "http://ex.org#Party"
        assert d.class_label == "party"
        assert d.source_silver_tables == ["silver_raw_party"]
        assert len(d.column_specs) == 1
        # gold_column_name is the URI fragment (camelCase property local name)
        assert d.column_specs[0].gold_column_name == "party_id"
        assert d.column_specs[0].source_column == "party_id"


class TestBuildMultipleClasses:
    def test_build_multiple_classes(self):
        report = _make_report(
            _make_table("raw_party", "http://ex.org#Party", "Party", _confirmed("party_id")),
            _make_table("raw_account", "http://ex.org#Account", "Account", _confirmed("account_id")),
        )
        defs = GoldDefinitionBuilder().build(report)
        assert len(defs) == 2
        names = {d.gold_table_name for d in defs}
        assert "gold_party" in names
        assert "gold_account" in names


class TestBuildSameClassMergesTables:
    def test_same_class_merges_tables(self):
        report = _make_report(
            _make_table("raw_party", "http://ex.org#Party", "Party", _confirmed("party_id")),
            _make_table("raw_party_ext", "http://ex.org#Party", "Party", _confirmed("email")),
        )
        defs = GoldDefinitionBuilder().build(report)
        assert len(defs) == 1
        d = defs[0]
        assert "silver_raw_party" in d.source_silver_tables
        assert "silver_raw_party_ext" in d.source_silver_tables
        # gold_column_name = URI fragment; _confirmed generates uri=f"http://ex.org#{col}"
        col_names = {spec.gold_column_name for spec in d.column_specs}
        assert "party_id" in col_names
        assert "email" in col_names


class TestBuildSkipsPendingColumns:
    def test_pending_columns_excluded(self):
        report = _make_report(
            _make_table(
                "raw_party", "http://ex.org#Party", "Party",
                _confirmed("party_id"),
                _pending("unresolved_col"),
            )
        )
        defs = GoldDefinitionBuilder().build(report)
        col_names = {spec.gold_column_name for spec in defs[0].column_specs}
        assert "party_id" in col_names
        # pending column has no mapped_property_uri so it's excluded entirely
        assert "unresolved_col" not in col_names


class TestBuildHighestConfidenceClassWins:
    def test_highest_confidence_class_selected(self):
        """Index-0 has low confidence; a later candidate should win."""
        table = TableMapping(
            source_table="raw_agreement",
            class_candidate_set=[
                ClassCandidate(
                    uri="http://ex.org#Resident",
                    label="Resident",
                    rationale="low confidence",
                    confidence_score=0.59,
                ),
                ClassCandidate(
                    uri="http://ex.org#Agreement",
                    label="Agreement",
                    rationale="high confidence",
                    confidence_score=1.00,
                ),
            ],
            column_mappings=[_confirmed("agr_id")],
        )
        report = _make_report(table)
        defs = GoldDefinitionBuilder().build(report)
        assert len(defs) == 1
        assert defs[0].primary_class_uri == "http://ex.org#Agreement"
        assert defs[0].gold_table_name == "gold_agreement"


class TestBuildSkipsNoClass:
    def test_empty_class_candidate_set_skipped(self):
        no_class_table = TableMapping(
            source_table="raw_orphan",
            class_candidate_set=[],
            column_mappings=[_confirmed("orphan_id")],
        )
        report = _make_report(no_class_table)
        defs = GoldDefinitionBuilder().build(report)
        assert defs == []


class TestBuildDeduplicatesColumns:
    def test_different_sources_same_uri_both_kept(self):
        """Two columns sharing the same property URI: both are retained in gold.

        Gold must faithfully carry every confirmed mapping regardless of URI collisions.
        The TripleBuilder will emit duplicate predicates; the mapping engine should
        assign distinct URIs to avoid that downstream.
        """
        report = _make_report(
            _make_table(
                "raw_party", "http://ex.org#Party", "Party",
                _confirmed("name", "VARCHAR(100)", "http://ex.org#hasName"),
                _confirmed("full_name", "TEXT", "http://ex.org#hasName"),
            ),
        )
        defs = GoldDefinitionBuilder().build(report)
        assert len(defs) == 1
        col_names = {s.gold_column_name for s in defs[0].column_specs}
        assert "name" in col_names, "First column (name) must be retained"
        assert "full_name" in col_names, "Second column with duplicate URI must also be retained"


# ---------------------------------------------------------------------------
# Helpers for FK resolution tests
# ---------------------------------------------------------------------------

def _ontology_with_object_prop(prop_uri: str, domain_uri: str, range_uri: str) -> Graph:
    g = Graph()
    g.add((URIRef(prop_uri), RDF.type, OWL.ObjectProperty))
    g.add((URIRef(prop_uri), RDFS.domain, URIRef(domain_uri)))
    g.add((URIRef(prop_uri), RDFS.range, URIRef(range_uri)))
    return g


PATIENT_URI = "http://ex.org#Patient"
ENCOUNTER_URI = "http://ex.org#Encounter"
PROCEDURE_URI = "http://ex.org#Procedure"
HAS_ENCOUNTER_PROP = "http://ex.org#patientHasEncounter"
HAS_PROCEDURE_PROP = "http://ex.org#patientHasProcedure"
HAS_DOB_PROP = "http://ex.org#hasDateOfBirth"


class TestFKPropertyResolution:
    def test_fk_column_gets_object_property_uri(self):
        """patient source column in gold_encounter → resolved to patientHasEncounter."""
        report = _make_report(
            _make_table(
                "encounters", ENCOUNTER_URI, "Encounter",
                _confirmed("patient", uri="http://ex.org#hasFirstName"),  # wrong data prop
                _confirmed("date", uri="http://ex.org#hasDate"),
            ),
            _make_table(
                "patients", PATIENT_URI, "Patient",
                _confirmed("id", uri="http://ex.org#hasIdentifier"),
            ),
        )
        # domain=Patient, range=Encounter → preferred (flip) direction
        g = _ontology_with_object_prop(HAS_ENCOUNTER_PROP, PATIENT_URI, ENCOUNTER_URI)
        defs = GoldDefinitionBuilder().build(report, ontology_graph=g)

        enc_def = next(d for d in defs if d.gold_table_name == "gold_encounter")
        # FK detected via source_column; gold_column_name is the URI fragment
        patient_spec = next(s for s in enc_def.column_specs if s.source_column == "patient")
        assert patient_spec.property_uri == HAS_ENCOUNTER_PROP

    def test_non_fk_column_unchanged(self):
        """date_of_birth source column is not an entity name; original URI must be preserved."""
        report = _make_report(
            _make_table(
                "patients", PATIENT_URI, "Patient",
                _confirmed("date_of_birth", uri=HAS_DOB_PROP),
            ),
        )
        g = Graph()  # empty — no object properties
        defs = GoldDefinitionBuilder().build(report, ontology_graph=g)

        spec = next(s for s in defs[0].column_specs if s.source_column == "date_of_birth")
        assert spec.property_uri == HAS_DOB_PROP

    def test_no_ontology_graph_unchanged(self):
        """Without ontology_graph argument, original property URI is preserved."""
        report = _make_report(
            _make_table(
                "encounters", ENCOUNTER_URI, "Encounter",
                _confirmed("patient", uri="http://ex.org#hasFirstName"),
            ),
            _make_table(
                "patients", PATIENT_URI, "Patient",
                _confirmed("id", uri="http://ex.org#hasIdentifier"),
            ),
        )
        defs = GoldDefinitionBuilder().build(report)  # no ontology_graph

        enc_def = next(d for d in defs if d.gold_table_name == "gold_encounter")
        patient_spec = next(s for s in enc_def.column_specs if s.source_column == "patient")
        assert patient_spec.property_uri == "http://ex.org#hasFirstName"

    def test_preferred_direction_domain_ref_range_current(self):
        """Preferred: domain=ref_class (Patient), range=current_class (Encounter)."""
        g = _ontology_with_object_prop(HAS_ENCOUNTER_PROP, PATIENT_URI, ENCOUNTER_URI)
        report = _make_report(
            _make_table("encounters", ENCOUNTER_URI, "Encounter",
                        _confirmed("patient", uri="http://ex.org#wrong")),
            _make_table("patients", PATIENT_URI, "Patient",
                        _confirmed("id", uri="http://ex.org#hasId")),
        )
        defs = GoldDefinitionBuilder().build(report, ontology_graph=g)
        enc_def = next(d for d in defs if d.gold_table_name == "gold_encounter")
        patient_spec = next(s for s in enc_def.column_specs if s.source_column == "patient")
        assert patient_spec.property_uri == HAS_ENCOUNTER_PROP

    def test_fallback_direction_domain_current_range_ref(self):
        """Fallback: domain=current_class (Encounter), range=ref_class (Patient)."""
        normal_prop = "http://ex.org#encounterHasPatient"
        # Only the normal direction (Encounter → Patient) exists, no flip variant
        g = _ontology_with_object_prop(normal_prop, ENCOUNTER_URI, PATIENT_URI)
        report = _make_report(
            _make_table("encounters", ENCOUNTER_URI, "Encounter",
                        _confirmed("patient", uri="http://ex.org#wrong")),
            _make_table("patients", PATIENT_URI, "Patient",
                        _confirmed("id", uri="http://ex.org#hasId")),
        )
        defs = GoldDefinitionBuilder().build(report, ontology_graph=g)
        enc_def = next(d for d in defs if d.gold_table_name == "gold_encounter")
        patient_spec = next(s for s in enc_def.column_specs if s.source_column == "patient")
        assert patient_spec.property_uri == normal_prop

    def test_no_matching_property_original_uri_preserved(self):
        """No object property connects Patient ↔ Encounter → original URI kept."""
        # Ontology has an unrelated property only
        g = _ontology_with_object_prop(
            "http://ex.org#unrelated", "http://ex.org#Foo", "http://ex.org#Bar"
        )
        report = _make_report(
            _make_table("encounters", ENCOUNTER_URI, "Encounter",
                        _confirmed("patient", uri="http://ex.org#hasFirstName")),
            _make_table("patients", PATIENT_URI, "Patient",
                        _confirmed("id", uri="http://ex.org#hasId")),
        )
        defs = GoldDefinitionBuilder().build(report, ontology_graph=g)
        enc_def = next(d for d in defs if d.gold_table_name == "gold_encounter")
        patient_spec = next(s for s in enc_def.column_specs if s.source_column == "patient")
        assert patient_spec.property_uri == "http://ex.org#hasFirstName"

    def test_multiple_fk_tables_all_resolved(self):
        """patient FK in both gold_encounter and gold_procedure resolved independently."""
        g = Graph()
        # patientHasEncounter: domain=Patient, range=Encounter
        g.add((URIRef(HAS_ENCOUNTER_PROP), RDF.type, OWL.ObjectProperty))
        g.add((URIRef(HAS_ENCOUNTER_PROP), RDFS.domain, URIRef(PATIENT_URI)))
        g.add((URIRef(HAS_ENCOUNTER_PROP), RDFS.range, URIRef(ENCOUNTER_URI)))
        # patientHasProcedure: domain=Patient, range=Procedure
        g.add((URIRef(HAS_PROCEDURE_PROP), RDF.type, OWL.ObjectProperty))
        g.add((URIRef(HAS_PROCEDURE_PROP), RDFS.domain, URIRef(PATIENT_URI)))
        g.add((URIRef(HAS_PROCEDURE_PROP), RDFS.range, URIRef(PROCEDURE_URI)))

        report = _make_report(
            _make_table("encounters", ENCOUNTER_URI, "Encounter",
                        _confirmed("patient", uri="http://ex.org#wrong1")),
            _make_table("procedures", PROCEDURE_URI, "Procedure",
                        _confirmed("patient", uri="http://ex.org#wrong2")),
            _make_table("patients", PATIENT_URI, "Patient",
                        _confirmed("id", uri="http://ex.org#hasId")),
        )
        defs = GoldDefinitionBuilder().build(report, ontology_graph=g)

        enc_def = next(d for d in defs if d.gold_table_name == "gold_encounter")
        enc_patient = next(s for s in enc_def.column_specs if s.source_column == "patient")
        assert enc_patient.property_uri == HAS_ENCOUNTER_PROP

        proc_def = next(d for d in defs if d.gold_table_name == "gold_procedure")
        proc_patient = next(s for s in proc_def.column_specs if s.source_column == "patient")
        assert proc_patient.property_uri == HAS_PROCEDURE_PROP


class TestFKAnnotationResolution:
    """Strategy 2: fm:sourceForeignKey annotations resolve FK columns whose name
    doesn't match the target entity (e.g. subject_id → hasAdmissionPatient)."""

    def _add_fk_annotation(self, g: Graph, prop_uri: str, annotation_value: str) -> None:
        from rdflib import Literal as _Lit, URIRef as _URIRef
        FM_FK = _URIRef("http://forgemesh.ai/ontology#sourceForeignKey")
        g.add((_URIRef(prop_uri), FM_FK, _Lit(annotation_value)))

    def test_non_entity_named_column_resolved_via_annotation(self):
        """subject_id is not an entity name but fm:sourceForeignKey maps it to hasAdmissionPatient."""
        HAS_PATIENT_PROP = "http://ex.org#hasAdmissionPatient"
        g = _ontology_with_object_prop(HAS_PATIENT_PROP, ENCOUNTER_URI, PATIENT_URI)
        # raw_encounters.subject_id -> raw_patients
        self._add_fk_annotation(g, HAS_PATIENT_PROP, "raw_encounters.subject_id -> raw_patients")

        report = _make_report(
            _make_table(
                "raw_encounters", ENCOUNTER_URI, "Encounter",
                _confirmed("subject_id", uri="http://ex.org#hasSubjectId"),  # wrong data prop
                _confirmed("date", uri="http://ex.org#hasDate"),
            ),
            _make_table(
                "raw_patients", PATIENT_URI, "Patient",
                _confirmed("id", uri="http://ex.org#hasId"),
            ),
        )
        defs = GoldDefinitionBuilder().build(report, ontology_graph=g)

        enc_def = next(d for d in defs if d.gold_table_name == "gold_encounter")
        subj_spec = next(s for s in enc_def.column_specs if s.source_column == "subject_id")
        assert subj_spec.property_uri == HAS_PATIENT_PROP

    def test_annotation_does_not_affect_non_matching_column(self):
        """date column has no annotation entry; its original property URI is preserved."""
        HAS_PATIENT_PROP = "http://ex.org#hasAdmissionPatient"
        g = _ontology_with_object_prop(HAS_PATIENT_PROP, ENCOUNTER_URI, PATIENT_URI)
        self._add_fk_annotation(g, HAS_PATIENT_PROP, "raw_encounters.subject_id -> raw_patients")

        report = _make_report(
            _make_table(
                "raw_encounters", ENCOUNTER_URI, "Encounter",
                _confirmed("subject_id", uri="http://ex.org#hasSubjectId"),
                _confirmed("date", uri="http://ex.org#hasDate"),
            ),
            _make_table(
                "raw_patients", PATIENT_URI, "Patient",
                _confirmed("id", uri="http://ex.org#hasId"),
            ),
        )
        defs = GoldDefinitionBuilder().build(report, ontology_graph=g)

        enc_def = next(d for d in defs if d.gold_table_name == "gold_encounter")
        date_spec = next(s for s in enc_def.column_specs if s.source_column == "date")
        assert date_spec.property_uri == "http://ex.org#hasDate"

    def test_strategy1_fallthrough_to_strategy2_on_namespace_mismatch(self):
        """Strategy 1 matches entity name but _find_object_property returns None
        (namespace mismatch between ecmap and rigor OWL).  Strategy 2 must then
        fire via the fm:sourceForeignKey annotation.

        This mirrors the Synthea 'patient' column: ecmap has OrgHealth.owl#Patient
        (base namespace) while the rigor OWL defines properties in synthea# namespace,
        so Strategy 1 finds the class but cannot find the property.  Strategy 2 must
        pick up hasEncounterPatient via the fm:sourceForeignKey annotation.
        """
        # Use two distinct class namespaces to simulate the mismatch:
        # - ENCOUNTER_URI / PATIENT_URI are in 'ex.org' (ecmap / base ontology NS)
        # - The OWL property uses synthea# NS (rigor OWL NS)
        RIGOR_ENCOUNTER = "http://mesh.local/ontology/synthea#Encounter"
        RIGOR_PATIENT    = "http://mesh.local/ontology/synthea#Patient"
        HAS_ENC_PATIENT  = "http://mesh.local/ontology/synthea#hasEncounterPatient"

        from rdflib import Literal as _Lit, URIRef as _URIRef
        FM_FK = _URIRef("http://forgemesh.ai/ontology#sourceForeignKey")

        g = Graph()
        # OWL property uses rigor namespace — domain/range in rigor NS
        g.add((_URIRef(HAS_ENC_PATIENT), RDF.type, OWL.ObjectProperty))
        g.add((_URIRef(HAS_ENC_PATIENT), RDFS.domain, _URIRef(RIGOR_ENCOUNTER)))
        g.add((_URIRef(HAS_ENC_PATIENT), RDFS.range,  _URIRef(RIGOR_PATIENT)))
        # fm:sourceForeignKey annotation
        g.add((_URIRef(HAS_ENC_PATIENT), FM_FK, _Lit("raw_encounters.patient -> raw_patients")))

        # Mapping report uses base namespace for class URIs (like OrgHealth.owl would)
        report = _make_report(
            _make_table(
                "raw_encounters", ENCOUNTER_URI, "Encounter",   # ENCOUNTER_URI = ex.org# NS
                _confirmed("patient", uri="http://ex.org#hasIdentifier"),  # wrong generic prop
            ),
            _make_table(
                "raw_patients", PATIENT_URI, "Patient",         # PATIENT_URI = ex.org# NS
                _confirmed("id", uri="http://ex.org#hasId"),
            ),
        )
        defs = GoldDefinitionBuilder().build(report, ontology_graph=g)

        enc_def = next(d for d in defs if d.gold_table_name == "gold_encounter")
        patient_spec = next(s for s in enc_def.column_specs if s.source_column == "patient")
        # Strategy 1: ecmap["patient"] = PATIENT_URI (ex.org# NS)
        #   → _find_object_property(PATIENT_URI, ENCOUNTER_URI, rigor_owl) → None
        #     (rigor OWL has RIGOR_PATIENT / RIGOR_ENCOUNTER — different NS)
        # Strategy 1 fallthrough → Strategy 2: fk_map[("raw_encounters", "patient")] = HAS_ENC_PATIENT
        assert patient_spec.property_uri == HAS_ENC_PATIENT, (
            f"Expected {HAS_ENC_PATIENT!r}, got {patient_spec.property_uri!r}"
        )

    def test_strategy1_takes_precedence_over_annotation(self):
        """If source_column matches an entity name (Strategy 1) it wins over annotation."""
        HAS_ENCOUNTER_VIA_NAME = "http://ex.org#patientHasEncounter"
        HAS_ENCOUNTER_VIA_FK = "http://ex.org#patientHasEncounterViaAnnotation"

        # Strategy 1 property: domain=Patient, range=Encounter
        g = _ontology_with_object_prop(HAS_ENCOUNTER_VIA_NAME, PATIENT_URI, ENCOUNTER_URI)
        # Strategy 2 property annotated on a different prop URI
        g.add((URIRef(HAS_ENCOUNTER_VIA_FK), RDF.type, OWL.ObjectProperty))
        g.add((URIRef(HAS_ENCOUNTER_VIA_FK), RDFS.domain, URIRef(PATIENT_URI)))
        g.add((URIRef(HAS_ENCOUNTER_VIA_FK), RDFS.range, URIRef(ENCOUNTER_URI)))
        from rdflib import Literal as _Lit, URIRef as _URIRef
        FM_FK = _URIRef("http://forgemesh.ai/ontology#sourceForeignKey")
        g.add((_URIRef(HAS_ENCOUNTER_VIA_FK), FM_FK, _Lit("raw_encounters.patient -> raw_patients")))

        # Column name "patient" matches entity name → Strategy 1 fires first.
        report = _make_report(
            _make_table("raw_encounters", ENCOUNTER_URI, "Encounter",
                        _confirmed("patient", uri="http://ex.org#wrong")),
            _make_table("raw_patients", PATIENT_URI, "Patient",
                        _confirmed("id", uri="http://ex.org#hasId")),
        )
        defs = GoldDefinitionBuilder().build(report, ontology_graph=g)

        enc_def = next(d for d in defs if d.gold_table_name == "gold_encounter")
        patient_spec = next(s for s in enc_def.column_specs if s.source_column == "patient")
        # Strategy 1 finds patientHasEncounter via entity name match — not the annotation one.
        assert patient_spec.property_uri == HAS_ENCOUNTER_VIA_NAME


# ---------------------------------------------------------------------------
# Strategy 3: fm:sourceColumn override (fixes multi-domain bleed)
# ---------------------------------------------------------------------------

class TestSourceColumnMapResolution:
    """Strategy 3: fm:sourceColumn exact-match overrides a shared (bleed) property URI."""

    FM_SC = URIRef("http://forgemesh.ai/ontology#sourceColumn")
    FM_FK = URIRef("http://forgemesh.ai/ontology#sourceForeignKey")

    def _add_source_column(self, g: Graph, prop_uri: str, sc_value: str) -> None:
        from rdflib import Literal as _Lit
        g.add((URIRef(prop_uri), self.FM_SC, _Lit(sc_value)))

    def test_bleed_property_overridden_by_scoped_sibling(self):
        """When the mapping report maps diagnoses.seq_num to a shared property but
        the OWL has a scoped hasDiagnosesSeqNum with fm:sourceColumn, Strategy 3 fires."""
        SHARED_PROP  = "http://ex.org#hasProcedureSeqNum"  # bleed: domain = Proc + Diag
        SCOPED_PROP  = "http://ex.org#hasDiagnosesSeqNum"  # fixed: domain = Diag only

        g = Graph()
        # Shared property (still exists in OWL, fm:sourceColumn points to procedures)
        g.add((URIRef(SHARED_PROP), RDF.type, OWL.DatatypeProperty))
        self._add_source_column(g, SHARED_PROP, "raw_procedures.seq_num")

        # New scoped property for diagnoses
        g.add((URIRef(SCOPED_PROP), RDF.type, OWL.DatatypeProperty))
        self._add_source_column(g, SCOPED_PROP, "raw_diagnoses.seq_num")

        report = _make_report(
            _make_table(
                "raw_diagnoses", "http://ex.org#Diagnosis", "Diagnosis",
                # Mapping report recorded the shared (bleed) URI
                _confirmed("seq_num", uri=SHARED_PROP),
            ),
        )
        defs = GoldDefinitionBuilder().build(report, ontology_graph=g)

        diag_def = next(d for d in defs if d.gold_table_name == "gold_diagnosis")
        seq_spec = next(s for s in diag_def.column_specs if s.source_column == "seq_num")
        assert seq_spec.property_uri == SCOPED_PROP

    def test_correct_property_unchanged_when_sc_matches(self):
        """If the mapping report already points to the correct scoped property, no change."""
        SCOPED_PROP = "http://ex.org#hasProcedureSeqNum"

        g = Graph()
        g.add((URIRef(SCOPED_PROP), RDF.type, OWL.DatatypeProperty))
        self._add_source_column(g, SCOPED_PROP, "raw_procedures.seq_num")

        report = _make_report(
            _make_table(
                "raw_procedures", "http://ex.org#Procedure", "Procedure",
                _confirmed("seq_num", uri=SCOPED_PROP),
            ),
        )
        defs = GoldDefinitionBuilder().build(report, ontology_graph=g)

        proc_def = next(d for d in defs if d.gold_table_name == "gold_procedure")
        seq_spec = next(s for s in proc_def.column_specs if s.source_column == "seq_num")
        assert seq_spec.property_uri == SCOPED_PROP

    def test_strategy2_takes_precedence_over_strategy3(self):
        """If fm:sourceForeignKey resolves the column (Strategy 2), Strategy 3 is skipped."""
        FK_PROP = "http://ex.org#hasAdmissionPatient"  # ObjectProperty via Strategy 2
        SC_PROP = "http://ex.org#hasDifferentProp"     # DatatypeProperty via Strategy 3

        g = Graph()
        # Strategy 2: FK annotation
        g.add((URIRef(FK_PROP), RDF.type, OWL.ObjectProperty))
        g.add((URIRef(FK_PROP), self.FM_FK, URIRef.__new__(URIRef, "http://ex.org#sourceForeignKey")))
        from rdflib import Literal as _Lit, URIRef as _U
        FM_FK_PRED = _U("http://forgemesh.ai/ontology#sourceForeignKey")
        g.add((_U(FK_PROP), FM_FK_PRED, _Lit("raw_encounters.subject_id -> raw_patients")))

        # Strategy 3: sourceColumn annotation on a different prop
        g.add((_U(SC_PROP), RDF.type, OWL.DatatypeProperty))
        self._add_source_column(g, SC_PROP, "raw_encounters.subject_id")

        report = _make_report(
            _make_table(
                "raw_encounters", "http://ex.org#Encounter", "Encounter",
                _confirmed("subject_id", uri="http://ex.org#wrongProp"),
            ),
            _make_table(
                "raw_patients", "http://ex.org#Patient", "Patient",
                _confirmed("id", uri="http://ex.org#hasId"),
            ),
        )
        defs = GoldDefinitionBuilder().build(report, ontology_graph=g)

        enc_def = next(d for d in defs if d.gold_table_name == "gold_encounter")
        subj_spec = next(s for s in enc_def.column_specs if s.source_column == "subject_id")
        # Strategy 2 fires → FK_PROP wins; Strategy 3 is never reached
        assert subj_spec.property_uri == FK_PROP


# ---------------------------------------------------------------------------
# Gold mode column naming
# ---------------------------------------------------------------------------

class TestGoldModeColumnNames:
    def test_gold_column_name_uses_safe_source_column(self):
        """gold_column_name is the safe snake_case of the source column (raw table naming)."""
        report = _make_report(
            _make_table("raw_party", "http://ex.org#Party", "Party",
                        _confirmed("DateOfBirth", uri="http://ex.org#hasDateOfBirth"))
        )
        defs = GoldDefinitionBuilder().build(report)
        spec = defs[0].column_specs[0]
        assert spec.gold_column_name == "dateofbirth"
        assert spec.source_column == "DateOfBirth"
        # property_uri is still preserved for RDF/semantic view use
        assert spec.property_uri == "http://ex.org#hasDateOfBirth"

    def test_gold_column_name_no_uri_falls_back_to_source(self):
        """If mapped_property_uri is None, gold_column_name = safe snake_case source column."""
        col = PropertyMapping(
            source_column="party_id",
            source_data_type="VARCHAR(50)",
            is_nullable=False,
            mapped_property_uri=None,
            status=MappingStatus.CONFIRMED,
            confidence_score=0.95,
            rationale="test",
        )
        report = _make_report(
            _make_table("raw_party", "http://ex.org#Party", "Party", col)
        )
        defs = GoldDefinitionBuilder().build(report)
        assert defs[0].column_specs[0].gold_column_name == "party_id"

    def test_deduplication_same_source_column_cross_table_deduped(self):
        """Same source column from two merged tables produces one gold column (first wins)."""
        report = _make_report(
            _make_table("raw_a", "http://ex.org#Party", "Party",
                        _confirmed("name", "VARCHAR(100)", "http://ex.org#hasName")),
            _make_table("raw_b", "http://ex.org#Party", "Party",
                        _confirmed("name", "TEXT", "http://ex.org#hasName")),
        )
        defs = GoldDefinitionBuilder().build(report)
        assert len(defs[0].column_specs) == 1
        assert defs[0].column_specs[0].source_data_type == "VARCHAR(100)"  # first wins

    def test_deduplication_uri_collision_both_kept(self):
        """Two source columns sharing the same property URI: both are retained in gold.

        Gold must faithfully carry every confirmed mapping.  The TripleBuilder emits
        duplicate predicates when URIs collide; the mapping engine should assign distinct
        URIs to avoid that downstream.
        """
        report = _make_report(
            _make_table("raw_a", "http://ex.org#Party", "Party",
                        _confirmed("dob", uri="http://ex.org#hasDateOfBirth"),
                        _confirmed("date_of_birth", uri="http://ex.org#hasDateOfBirth")),
        )
        defs = GoldDefinitionBuilder().build(report)
        col_names = {s.gold_column_name for s in defs[0].column_specs}
        assert "dob" in col_names
        assert "date_of_birth" in col_names


class TestValueMapOverrides:
    def test_override_replaces_report_value_map(self):
        """value_map_overrides takes priority over report value_mappings."""
        from semantic_mapper.schema.mapping_output import ValueMappingRule
        col = _confirmed("status", uri="http://ex.org#hasStatus")
        col = col.model_copy(update={"value_mappings": [
            ValueMappingRule(source_value="ACTIVE", mapped_uri="http://ex.org#Active", rationale="r", confidence_score=0.9),
        ]})
        report = _make_report(_make_table("raw_party", "http://ex.org#Party", "Party", col))

        overrides = {
            "raw_party.status": [
                {"source_value": "ACTIVE", "mapped_uri": "http://ex.org#ActiveOverride"},
                {"source_value": "CLOSED", "mapped_uri": "http://ex.org#ClosedOverride"},
            ]
        }
        defs = GoldDefinitionBuilder().build(report, value_map_overrides=overrides)
        spec = defs[0].column_specs[0]
        assert spec.value_map == {
            "ACTIVE": "http://ex.org#ActiveOverride",
            "CLOSED": "http://ex.org#ClosedOverride",
        }

    def test_no_override_uses_report_value_map(self):
        """Without an override the report's value_mappings are used as normal."""
        from semantic_mapper.schema.mapping_output import ValueMappingRule
        col = _confirmed("status", uri="http://ex.org#hasStatus")
        col = col.model_copy(update={"value_mappings": [
            ValueMappingRule(source_value="ACTIVE", mapped_uri="http://ex.org#Active", rationale="r", confidence_score=0.9),
        ]})
        report = _make_report(_make_table("raw_party", "http://ex.org#Party", "Party", col))

        defs = GoldDefinitionBuilder().build(report)
        spec = defs[0].column_specs[0]
        assert spec.value_map == {"ACTIVE": "http://ex.org#Active"}


# ---------------------------------------------------------------------------
# BUG-04 — Property URI collision handling
# ---------------------------------------------------------------------------

class TestPropertyUriCollision:
    """When two CONFIRMED columns in the same class group share the same mapped_property_uri,
    BOTH columns must reach gold and a warning must be logged.

    Root cause: the LLM mapping engine can assign the same property URI to two semantically
    distinct columns (e.g. Synthea encounter 'code' and 'reasoncode' both mapped to
    hasReasonCode; 'id' and 'patient' both mapped to hasIdentifier).

    The correct behaviour: keep all columns so gold faithfully reflects every confirmed
    mapping.  The TripleBuilder will emit duplicate predicates; the mapping engine should
    assign distinct URIs to avoid that downstream.  Silently dropping data is worse than
    emitting duplicate predicates.
    """

    def test_collision_keeps_both_columns(self):
        """Two confirmed columns sharing the same property URI: both are retained in gold."""
        SHARED_URI = "http://ex.org#hasCode"
        col_a = _confirmed("code",       uri=SHARED_URI)
        col_b = _confirmed("reasoncode", uri=SHARED_URI)   # same URI → collision

        report = _make_report(
            _make_table("raw_encounter", "http://ex.org#Encounter", "Encounter", col_a, col_b)
        )
        defs = GoldDefinitionBuilder().build(report)
        assert len(defs) == 1
        col_names = {s.source_column for s in defs[0].column_specs}
        assert "code" in col_names,       "First column must be retained"
        assert "reasoncode" in col_names, "Second column with duplicate URI must also be retained"

    def test_all_collision_columns_present(self):
        """All columns sharing a property URI are present in document order."""
        SHARED_URI = "http://ex.org#hasValue"
        col_first  = _confirmed("value",    uri=SHARED_URI)
        col_second = _confirmed("valuenum", uri=SHARED_URI)

        report = _make_report(
            _make_table("raw_chartevents", "http://ex.org#ChartEvent", "chartevents",
                        col_first, col_second)
        )
        defs = GoldDefinitionBuilder().build(report)
        retained = [s.source_column for s in defs[0].column_specs]
        assert "value"    in retained, "First column must be present"
        assert "valuenum" in retained, "Second column must also be present"

    def test_distinct_uris_both_included(self):
        """Two confirmed columns with DIFFERENT property URIs must both be in gold."""
        col_a = _confirmed("code",       uri="http://ex.org#hasCode")
        col_b = _confirmed("reasoncode", uri="http://ex.org#hasReasonCode")

        report = _make_report(
            _make_table("raw_encounter", "http://ex.org#Encounter", "Encounter", col_a, col_b)
        )
        defs = GoldDefinitionBuilder().build(report)
        col_names = {s.source_column for s in defs[0].column_specs}
        assert "code" in col_names
        assert "reasoncode" in col_names

    def test_collision_across_merged_tables_both_kept(self):
        """When two source tables merge into one gold table and both have a column
        mapping to the same property URI, both columns must still reach gold."""
        SHARED_URI = "http://ex.org#hasName"
        col_a = _confirmed("name", uri=SHARED_URI)
        col_b = _confirmed("full_name", uri=SHARED_URI)

        report = _make_report(
            _make_table("raw_party",     "http://ex.org#Party", "Party", col_a),
            _make_table("raw_party_ext", "http://ex.org#Party", "Party", col_b),
        )
        defs = GoldDefinitionBuilder().build(report)
        assert len(defs) == 1
        col_names = {s.source_column for s in defs[0].column_specs}
        # Both columns must be present — dropping data is never acceptable
        assert "name"      in col_names
        assert "full_name" in col_names

    def test_empty_property_uri_not_deduplicated(self):
        """Columns with no mapped_property_uri (empty string) must not be cross-deduplicated
        against each other — only populated URIs participate in the collision check."""
        col_a = _confirmed("col_a", uri="")
        col_b = _confirmed("col_b", uri="")

        report = _make_report(
            _make_table("raw_party", "http://ex.org#Party", "Party", col_a, col_b)
        )
        defs = GoldDefinitionBuilder().build(report)
        col_names = {s.source_column for s in defs[0].column_specs}
        # Both should be included since the URI is empty (no collision key)
        assert "col_a" in col_names
        assert "col_b" in col_names

    def test_collision_warning_logged(self, caplog):
        """A WARNING is logged when a property URI collision is detected (both columns kept)."""
        import logging
        SHARED_URI = "http://ex.org#hasCode"
        col_a = _confirmed("code",       uri=SHARED_URI)
        col_b = _confirmed("reasoncode", uri=SHARED_URI)

        report = _make_report(
            _make_table("raw_encounter", "http://ex.org#Encounter", "Encounter", col_a, col_b)
        )
        with caplog.at_level(logging.WARNING, logger="semantic_mapper.gold.builder"):
            defs = GoldDefinitionBuilder().build(report)

        collision_warnings = [r for r in caplog.records if "collision" in r.message.lower()]
        assert len(collision_warnings) >= 1
        assert "reasoncode" in collision_warnings[0].message
        # Both columns must still be in gold despite the warning
        col_names = {s.source_column for s in defs[0].column_specs}
        assert "code"       in col_names
        assert "reasoncode" in col_names


# ---------------------------------------------------------------------------
# C2 fix: collision resolver — loser property_uri cleared, collisions recorded
# ---------------------------------------------------------------------------

class TestPropertyUriCollisionC2Fix:
    """After _resolve_property_uri_collisions(), the lower-confidence loser keeps its
    gold column but has property_uri cleared to "" so TripleBuilder skips it.
    The builder.collisions list records the outcome for mapping-report patching.
    """

    def _confirmed_scored(self, col, uri, score):
        return PropertyMapping(
            source_column=col,
            source_data_type="VARCHAR(50)",
            is_nullable=False,
            mapped_property_uri=uri,
            status=MappingStatus.CONFIRMED,
            confidence_score=score,
            rationale="test",
        )

    def test_higher_confidence_wins_property_uri(self):
        """Winner (higher confidence) retains property_uri; loser gets ""."""
        SHARED_URI = "http://ex.org#hasValue"
        winner_col = self._confirmed_scored("valuenum", SHARED_URI, 0.90)
        loser_col  = self._confirmed_scored("value",    SHARED_URI, 0.75)

        report = _make_report(
            _make_table("raw_chartevents", "http://ex.org#ChartEvent", "chartevent",
                        winner_col, loser_col)
        )
        builder = GoldDefinitionBuilder()
        defs = builder.build(report)

        specs = {s.source_column: s for s in defs[0].column_specs}
        assert specs["valuenum"].property_uri == SHARED_URI, "winner must keep URI"
        assert specs["value"].property_uri == "", "loser must have empty property_uri"

    def test_both_columns_still_in_gold(self):
        """The losing column's data is still written to gold (property_uri cleared only)."""
        SHARED_URI = "http://ex.org#hasValue"
        col_a = self._confirmed_scored("valuenum", SHARED_URI, 0.90)
        col_b = self._confirmed_scored("value",    SHARED_URI, 0.75)

        report = _make_report(
            _make_table("raw_chartevents", "http://ex.org#ChartEvent", "chartevent",
                        col_a, col_b)
        )
        defs = GoldDefinitionBuilder().build(report)
        col_names = {s.source_column for s in defs[0].column_specs}
        assert "valuenum" in col_names
        assert "value"    in col_names

    def test_collisions_list_populated(self):
        """builder.collisions is populated with the loser column details."""
        SHARED_URI = "http://ex.org#hasValue"
        col_a = self._confirmed_scored("valuenum", SHARED_URI, 0.90)
        col_b = self._confirmed_scored("value",    SHARED_URI, 0.75)

        report = _make_report(
            _make_table("raw_chartevents", "http://ex.org#ChartEvent", "chartevent",
                        col_a, col_b)
        )
        builder = GoldDefinitionBuilder()
        builder.build(report)

        assert len(builder.collisions) == 1
        coll = builder.collisions[0]
        assert coll["source_column"] == "value",        "loser is the lower-confidence column"
        assert coll["winner_column"] == "valuenum",     "winner is the higher-confidence column"
        assert coll["property_uri"] == SHARED_URI
        assert coll["loser_confidence"] == 0.75
        assert coll["winner_confidence"] == 0.90

    def test_collisions_cleared_between_builds(self):
        """builder.collisions is reset on each build() call."""
        SHARED_URI = "http://ex.org#hasValue"
        col_a = self._confirmed_scored("valuenum", SHARED_URI, 0.90)
        col_b = self._confirmed_scored("value",    SHARED_URI, 0.75)
        report_collision = _make_report(
            _make_table("raw_chartevents", "http://ex.org#ChartEvent", "chartevent",
                        col_a, col_b)
        )
        no_collision_col = _confirmed("only_col", uri="http://ex.org#hasOnly")
        report_clean = _make_report(
            _make_table("raw_clean", "http://ex.org#Clean", "clean", no_collision_col)
        )

        builder = GoldDefinitionBuilder()
        builder.build(report_collision)
        assert len(builder.collisions) == 1  # collision build

        builder.build(report_clean)
        assert len(builder.collisions) == 0  # cleared; clean build has no collisions

    def test_no_collision_no_uri_cleared(self):
        """When property URIs are distinct, no property_uri is cleared."""
        col_a = self._confirmed_scored("code",       "http://ex.org#hasCode",       0.90)
        col_b = self._confirmed_scored("reasoncode", "http://ex.org#hasReasonCode", 0.75)

        report = _make_report(
            _make_table("raw_encounter", "http://ex.org#Encounter", "encounter",
                        col_a, col_b)
        )
        builder = GoldDefinitionBuilder()
        defs = builder.build(report)

        assert len(builder.collisions) == 0
        specs = {s.source_column: s for s in defs[0].column_specs}
        assert specs["code"].property_uri       == "http://ex.org#hasCode"
        assert specs["reasoncode"].property_uri == "http://ex.org#hasReasonCode"

    def test_triple_builder_skips_cleared_spec(self):
        """TripleBuilder emits no triple for a spec whose property_uri is "".
        The triples/subjects ratio for the property stays exactly 1.0."""
        from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition
        from semantic_mapper.rdf.triple_builder import TripleBuilder
        from semantic_mapper.rdf.uri_factory import UriFactory

        PROP_URI  = "http://ex.org#hasValue"
        CLASS_URI = "http://ex.org#ChartEvent"
        BASE      = "http://ex.org/individual/"

        # Definition with two specs sharing a URI — simulates post-collision state
        # where the loser has property_uri = "".
        winner_spec = GoldColumnSpec(
            silver_table="silver_raw_chartevents",
            source_column="valuenum",
            gold_column_name="valuenum",
            source_data_type="FLOAT",
            property_uri=PROP_URI,
            confidence_score=0.90,
        )
        loser_spec = GoldColumnSpec(
            silver_table="silver_raw_chartevents",
            source_column="value",
            gold_column_name="value",
            source_data_type="VARCHAR(255)",
            property_uri="",  # cleared by C2 collision resolver
            confidence_score=0.75,
        )
        defn = GoldTableDefinition(
            gold_table_name="gold_chartevent",
            primary_class_uri=CLASS_URI,
            class_label="chartevent",
            source_silver_tables=["silver_raw_chartevents"],
            column_specs=[winner_spec, loser_spec],
            id_column="valuenum",
        )

        triple_builder = TripleBuilder(
            definitions=[defn],
            uri_factory=UriFactory(BASE),
        )
        rows = [
            {"valuenum": 98.6, "value": "98.6", "_bronze_source_ref": "ref1", "_gold_run_id": "r1"},
            {"valuenum": 37.0, "value": "37.0", "_bronze_source_ref": "ref2", "_gold_run_id": "r2"},
        ]
        all_triples = []
        for row in rows:
            all_triples.extend(triple_builder.build(row, defn))

        # Count subjects and triples for the winner property
        subjects = {s for s, p, o in all_triples if str(p) == PROP_URI}
        prop_triples = [(s, p, o) for s, p, o in all_triples if str(p) == PROP_URI]
        assert len(prop_triples) == len(subjects), (
            f"triples/subjects ratio must be exactly 1.0 for <{PROP_URI}>; "
            f"got {len(prop_triples)} triples for {len(subjects)} subjects"
        )
        # Confirm the loser emitted no triples for the property
        assert len(prop_triples) == 2, "one triple per row from winner spec only"


# ---------------------------------------------------------------------------
# All confirmed silver mappings must reach gold
# ---------------------------------------------------------------------------

class TestAllConfirmedColumnsReachGold:
    """Every confirmed silver mapping must appear as a gold column regardless of
    property URI collisions.  Gold column count (non-provenance) must equal the
    number of distinct confirmed source columns per class.
    """

    def test_synthea_encounter_four_uri_collisions_all_kept(self):
        """Mirrors the Synthea gold_encounter scenario: four pairs of columns sharing
        property URIs (id+patient → hasIdentifier, code+reasoncode → hasReasonCode,
        description+reasondescription → hasReasonDescription, payer+payer_coverage →
        hasPayerCoverage).  All 15 columns must reach gold_encounter."""
        report = _make_report(
            _make_table(
                "raw_encounters", "http://ex.org#Encounter", "Encounter",
                _confirmed("id",                 uri="http://ex.org#hasIdentifier"),
                _confirmed("start",              uri="http://ex.org#hasStartDateTime"),
                _confirmed("stop",               uri="http://ex.org#hasStopDateTime"),
                _confirmed("patient",            uri="http://ex.org#hasIdentifier"),         # collision 1
                _confirmed("organization",       uri="http://ex.org#hasOrganisation"),
                _confirmed("provider",           uri="http://ex.org#hasProvider"),
                _confirmed("payer",              uri="http://ex.org#hasPayerCoverage"),
                _confirmed("encounterclass",     uri="http://ex.org#hasEncounterClass"),
                _confirmed("code",               uri="http://ex.org#hasReasonCode"),
                _confirmed("description",        uri="http://ex.org#hasReasonDescription"),
                _confirmed("base_encounter_cost", uri="http://ex.org#hasBaseEncounterCost"),
                _confirmed("total_claim_cost",   uri="http://ex.org#hasTotalClaimCost"),
                _confirmed("payer_coverage",     uri="http://ex.org#hasPayerCoverage"),      # collision 2
                _confirmed("reasoncode",         uri="http://ex.org#hasReasonCode"),         # collision 3
                _confirmed("reasondescription",  uri="http://ex.org#hasReasonDescription"),  # collision 4
            )
        )
        defs = GoldDefinitionBuilder().build(report)
        assert len(defs) == 1
        gold_cols = {s.source_column for s in defs[0].column_specs}
        expected = {
            "id", "start", "stop", "patient", "organization", "provider", "payer",
            "encounterclass", "code", "description", "base_encounter_cost",
            "total_claim_cost", "payer_coverage", "reasoncode", "reasondescription",
        }
        assert gold_cols == expected, (
            f"Missing from gold: {expected - gold_cols}\n"
            f"Unexpected in gold: {gold_cols - expected}"
        )

    def test_silver_confirmed_count_equals_gold_column_count(self):
        """For each gold table: the number of distinct confirmed source columns equals
        the number of GoldColumnSpec entries (non-provenance columns start with '_')."""
        report = _make_report(
            _make_table(
                "raw_patient", "http://ex.org#Patient", "Patient",
                _confirmed("patient_id", uri="http://ex.org#hasId"),
                _confirmed("birthdate",  uri="http://ex.org#hasBirthDate"),
            ),
            _make_table(
                "raw_encounter", "http://ex.org#Encounter", "Encounter",
                _confirmed("id",                  uri="http://ex.org#hasIdentifier"),
                _confirmed("patient",             uri="http://ex.org#hasIdentifier"),
                _confirmed("code",                uri="http://ex.org#hasReasonCode"),
                _confirmed("reasoncode",          uri="http://ex.org#hasReasonCode"),
                _confirmed("description",         uri="http://ex.org#hasReasonDescription"),
                _confirmed("reasondescription",   uri="http://ex.org#hasReasonDescription"),
            ),
        )
        defs = GoldDefinitionBuilder().build(report)

        # Build expected counts: distinct confirmed source columns per class URI.
        expected: dict = {}
        for tm in report.tables:
            class_uri = max(tm.class_candidate_set, key=lambda c: c.confidence_score).uri
            confirmed_cols = {
                m.source_column for m in tm.column_mappings
                if m.status.value == "confirmed"
            }
            expected.setdefault(class_uri, set()).update(confirmed_cols)

        for defn in defs:
            data_cols = [s for s in defn.column_specs
                         if not s.gold_column_name.startswith("_")]
            expected_count = len(expected[defn.primary_class_uri])
            assert len(data_cols) == expected_count, (
                f"{defn.gold_table_name}: expected {expected_count} gold columns "
                f"(one per confirmed mapping), got {len(data_cols)}: "
                f"{[s.source_column for s in data_cols]}"
            )
