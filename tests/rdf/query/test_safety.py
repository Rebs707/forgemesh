# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from semantic_mapper.core.exceptions import QuerySafetyError
from semantic_mapper.rdf.query.safety import validate_sparql_select_only


class TestValidateSparqlSelectOnly:
    def test_plain_select_passes(self):
        validate_sparql_select_only("SELECT ?s WHERE { ?s a <http://ex.org/Patient> }")

    def test_select_with_prefix_passes(self):
        sparql = (
            "PREFIX ex: <http://ex.org/>\n"
            "SELECT ?s WHERE { ?s a ex:Patient }"
        )
        validate_sparql_select_only(sparql)

    def test_select_with_hash_comment_passes(self):
        sparql = (
            "# find all patients\n"
            "SELECT ?s WHERE { ?s a <http://ex.org/Patient> }"
        )
        validate_sparql_select_only(sparql)

    def test_multiple_prefixes_passes(self):
        sparql = (
            "PREFIX ex: <http://ex.org/>\n"
            "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
            "SELECT ?s ?d WHERE { ?s a ex:Patient ; ex:dateOfBirth ?d }"
        )
        validate_sparql_select_only(sparql)

    def test_construct_raises(self):
        with pytest.raises(QuerySafetyError, match="SELECT"):
            validate_sparql_select_only("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }")

    def test_ask_raises(self):
        with pytest.raises(QuerySafetyError, match="SELECT"):
            validate_sparql_select_only("ASK { ?s a <http://ex.org/Patient> }")

    def test_describe_raises(self):
        with pytest.raises(QuerySafetyError, match="SELECT"):
            validate_sparql_select_only("DESCRIBE <http://ex.org/patient/P1>")

    def test_insert_raises(self):
        with pytest.raises(QuerySafetyError):
            validate_sparql_select_only(
                "SELECT ?s WHERE { ?s a <http://ex.org/Patient> } INSERT DATA { <a> <b> <c> }"
            )

    def test_delete_raises(self):
        with pytest.raises(QuerySafetyError):
            validate_sparql_select_only(
                "DELETE { ?s ?p ?o } WHERE { ?s ?p ?o }"
            )

    def test_drop_raises(self):
        with pytest.raises(QuerySafetyError):
            validate_sparql_select_only("DROP GRAPH <http://ex.org/graph>")

    def test_clear_raises(self):
        with pytest.raises(QuerySafetyError):
            validate_sparql_select_only("CLEAR GRAPH <http://ex.org/graph>")

    def test_load_raises(self):
        with pytest.raises(QuerySafetyError):
            validate_sparql_select_only("LOAD <http://ex.org/data.ttl>")

    def test_banned_keyword_in_select_raises(self):
        # INSERT buried inside an otherwise SELECT-starting query
        sparql = "SELECT ?s WHERE { ?s a <http://ex.org/Patient> } ; INSERT DATA { <a> <b> <c> }"
        with pytest.raises(QuerySafetyError, match="INSERT"):
            validate_sparql_select_only(sparql)

    def test_comment_cannot_hide_construct(self):
        # CONSTRUCT is not a comment — stripping # comments should not hide it
        sparql = "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"
        with pytest.raises(QuerySafetyError):
            validate_sparql_select_only(sparql)
