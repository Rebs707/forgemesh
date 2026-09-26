# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""External terminology binding.

Attaches value-mapped vocabulary individuals (and, where confident, their owning
class/property) to authoritative external concepts — SNOMED CT, ICD, LOINC, FHIR
resource types, OMOP concepts — via ``skos:exactMatch``, carrying provenance.

Detection keys ONLY on value-set overlap: the fraction of a column's distinct
non-null values that are valid codes/labels in a configured terminology source.
There is no column-name heuristic and no dataset branch — a column whose values
match no source binds to nothing.  See CONTRIBUTING.md, "No Dataset-Specific Knowledge".

Public entry point: :func:`binder.bind_terminology`, hooked into the iterative
generator AFTER ``materialise_categorical_individuals`` and BEFORE ``owl:oneOf``
enumeration.
"""
from .binder import bind_terminology
from .models import BindingResult, DetectedColumn, ProposedBinding

__all__ = [
    "bind_terminology",
    "BindingResult",
    "DetectedColumn",
    "ProposedBinding",
]
