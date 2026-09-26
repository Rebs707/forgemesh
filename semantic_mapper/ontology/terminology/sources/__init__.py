# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Pluggable terminology sources (reference data / service adapters).

A :class:`TerminologySource` exposes a code/label set and a concept-IRI minting
rule.  Sources are reference data configured generically in
``settings.TERMINOLOGY_SOURCES`` — they are NOT dataset-specific branches.
Adding a code system is adding an adapter, not editing pipeline logic.
"""
from .base import TerminologySource
from .offline_codeset import OfflineCodesetSource
from .registry import build_sources, register_adapter

__all__ = [
    "TerminologySource",
    "OfflineCodesetSource",
    "build_sources",
    "register_adapter",
]
