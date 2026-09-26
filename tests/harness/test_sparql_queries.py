# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""SPARQL query harness.

Each test case represents a natural-language question and the SPARQL the LLM
is expected to generate.  The query executes directly against an in-memory
rdflib Dataset populated with the same logical test data as the SQL harness.

patientHasEncounter triples are stored in the FLIPPED direction:
    <patient/Pxxx> patientHasEncounter <encounter/Exxx>
because rdfs:range=Encounter → TripleBuilder makes the patient the subject.
SPARQL queries must reflect this direction.

Add a new case whenever a real-world SPARQL failure is found.

Assertion keys per case:
  question          — natural-language question (for documentation)
  sparql            — SPARQL SELECT to test
  expect_row_count  — expected number of result rows
  expect_values     — optional list of {var: value} dicts to check in results
"""
from __future__ import annotations

import pytest

from tests.harness.conftest import (
    BASE,
    ENCOUNTER_CLASS,
    NAMED_GRAPH,
    NS,
    PATIENT_CLASS,
    PROP_PAT_HAS_ENC,
    sparql_against_dataset,
)

PAT_BASE = f"{BASE}patient/"
ENC_BASE = f"{BASE}encounter/"

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

SPARQL_CASES = [
    # ------------------------------------------------------------------
    # Count queries
    # ------------------------------------------------------------------
    {
        "id": "count_patients",
        "question": "How many patients are there?",
        "sparql": f"""
            PREFIX ex: <{NS}>
            SELECT (COUNT(?s) AS ?count)
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    ?s a ex:Patient .
                }}
            }}
        """,
        "expect_row_count": 1,
        "expect_values": [{"count": "3"}],
    },
    {
        "id": "count_encounters",
        "question": "How many encounters are there?",
        "sparql": f"""
            PREFIX ex: <{NS}>
            SELECT (COUNT(?s) AS ?count)
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    ?s a ex:Encounter .
                }}
            }}
        """,
        "expect_row_count": 1,
        "expect_values": [{"count": "3"}],
    },
    # ------------------------------------------------------------------
    # List all instances
    # ------------------------------------------------------------------
    {
        "id": "list_all_patients",
        "question": "List all patients.",
        "sparql": f"""
            PREFIX ex: <{NS}>
            SELECT ?s
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    ?s a ex:Patient .
                }}
            }}
            LIMIT 100
        """,
        "expect_row_count": 3,
    },
    {
        "id": "list_all_encounters",
        "question": "List all encounters.",
        "sparql": f"""
            PREFIX ex: <{NS}>
            SELECT ?s
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    ?s a ex:Encounter .
                }}
            }}
            LIMIT 100
        """,
        "expect_row_count": 3,
    },
    # ------------------------------------------------------------------
    # Property access
    # ------------------------------------------------------------------
    {
        "id": "patient_first_names",
        "question": "List patient first names.",
        "sparql": f"""
            PREFIX ex: <{NS}>
            SELECT ?s ?name
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    ?s a ex:Patient .
                    ?s ex:firstName ?name .
                }}
            }}
            LIMIT 100
        """,
        "expect_row_count": 3,
    },
    {
        "id": "encounter_dates",
        "question": "List encounter dates.",
        "sparql": f"""
            PREFIX ex: <{NS}>
            SELECT ?s ?date
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    ?s a ex:Encounter .
                    ?s ex:encounterDate ?date .
                }}
            }}
            LIMIT 100
        """,
        "expect_row_count": 3,
    },
    # ------------------------------------------------------------------
    # FILTER queries
    # ------------------------------------------------------------------
    {
        "id": "filter_female_patients",
        "question": "List female patients.",
        "sparql": f"""
            PREFIX ex: <{NS}>
            SELECT ?s ?name
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    ?s a ex:Patient .
                    ?s ex:firstName ?name .
                    ?s ex:gender ?g .
                    FILTER(?g = "Female")
                }}
            }}
            LIMIT 100
        """,
        "expect_row_count": 2,
    },
    {
        "id": "filter_high_cost_encounters",
        "question": "Show encounters with total cost above 100.",
        "sparql": f"""
            PREFIX ex: <{NS}>
            PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
            SELECT ?s ?cost
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    ?s a ex:Encounter .
                    ?s ex:totalCost ?cost .
                    FILTER(xsd:decimal(?cost) > 100)
                }}
            }}
            LIMIT 100
        """,
        "expect_row_count": 2,
    },
    {
        "id": "filter_patient_by_id",
        "question": "Show details for patient P001.",
        "sparql": f"""
            PREFIX ex: <{NS}>
            SELECT ?name ?dob ?gender
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    <{PAT_BASE}P001> ex:firstName ?name .
                    <{PAT_BASE}P001> ex:dateOfBirth ?dob .
                    <{PAT_BASE}P001> ex:gender ?gender .
                }}
            }}
        """,
        "expect_row_count": 1,
        "expect_values": [{"name": "Alice", "gender": "Female"}],
    },
    # ------------------------------------------------------------------
    # Object property (JOIN via patientHasEncounter — FLIPPED direction)
    # Triples: <patient/Pxxx> patientHasEncounter <encounter/Exxx>
    # ------------------------------------------------------------------
    {
        "id": "encounters_for_patient_p001",
        "question": "Which encounters belong to patient P001?",
        "sparql": f"""
            PREFIX ex: <{NS}>
            SELECT ?enc
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    <{PAT_BASE}P001> ex:patientHasEncounter ?enc .
                }}
            }}
            LIMIT 100
        """,
        "expect_row_count": 2,
    },
    {
        "id": "all_patient_encounter_pairs",
        "question": "List all patient–encounter pairs.",
        "sparql": f"""
            PREFIX ex: <{NS}>
            SELECT ?pat ?enc
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    ?pat ex:patientHasEncounter ?enc .
                    ?enc a ex:Encounter .
                }}
            }}
            LIMIT 100
        """,
        "expect_row_count": 3,
    },
    {
        "id": "patient_name_with_encounter_date",
        "question": "List patient names with their encounter dates.",
        "sparql": f"""
            PREFIX ex: <{NS}>
            SELECT ?name ?date
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    ?pat a ex:Patient .
                    ?pat ex:firstName ?name .
                    ?pat ex:patientHasEncounter ?enc .
                    ?enc ex:encounterDate ?date .
                }}
            }}
            LIMIT 100
        """,
        "expect_row_count": 3,
    },
    # ------------------------------------------------------------------
    # OPTIONAL
    # ------------------------------------------------------------------
    {
        "id": "patients_with_optional_dob",
        "question": "List all patients, optionally including date of birth.",
        "sparql": f"""
            PREFIX ex: <{NS}>
            SELECT ?s ?name ?dob
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    ?s a ex:Patient .
                    ?s ex:firstName ?name .
                    OPTIONAL {{ ?s ex:dateOfBirth ?dob }}
                }}
            }}
            LIMIT 100
        """,
        "expect_row_count": 3,
    },
    # ------------------------------------------------------------------
    # DISTINCT
    # ------------------------------------------------------------------
    {
        "id": "distinct_genders",
        "question": "What genders are in the patient population?",
        "sparql": f"""
            PREFIX ex: <{NS}>
            SELECT DISTINCT ?gender
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    ?s a ex:Patient .
                    ?s ex:gender ?gender .
                }}
            }}
        """,
        "expect_row_count": 2,
    },
    # ------------------------------------------------------------------
    # Subquery / GROUP BY
    # ------------------------------------------------------------------
    {
        "id": "count_encounters_per_patient",
        "question": "How many encounters does each patient have?",
        "sparql": f"""
            PREFIX ex: <{NS}>
            SELECT ?pat (COUNT(?enc) AS ?count)
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    ?pat a ex:Patient .
                    ?pat ex:patientHasEncounter ?enc .
                }}
            }}
            GROUP BY ?pat
            LIMIT 100
        """,
        "expect_row_count": 2,  # P001 (2 encounters) + P002 (1 encounter)
    },
    {
        # Regression: xsd:dateTime range filter must return non-zero results.
        # This test was added after a silent data bug where datetime triples were
        # stored as xsd:string (MySQL space format not recognised by _DATETIME_RE),
        # causing properly-typed FILTER comparisons to silently return 0 rows.
        "id": "datetime_range_filter",
        "question": "How many encounters were admitted before July 2023?",
        "sparql": f"""
            PREFIX ex: <{NS}>
            PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
            SELECT ?enc
            WHERE {{
                GRAPH <{NAMED_GRAPH}> {{
                    ?enc a ex:Encounter .
                    ?enc ex:admitTime ?t .
                    FILTER(?t < "2023-07-01T00:00:00"^^xsd:dateTime)
                }}
            }}
            LIMIT 100
        """,
        "expect_row_count": 2,  # E001 (Jan) + E002 (Jun); E003 (Sep) excluded
    },
]


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", SPARQL_CASES, ids=[c["id"] for c in SPARQL_CASES])
def test_sparql_query(case, rdf_dataset):
    """Execute the SPARQL directly against the rdflib Dataset and assert results.

    This harness verifies that the SPARQL the LLM is expected to generate is
    syntactically valid and returns correct results from known test data.
    Runs fully offline — no Fuseki required.
    """
    rows = sparql_against_dataset(rdf_dataset, case["sparql"])

    assert len(rows) == case["expect_row_count"], (
        f"[{case['id']}] Expected {case['expect_row_count']} rows, got {len(rows)}.\n"
        f"SPARQL: {case['sparql'].strip()}\nRows: {rows}"
    )

    if "expect_values" in case:
        for expected_row in case["expect_values"]:
            # Check that at least one returned row matches all expected key/values
            match = any(
                all(str(row.get(k)) == str(v) for k, v in expected_row.items())
                for row in rows
            )
            assert match, (
                f"[{case['id']}] Expected to find row {expected_row} in results.\n"
                f"Got: {rows}"
            )
