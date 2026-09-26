# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""TerminologySource — the adapter contract for an external code system."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional


class TerminologySource(ABC):
    """One external code system, exposed as a value membership test plus a
    concept-IRI minting rule.

    A source is reference data, not pipeline logic: detection asks every
    configured source whether it ``contains`` a value, and (for matches) what
    its external ``concept_iri`` is.  Concrete adapters back this with an offline
    code/label set, a bundled file, or a live service.

    Attributes
    ----------
    source_id:
        Stable identifier used as the ``TERMINOLOGY_SOURCES`` key, e.g.
        ``"snomedct"``.  Distinguishes one configured source from another.
    system_label:
        Human-readable code-system name, e.g. ``"SNOMED CT"``.  This is the
        literal written to ``fm:terminologySystem`` on each bound individual.
    """

    source_id: str
    system_label: str

    @abstractmethod
    def contains(self, value: str) -> bool:
        """True if *value* is a valid code (or label) in this code system.

        Implementations must be case- and whitespace-tolerant where the code
        system is; ``value`` is the raw source value (e.g. a ``rdfs:label`` on a
        minted individual).
        """
        ...

    @abstractmethod
    def concept_iri(self, value: str) -> Optional[str]:
        """Return the external concept IRI for *value*, or ``None`` if the value
        is not in this code system.

        A source that maps one value to several concepts (an ambiguous concept
        map) returns its single best IRI here and surfaces the alternatives via
        :meth:`concept_iris`.
        """
        ...

    def concept_iris(self, value: str) -> List[str]:
        """All candidate concept IRIs for *value* (ordered, best first).

        Default: the single :meth:`concept_iri`, or ``[]``.  Override for a code
        system with genuine one-to-many concept maps — the extra candidates are
        surfaced for review, never auto-resolved (plan KNOWN-PENDING).
        """
        iri = self.concept_iri(value)
        return [iri] if iri else []

    def system_iri(self) -> Optional[str]:
        """The IRI naming the code system itself (e.g. ``http://snomed.info/sct``),
        used for system-level ``rdfs:seeAlso`` / ``skos:closeMatch`` on a bound
        property or class.  ``None`` if the source does not name a system IRI.
        """
        return None
