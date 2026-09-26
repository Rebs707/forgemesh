# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for the MCP schema-freshness helpers (Bug 2 hardening).

Pure logic, no live server: the fingerprint detects on-disk artifact changes (so the
server self-refreshes) and the agreement check catches a schema that describes a
different dataset than the materialised graph. Generic — no dataset-specific strings.
"""
from __future__ import annotations

from semantic_mapper.mcp.schema_freshness import (
    artifact_fingerprint,
    namespace_of,
    schema_graph_disagreements,
)


class TestArtifactFingerprint:
    def test_changes_when_a_file_is_rewritten(self, tmp_path):
        a = tmp_path / "report.json"; a.write_text("v1")
        b = tmp_path / "onto.owl"; b.write_text("o1")
        fp1 = artifact_fingerprint([str(a), str(b)])
        import os
        os.utime(a, (fp1[0][1] + 10, fp1[0][1] + 10))  # bump mtime
        fp2 = artifact_fingerprint([str(a), str(b)])
        assert fp1 != fp2

    def test_stable_when_unchanged(self, tmp_path):
        a = tmp_path / "report.json"; a.write_text("v1")
        assert artifact_fingerprint([str(a)]) == artifact_fingerprint([str(a)])

    def test_order_independent(self, tmp_path):
        a = tmp_path / "a"; a.write_text("x")
        b = tmp_path / "b"; b.write_text("y")
        assert artifact_fingerprint([str(a), str(b)]) == artifact_fingerprint([str(b), str(a)])

    def test_missing_file_is_none_and_differs_from_present(self, tmp_path):
        a = tmp_path / "gone"
        fp_absent = artifact_fingerprint([str(a)])
        a.write_text("now here")
        fp_present = artifact_fingerprint([str(a)])
        assert fp_absent != fp_present

    def test_relative_paths_resolve_against_base_dir(self, tmp_path):
        (tmp_path / "r.json").write_text("x")
        fp = artifact_fingerprint(["r.json"], base_dir=str(tmp_path))
        assert fp[0][1] is not None  # resolved and found

    def test_blank_paths_ignored(self):
        assert artifact_fingerprint(["", None]) == ()  # type: ignore[list-item]


class TestNamespaceOf:
    def test_hash_namespace(self):
        assert namespace_of("http://x/ontology/mimic#Admission") == "http://x/ontology/mimic#"

    def test_slash_namespace(self):
        assert namespace_of("http://x/ontology/mimic/Admission") == "http://x/ontology/mimic/"


class TestSchemaGraphDisagreements:
    NS = "http://mesh.local/ontology/mimic#"
    OLD = "http://mesh.local/ontology/mimic_bronze#"

    def test_agrees_when_namespace_and_classes_match(self):
        served = [self.NS + "Admission", self.NS + "Patient"]
        graph = [self.NS + "Admission", self.NS + "Patient", self.NS + "Chartevent"]
        assert schema_graph_disagreements(self.NS, served, graph) == []

    def test_flags_stale_namespace(self):
        # The classic bug: export on the old namespace, graph on the new one.
        served = [self.OLD + "Admission"]
        graph = [self.NS + "Admission", self.NS + "Patient"]
        reasons = schema_graph_disagreements(self.OLD, served, graph)
        assert any("different dataset" in r for r in reasons)

    def test_flags_when_no_served_class_has_instances(self):
        served = [self.NS + "Ghost"]
        graph = [self.NS + "Admission"]
        reasons = schema_graph_disagreements(self.NS, served, graph)
        assert any("no" in r.lower() and "instance" in r.lower() for r in reasons)

    def test_empty_graph_reports_nothing(self):
        # Graph unreachable / not materialised → cannot judge, stay silent.
        assert schema_graph_disagreements(self.NS, [self.NS + "Admission"], []) == []
