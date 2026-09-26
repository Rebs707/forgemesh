# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""UriFactory mints {base}/{source}/{class}/{16-hex-hash} URIs.

The identifier is reduced to a deterministic 64-bit hash (uniform + URI-safe for any
key shape); the class stays readable in the path. The SAME transform is used by the FK
resolver so joins reconstruct the identical URI.
"""
from __future__ import annotations

import re

from rdflib import URIRef

from semantic_mapper.rdf.uri_factory import UriFactory, hash_identifier

_HEX16 = re.compile(r"^[0-9a-f]{16}$")


class TestUriFactoryBasic:
    def test_structure_class_readable_id_hashed(self):
        factory = UriFactory("http://mesh.local/data/")
        result = factory.individual("patient", "P1")
        assert result == URIRef(f"http://mesh.local/data/patient/{hash_identifier('P1')}")

    def test_id_segment_is_16_hex(self):
        result = UriFactory("http://mesh.local/data/").individual("patient", "P1")
        assert _HEX16.match(str(result).rsplit("/", 1)[-1])

    def test_class_stays_in_path(self):
        result = UriFactory("http://mesh.local/data/").individual("encounter", "E99")
        assert "/encounter/" in str(result)

    def test_base_without_trailing_slash_normalised(self):
        a = UriFactory("http://mesh.local/data").individual("patient", "P1")
        b = UriFactory("http://mesh.local/data/").individual("patient", "P1")
        assert a == b

    def test_returns_uriref(self):
        assert isinstance(UriFactory("http://mesh.local/data/").individual("e", "1"), URIRef)


class TestDeterminismAndUniqueness:
    def test_same_value_same_uri(self):
        f = UriFactory("http://mesh.local/data/")
        assert f.individual("patient", "P1") == f.individual("patient", "P1")

    def test_different_values_differ(self):
        f = UriFactory("http://mesh.local/data/")
        assert f.individual("patient", "P1") != f.individual("patient", "P2")

    def test_normalisation_strips_whitespace(self):
        # the FK side and the subject side must hash the "same" value identically
        assert hash_identifier("P1") == hash_identifier("  P1  ")

    def test_numeric_and_string_form_match(self):
        # row value may arrive as int or str; both must hash the same
        assert hash_identifier(139896) == hash_identifier("139896")

    def test_same_value_different_class_no_collision(self):
        f = UriFactory("http://mesh.local/data/")
        # same hash, but namespaced by the class path → distinct URIs
        assert f.individual("athlete", "50140") != f.individual("coach", "50140")


class TestAnyKeyShapeIsUriSafe:
    def test_spaces_slashes_hashes_equals_all_clean(self):
        f = UriFactory("http://mesh.local/data/")
        for ugly in ("abc def", "a/b", "abc#123", "raw_x.id=E1", "Cortina Curling Olympic Stadium"):
            seg = str(f.individual("patient", ugly)).rsplit("/", 1)[-1]
            assert _HEX16.match(seg), ugly  # no %-encoding, no special chars leak through


class TestSourceScoped:
    def test_source_name_included(self):
        f = UriFactory("http://mesh.local/data/", source_name="synthea")
        assert str(f.individual("patient", "P1")).startswith("http://mesh.local/data/synthea/patient/")

    def test_empty_source_unscoped(self):
        f = UriFactory("http://mesh.local/data/", source_name="")
        assert str(f.individual("patient", "P1")).startswith("http://mesh.local/data/patient/")

    def test_whitespace_only_source_treated_empty(self):
        f = UriFactory("http://mesh.local/data/", source_name="   ")
        assert str(f.individual("patient", "P1")).startswith("http://mesh.local/data/patient/")

    def test_different_sources_differ(self):
        a = UriFactory("http://mesh.local/data/", source_name="A").individual("patient", "P1")
        b = UriFactory("http://mesh.local/data/", source_name="B").individual("patient", "P1")
        assert a != b
