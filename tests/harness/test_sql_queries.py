# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""SQL query harness.

Each test case represents a natural-language question and the SQL the LLM is
expected to generate for it.  The SQL executes directly against an in-memory
SQLite gold database populated with known test data.

Add a new entry to SQL_CASES whenever a real-world query failure is found — this
prevents regressions and documents the expected query pattern.

Assertion keys per case:
  question          — natural-language question (for documentation / IDs)
  sql               — SQL that the LLM should generate (executed against SQLite)
  expect_row_count  — expected number of result rows
  expect_value      — optional {column: value} dict checked on first row
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

SQL_CASES = [
    # ------------------------------------------------------------------
    # Count queries
    # ------------------------------------------------------------------
    {
        "id": "count_patients",
        "question": "How many patients are there?",
        "sql": "SELECT COUNT(*) AS count FROM `gold_patient` LIMIT 100",
        "expect_row_count": 1,
        "expect_value": {"count": 3},
    },
    {
        "id": "count_encounters",
        "question": "How many encounters are in the database?",
        "sql": "SELECT COUNT(*) AS count FROM `gold_encounter` LIMIT 100",
        "expect_row_count": 1,
        "expect_value": {"count": 3},
    },
    # ------------------------------------------------------------------
    # Simple selects
    # ------------------------------------------------------------------
    {
        "id": "list_all_patients",
        "question": "List all patients.",
        "sql": "SELECT `id`, `first_name`, `date_of_birth`, `gender` FROM `gold_patient` LIMIT 100",
        "expect_row_count": 3,
    },
    {
        "id": "list_all_encounters",
        "question": "Show all encounters.",
        "sql": "SELECT `id`, `patient`, `encounter_date`, `total_cost` FROM `gold_encounter` LIMIT 100",
        "expect_row_count": 3,
    },
    # ------------------------------------------------------------------
    # WHERE filters
    # ------------------------------------------------------------------
    {
        "id": "filter_female_patients",
        "question": "Show all female patients.",
        "sql": "SELECT `id`, `first_name` FROM `gold_patient` WHERE `gender` = 'Female' LIMIT 100",
        "expect_row_count": 2,
    },
    {
        "id": "filter_patient_by_id",
        "question": "Show details for patient P001.",
        "sql": "SELECT * FROM `gold_patient` WHERE `id` = 'P001' LIMIT 100",
        "expect_row_count": 1,
        "expect_value": {"first_name": "Alice"},
    },
    {
        "id": "filter_encounters_by_patient",
        "question": "Show encounters for patient P001.",
        "sql": "SELECT `id`, `encounter_date`, `total_cost` FROM `gold_encounter` WHERE `patient` = 'P001' LIMIT 100",
        "expect_row_count": 2,
    },
    {
        "id": "filter_dob_before_1985",
        "question": "List patients born before 1985.",
        "sql": "SELECT `id`, `first_name`, `date_of_birth` FROM `gold_patient` WHERE `date_of_birth` < '1985-01-01' LIMIT 100",
        "expect_row_count": 1,
        "expect_value": {"first_name": "Bob"},
    },
    {
        "id": "filter_high_cost_encounters",
        "question": "Show encounters with total cost above 100.",
        "sql": "SELECT `id`, `total_cost` FROM `gold_encounter` WHERE `total_cost` > 100 LIMIT 100",
        "expect_row_count": 2,
    },
    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------
    {
        "id": "sum_total_cost",
        "question": "What is the total cost of all encounters?",
        "sql": "SELECT SUM(`total_cost`) AS total FROM `gold_encounter` LIMIT 100",
        "expect_row_count": 1,
        "expect_value": {"total": 425.0},
    },
    {
        "id": "avg_total_cost",
        "question": "What is the average encounter cost?",
        "sql": "SELECT AVG(`total_cost`) AS avg_cost FROM `gold_encounter` LIMIT 100",
        "expect_row_count": 1,
    },
    {
        "id": "count_encounters_per_patient",
        "question": "How many encounters does each patient have?",
        "sql": (
            "SELECT `patient`, COUNT(*) AS encounter_count"
            " FROM `gold_encounter`"
            " GROUP BY `patient`"
            " LIMIT 100"
        ),
        "expect_row_count": 2,  # P001 and P002 have encounters
    },
    {
        "id": "count_patients_by_gender",
        "question": "How many patients are there by gender?",
        "sql": (
            "SELECT `gender`, COUNT(*) AS count"
            " FROM `gold_patient`"
            " GROUP BY `gender`"
            " ORDER BY `gender`"
            " LIMIT 100"
        ),
        "expect_row_count": 2,  # Female=2, Male=1
    },
    # ------------------------------------------------------------------
    # JOIN queries
    # ------------------------------------------------------------------
    {
        "id": "join_patient_name_with_encounters",
        "question": "List patient names with their encounter dates.",
        "sql": (
            "SELECT `gold_patient`.`first_name`, `gold_encounter`.`encounter_date`"
            " FROM `gold_encounter`"
            " JOIN `gold_patient` ON `gold_encounter`.`patient` = `gold_patient`.`id`"
            " LIMIT 100"
        ),
        "expect_row_count": 3,
    },
    {
        "id": "join_with_cost_filter",
        "question": "Which patients have had encounters costing more than 100?",
        "sql": (
            "SELECT DISTINCT `gold_patient`.`first_name`"
            " FROM `gold_encounter`"
            " JOIN `gold_patient` ON `gold_encounter`.`patient` = `gold_patient`.`id`"
            " WHERE `gold_encounter`.`total_cost` > 100"
            " LIMIT 100"
        ),
        "expect_row_count": 1,  # Only Alice (P001) has encounters > 100
    },
    {
        "id": "join_sum_cost_per_patient",
        "question": "What is the total encounter cost per patient?",
        "sql": (
            "SELECT `gold_patient`.`first_name`,"
            " SUM(`gold_encounter`.`total_cost`) AS total_cost"
            " FROM `gold_encounter`"
            " JOIN `gold_patient` ON `gold_encounter`.`patient` = `gold_patient`.`id`"
            " GROUP BY `gold_patient`.`id`, `gold_patient`.`first_name`"
            " LIMIT 100"
        ),
        "expect_row_count": 2,
    },
    # ------------------------------------------------------------------
    # ORDER BY / TOP-N
    # ------------------------------------------------------------------
    {
        "id": "top_encounter_by_cost",
        "question": "What is the most expensive encounter?",
        "sql": (
            "SELECT `id`, `total_cost`"
            " FROM `gold_encounter`"
            " ORDER BY `total_cost` DESC"
            " LIMIT 1"
        ),
        "expect_row_count": 1,
        "expect_value": {"id": "E002"},
    },
    {
        "id": "patients_sorted_by_dob",
        "question": "List patients sorted by date of birth (oldest first).",
        "sql": (
            "SELECT `id`, `first_name`, `date_of_birth`"
            " FROM `gold_patient`"
            " ORDER BY `date_of_birth` ASC"
            " LIMIT 100"
        ),
        "expect_row_count": 3,
        "expect_value": {"first_name": "Bob"},  # oldest patient is first
    },
]


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", SQL_CASES, ids=[c["id"] for c in SQL_CASES])
def test_sql_query(case, gold_engine):
    """Execute the SQL directly against gold SQLite and assert expected results.

    This harness verifies that the SQL the LLM is expected to generate is
    syntactically valid for SQLite and returns the correct results from known
    test data.  Add a new case whenever a production query failure is found.
    """
    with gold_engine.connect() as conn:
        result = conn.execute(text(case["sql"]))
        columns = list(result.keys())
        rows = [dict(zip(columns, row)) for row in result.fetchall()]

    assert len(rows) == case["expect_row_count"], (
        f"[{case['id']}] Expected {case['expect_row_count']} rows, got {len(rows)}.\n"
        f"SQL: {case['sql']}\nRows: {rows}"
    )

    if "expect_value" in case:
        first = rows[0]
        for col, expected in case["expect_value"].items():
            actual = first.get(col)
            # SQLite may return floats as float — coerce for comparison
            if isinstance(expected, (int, float)) and actual is not None:
                actual = type(expected)(actual)
            assert actual == expected, (
                f"[{case['id']}] Column '{col}': expected {expected!r}, got {actual!r}.\n"
                f"SQL: {case['sql']}\nRow: {first}"
            )
