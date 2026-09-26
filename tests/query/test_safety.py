# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from semantic_mapper.core.exceptions import QuerySafetyError
from semantic_mapper.query.safety import validate_select_only


class TestValidateSelectOnly:
    # --- Happy path ---

    def test_plain_select_passes(self):
        validate_select_only("SELECT * FROM silver_raw_party LIMIT 10")

    def test_select_with_where_passes(self):
        validate_select_only(
            "SELECT id FROM silver_raw_party WHERE party_type = 1 LIMIT 10"
        )

    def test_select_with_subquery_passes(self):
        validate_select_only(
            "SELECT * FROM (SELECT id FROM silver_raw_party) AS sub LIMIT 10"
        )

    def test_select_case_insensitive_passes(self):
        validate_select_only("select count(*) from silver_raw_party limit 10")

    # --- DML / DDL --- each must raise

    def test_insert_raises(self):
        with pytest.raises(QuerySafetyError):
            validate_select_only("INSERT INTO foo VALUES (1)")

    def test_update_raises(self):
        with pytest.raises(QuerySafetyError):
            validate_select_only("UPDATE foo SET bar=1")

    def test_delete_raises(self):
        with pytest.raises(QuerySafetyError):
            validate_select_only("DELETE FROM foo")

    def test_drop_raises(self):
        with pytest.raises(QuerySafetyError):
            validate_select_only("DROP TABLE foo")

    def test_create_raises(self):
        with pytest.raises(QuerySafetyError):
            validate_select_only("CREATE TABLE foo (id INT)")

    def test_alter_raises(self):
        with pytest.raises(QuerySafetyError):
            validate_select_only("ALTER TABLE foo ADD COLUMN bar INT")

    def test_truncate_raises(self):
        with pytest.raises(QuerySafetyError):
            validate_select_only("TRUNCATE TABLE foo")

    # --- Comment stripping ---

    def test_line_comment_before_select_stripped(self):
        # Comment is stripped so bare SELECT remains
        validate_select_only("-- comment\nSELECT * FROM foo LIMIT 10")

    def test_insert_hidden_in_line_comment_is_still_allowed(self):
        # -- INSERT in a comment must not trigger the banned-keyword check
        validate_select_only("SELECT * FROM foo -- INSERT hack\n LIMIT 10")

    def test_block_comment_stripped(self):
        validate_select_only("/* comment */ SELECT * FROM foo LIMIT 10")

    def test_insert_in_block_comment_allowed(self):
        validate_select_only("SELECT * FROM foo /* INSERT ignored */ LIMIT 10")

    # --- Banned keyword in query body ---

    def test_select_then_drop_in_body_raises(self):
        # Subquery trick: SELECT ... ; DROP TABLE
        with pytest.raises(QuerySafetyError):
            validate_select_only("SELECT 1; DROP TABLE foo")

    def test_exec_raises(self):
        with pytest.raises(QuerySafetyError):
            validate_select_only("SELECT 1; EXEC xp_cmdshell('ls')")

    def test_not_starting_with_select_raises(self):
        with pytest.raises(QuerySafetyError):
            validate_select_only("SHOW TABLES")
