# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Terminology source adapters + registry."""
from __future__ import annotations

from semantic_mapper.ontology.terminology.sources import (
    OfflineCodesetSource,
    build_sources,
)

from .builders import offline_source_config


class TestOfflineCodesetSource:
    def test_contains_by_code(self) -> None:
        s = OfflineCodesetSource("sys", "Sys", codes=["A1", "B2"],
                                 iri_template="http://x/{code}")
        assert s.contains("A1")
        assert s.contains(" A1 ")          # whitespace-tolerant
        assert not s.contains("ZZ")

    def test_contains_by_label_casefold(self) -> None:
        s = OfflineCodesetSource("sys", "Sys", codes={"A1": "Alpha One"},
                                 iri_template="http://x/{code}")
        assert s.contains("alpha one")     # match by label, case-insensitive
        assert s.concept_iri("alpha one") == "http://x/A1"

    def test_concept_iri_minting(self) -> None:
        s = OfflineCodesetSource("sys", "Sys", codes=["12345"],
                                 iri_template="http://snomed.info/id/{code}")
        assert s.concept_iri("12345") == "http://snomed.info/id/12345"
        assert s.concept_iri("nope") is None

    def test_no_template_means_no_iri_but_membership_holds(self) -> None:
        s = OfflineCodesetSource("sys", "Sys", codes=["A1"], system_iri="http://x/sys")
        assert s.contains("A1")
        assert s.concept_iri("A1") is None
        assert s.system_iri() == "http://x/sys"

    def test_empty_codeset_binds_nothing(self) -> None:
        s = OfflineCodesetSource("sys", "Sys", codes=[], iri_template="http://x/{code}")
        assert not s.contains("A1")


class TestRegistry:
    def test_build_from_config(self) -> None:
        cfg = offline_source_config("snomedct", system_label="SNOMED CT",
                                    codes={"22298006": "MI"},
                                    iri_template="http://snomed.info/id/{code}",
                                    system_iri="http://snomed.info/sct")
        sources = build_sources(cfg)
        assert len(sources) == 1
        s = sources[0]
        assert s.source_id == "snomedct"
        assert s.system_label == "SNOMED CT"
        assert s.contains("22298006")
        assert s.concept_iri("22298006") == "http://snomed.info/id/22298006"
        assert s.system_iri() == "http://snomed.info/sct"

    def test_empty_config(self) -> None:
        assert build_sources({}) == []

    def test_unknown_adapter_skipped_not_raised(self) -> None:
        cfg = {"weird": {"adapter": "does_not_exist", "codes": ["A"]}}
        assert build_sources(cfg) == []     # logged + skipped, never raises

    def test_malformed_entry_skipped(self) -> None:
        cfg = {"bad": "not-a-mapping"}
        assert build_sources(cfg) == []

    def test_stub_service_alias_resolves(self) -> None:
        cfg = {"svc": {"adapter": "stub_service", "codes": ["A1"],
                       "iri_template": "http://x/{code}"}}
        sources = build_sources(cfg)
        assert len(sources) == 1 and sources[0].contains("A1")
