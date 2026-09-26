# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from pydantic import BaseModel, Field
from typing import List, Any, Dict, Optional

class DataQualityWarning(BaseModel):
    category: str
    message: str
    severity: str
    sample_anomalies: List[Any] = Field(default_factory=list)

class ColumnProfile(BaseModel):
    name: str
    inferred_type: str
    is_nullable: bool
    is_primary_key: bool
    null_rate: float
    distinct_count: int
    top_values: List[Dict[str, Any]] = Field(default_factory=list)
    detected_formats: List[str] = Field(default_factory=list)
    sample_data: List[Any] = Field(default_factory=list)
    # Up to _FK_OVERLAP_SAMPLE *distinct* non-null values, for value-overlap tests
    # on higher-cardinality columns (terminology binding) where top_values is empty
    # and sample_data's first-N rows may not be distinct. Empty for PK columns.
    distinct_sample: List[Any] = Field(default_factory=list)
    quality_warnings: List[DataQualityWarning] = Field(default_factory=list)

class TableProfile(BaseModel):
    table_name: str
    row_count: int
    columns: Dict[str, ColumnProfile]
    foreign_keys: List[Dict[str, Any]] = Field(default_factory=list)
