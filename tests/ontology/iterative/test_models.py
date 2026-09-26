# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import pytest
from pydantic import ValidationError
from semantic_mapper.ontology.iterative.models import (
    AccumulatedClassSummary,
    ColumnContext,
    JudgeVerdict,
    OntologyFragment,
    TableContext,
)


class TestColumnContext:
    def test_defaults(self):
        col = ColumnContext(
            table_name="t",
            col_name="c",
            inferred_type="VARCHAR",
            is_pk=False,
            is_fk=False,
            null_rate=0.0,
            distinct_count=10,
        )
        assert col.top_values == []
        assert col.categorical_values == []
        assert col.is_categorical is False

    def test_fk_ref_table(self):
        col = ColumnContext(
            table_name="t", col_name="c", inferred_type="INT",
            is_pk=False, is_fk=True, fk_ref_table="patients",
            null_rate=0.0, distinct_count=500,
        )
        assert col.fk_ref_table == "patients"


class TestJudgeVerdict:
    def test_approved_defaults(self):
        v = JudgeVerdict(approved=True)
        assert v.coverage_issues == []
        assert v.coherence_issues == []
        assert v.confidence == 0.0
        assert v.revision_instructions == ""

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            JudgeVerdict(approved=True, confidence=1.5)
        with pytest.raises(ValidationError):
            JudgeVerdict(approved=True, confidence=-0.1)

    def test_full_verdict(self):
        v = JudgeVerdict(
            approved=False,
            coverage_issues=["value: missing property"],
            coherence_issues=["wrong namespace"],
            revision_instructions="Fix namespace",
            confidence=0.4,
        )
        assert not v.approved
        assert len(v.coverage_issues) == 1
        assert v.confidence == 0.4


class TestOntologyFragment:
    def test_defaults(self):
        f = OntologyFragment(table_name="t", manchester_syntax="Class: <http://x#T>")
        assert f.retry_count == 0
        assert f.classes_declared == []


class TestAccumulatedClassSummary:
    def test_basic(self):
        cls = AccumulatedClassSummary(
            local_name="Patient", label="Patient",
            uri="http://x#Patient", source_tables=["raw_patients"],
        )
        assert cls.source_tables == ["raw_patients"]
