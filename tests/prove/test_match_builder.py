# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Phase 7 — Tests for infer_match_links() and related match-record helpers.
"""
from __future__ import annotations

import pytest

from semantic_mapper.prove.composer import infer_match_links, _pred_local_name


# ---------------------------------------------------------------------------
# _pred_local_name unit tests
# ---------------------------------------------------------------------------


class TestPredLocalName:
    def test_prefixed_predicate(self):
        assert _pred_local_name("ont:hasDiagnosis") == "hasDiagnosis"

    def test_full_uri_hash(self):
        assert _pred_local_name("<http://ont#hasIcdDiagnose>") == "hasIcdDiagnose"

    def test_full_uri_slash(self):
        assert _pred_local_name("<http://ont/property/hasSomething>") == "hasSomething"

    def test_bare_token(self):
        assert _pred_local_name("a") == "a"


# ---------------------------------------------------------------------------
# infer_match_links unit tests
# ---------------------------------------------------------------------------


_WHERE_SIMPLE = """
    ?admission a ont:Admission .
    ?admission ont:hasDiagnosesIcd ?diagnosesicd .
    ?diagnosesicd ont:hasIcdDiagnose ?icddiagnose .
"""

_WHERE_WITH_LITERAL = """
    ?admission a ont:Admission .
    ?admission ont:hasHospitalExpireFlag "1" .
    ?admission ont:hasDiagnosesIcd ?diagnosesicd .
"""

_WHERE_FILTER = """
    ?adm a ont:Admission .
    ?adm ont:hasIcdDiagnosis ?diag .
    FILTER(?diag != <http://ex/0>)
"""

_WHERE_SELF_LINK = """
    ?admission ont:relatedTo ?admission .
"""


class TestInferMatchLinks:
    def test_finds_object_property_link(self):
        bound = {"admission", "diagnosesicd", "icddiagnose"}
        links = infer_match_links(_WHERE_SIMPLE, bound)
        from_roles = {l["from_role"] for l in links}
        to_roles   = {l["to_role"]   for l in links}
        assert "admission"   in from_roles
        assert "diagnosesicd" in to_roles

    def test_chain_has_two_links(self):
        bound = {"admission", "diagnosesicd", "icddiagnose"}
        links = infer_match_links(_WHERE_SIMPLE, bound)
        assert len(links) == 2

    def test_link_via_property_extracted(self):
        bound = {"admission", "diagnosesicd"}
        links = infer_match_links(_WHERE_SIMPLE, bound)
        via = {l["via_property"] for l in links}
        assert "hasDiagnosesIcd" in via

    def test_literal_valued_triples_not_included(self):
        bound = {"admission", "diagnosesicd"}
        links = infer_match_links(_WHERE_WITH_LITERAL, bound)
        # Only the URI→URI link should be present
        assert len(links) == 1
        assert links[0]["from_role"] == "admission"
        assert links[0]["to_role"] == "diagnosesicd"

    def test_filter_patterns_skipped(self):
        bound = {"adm", "diag"}
        links = infer_match_links(_WHERE_FILTER, bound)
        # The FILTER pattern must not produce a link
        via_props = {l["via_property"] for l in links}
        assert "!=" not in via_props
        assert len(links) == 1

    def test_unbound_vars_not_linked(self):
        # diagnosesicd not in bound_vars
        bound = {"admission"}
        links = infer_match_links(_WHERE_SIMPLE, bound)
        assert links == []

    def test_empty_where_body_returns_empty(self):
        links = infer_match_links("", {"admission", "diagnosesicd"})
        assert links == []

    def test_no_duplicate_links(self):
        where = """
            ?adm ont:hasIcd ?diag .
            ?adm ont:hasIcd ?diag .
        """
        bound = {"adm", "diag"}
        links = infer_match_links(where, bound)
        assert len(links) == 1

    def test_link_contains_required_keys(self):
        bound = {"admission", "diagnosesicd"}
        links = infer_match_links(_WHERE_WITH_LITERAL, bound)
        assert len(links) == 1
        lnk = links[0]
        assert {"from_role", "to_role", "via_property", "via_property_uri"} == set(lnk.keys())

    def test_non_sparql_mode_passthrough(self):
        """infer_match_links with SQL WHERE-like text returns empty — no triples."""
        links = infer_match_links("admission_id = 123 AND diag_code = 'A01'", {"admission", "diag"})
        assert links == []
