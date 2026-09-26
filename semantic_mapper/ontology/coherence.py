# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
OntologyCoherenceChecker — issue #52 (Ontology Coherence Check).

Runs lightweight structural checks against a loaded rdflib.Graph without
requiring a Java DL reasoner.  Checks performed:

  MISSING_DOMAIN          — data/object property has no rdfs:domain
  MISSING_RANGE           — data/object property has no rdfs:range
  NAMESPACE_COLLISION     — same local name used by multiple URIs
  ORPHAN_CLASS            — named class with no properties declaring it as domain
  INVERSE_ASYMMETRY       — P owl:inverseOf Q, but Q has no owl:inverseOf P
  SOURCE_COLUMN_COLLISION — same (table, col) annotated as both FK source
                            (fm:sourceForeignKey on an ObjectProperty) and
                            scalar source (fm:sourceColumn on a DatatypeProperty)
  MIXED_RANGE             — ObjectProperty has both a class URI range and XSD range(s)
                            (unsatisfiable: property cannot be both object and data)
  WRONG_RANGE_TYPE        — ObjectProperty has only XSD range(s), no class URI range
                            (property typed as ObjectProperty but range is literal-typed)
  MULTI_RANGE             — ObjectProperty has more than one class URI range, no XSD
                            (implicit intersection — almost always a generation error)
  DANGLING_RANGE          — ObjectProperty range URI is not a declared owl:Class and not
                            an XSD type (likely a name-derivation mismatch)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import rdflib
from rdflib.namespace import OWL, RDF, RDFS

FM = rdflib.Namespace("http://forgemesh.ai/ontology#")

logger = logging.getLogger(__name__)


@dataclass
class CoherenceIssue:
    severity: str         # "error" | "warning"
    category: str         # one of the check names above
    message: str
    subject_uri: Optional[str] = None


@dataclass
class CoherenceReport:
    issues: List[CoherenceIssue] = field(default_factory=list)
    classes_checked: int = 0
    properties_checked: int = 0

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    @property
    def is_coherent(self) -> bool:
        return self.error_count == 0

    def to_dict(self) -> dict:
        return {
            "is_coherent": self.is_coherent,
            "classes_checked": self.classes_checked,
            "properties_checked": self.properties_checked,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "issues": [
                {
                    "severity": i.severity,
                    "category": i.category,
                    "message": i.message,
                    "subject_uri": i.subject_uri,
                }
                for i in self.issues
            ],
        }


class OntologyCoherenceChecker:
    """Perform structural coherence checks on an rdflib.Graph."""

    _OWL_THING = "http://www.w3.org/2002/07/owl#Thing"
    _OWL_NOTHING = "http://www.w3.org/2002/07/owl#Nothing"

    def __init__(self, graph: rdflib.Graph):
        self._g = graph

    def check(self) -> CoherenceReport:
        report = CoherenceReport()
        classes = self._get_classes()
        props = self._get_properties()
        report.classes_checked = len(classes)
        report.properties_checked = len(props)

        self._check_missing_domains(report, props)
        self._check_missing_ranges(report, props)
        self._check_namespace_collisions(report, classes, props)
        self._check_orphan_classes(report, classes)
        self._check_inverse_asymmetry(report, props)
        self._check_source_column_collisions(report)
        self._check_range_coherence(report, props, classes)

        return report

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_classes(self) -> List[rdflib.URIRef]:
        return [
            s for s in self._g.subjects(RDF.type, OWL.Class)
            if isinstance(s, rdflib.URIRef)
            and str(s) not in (self._OWL_THING, self._OWL_NOTHING)
        ]

    def _get_properties(self) -> List[rdflib.URIRef]:
        props: Set[rdflib.URIRef] = set()
        for prop_type in (OWL.DatatypeProperty, OWL.ObjectProperty):
            for s in self._g.subjects(RDF.type, prop_type):
                if isinstance(s, rdflib.URIRef):
                    props.add(s)
        return list(props)

    @staticmethod
    def _local_name(uri: rdflib.URIRef) -> str:
        s = str(uri)
        return s.split("#")[-1] if "#" in s else s.rsplit("/", 1)[-1]

    # ── Checks ────────────────────────────────────────────────────────────────

    def _check_missing_domains(
        self, report: CoherenceReport, props: List[rdflib.URIRef]
    ) -> None:
        for p in props:
            if not list(self._g.objects(p, RDFS.domain)):
                report.issues.append(CoherenceIssue(
                    severity="warning",
                    category="MISSING_DOMAIN",
                    message=f"Property has no rdfs:domain declaration",
                    subject_uri=str(p),
                ))

    def _check_missing_ranges(
        self, report: CoherenceReport, props: List[rdflib.URIRef]
    ) -> None:
        for p in props:
            if not list(self._g.objects(p, RDFS.range)):
                report.issues.append(CoherenceIssue(
                    severity="warning",
                    category="MISSING_RANGE",
                    message="Property has no rdfs:range declaration",
                    subject_uri=str(p),
                ))

    def _check_namespace_collisions(
        self,
        report: CoherenceReport,
        classes: List[rdflib.URIRef],
        props: List[rdflib.URIRef],
    ) -> None:
        local_map: Dict[str, List[str]] = {}
        for uri in classes + props:
            local = self._local_name(uri)
            local_map.setdefault(local, []).append(str(uri))
        for local, uris in local_map.items():
            if len(uris) > 1:
                report.issues.append(CoherenceIssue(
                    severity="warning",
                    category="NAMESPACE_COLLISION",
                    message=(
                        f"Local name '{local}' shared by {len(uris)} URIs: "
                        + ", ".join(uris)
                    ),
                    subject_uri=local,
                ))

    def _check_orphan_classes(
        self, report: CoherenceReport, classes: List[rdflib.URIRef]
    ) -> None:
        """Named classes with no property declaring them as domain."""
        classes_as_domain: Set[str] = {
            str(obj)
            for _, _, obj in self._g.triples((None, RDFS.domain, None))
        }
        for cls in classes:
            if str(cls) not in classes_as_domain:
                report.issues.append(CoherenceIssue(
                    severity="warning",
                    category="ORPHAN_CLASS",
                    message="Class has no properties declaring it as rdfs:domain",
                    subject_uri=str(cls),
                ))

    def _check_inverse_asymmetry(
        self, report: CoherenceReport, props: List[rdflib.URIRef]
    ) -> None:
        """If P owl:inverseOf Q, then Q should also declare owl:inverseOf P."""
        for p in props:
            for q in self._g.objects(p, OWL.inverseOf):
                if not isinstance(q, rdflib.URIRef):
                    continue
                if not list(self._g.objects(q, OWL.inverseOf)):
                    report.issues.append(CoherenceIssue(
                        severity="warning",
                        category="INVERSE_ASYMMETRY",
                        message=(
                            f"Declares owl:inverseOf {str(q)}, "
                            f"but {str(q)} has no inverseOf declaration"
                        ),
                        subject_uri=str(p),
                    ))

    def _check_source_column_collisions(self, report: CoherenceReport) -> None:
        """SOURCE_COLUMN_COLLISION — same (table, col) is both an FK source and a scalar source.

        Detects when fm:sourceColumn on a DatatypeProperty and fm:sourceForeignKey on an
        ObjectProperty both point at the same source column.  The pipeline cannot determine
        whether the column is a literal or a foreign key; the collision is surfaced as an
        error so the affected mapping is flagged PENDING_REVIEW rather than silently resolved
        by strategy ordering.
        """
        dt_sources: Dict[Tuple[str, str], str] = {}
        for prop, _, val in self._g.triples((None, FM.sourceColumn, None)):
            if not isinstance(prop, rdflib.URIRef):
                continue
            s = str(val)
            if "." not in s:
                continue
            table, col = s.split(".", 1)
            dt_sources[(table.strip(), col.strip())] = str(prop)

        fk_sources: Dict[Tuple[str, str], str] = {}
        for prop, _, val in self._g.triples((None, FM.sourceForeignKey, None)):
            if not isinstance(prop, rdflib.URIRef):
                continue
            s = str(val)
            if "." not in s or "->" not in s:
                continue
            left = s.split("->", 1)[0].strip()
            if "." not in left:
                continue
            table, col = left.split(".", 1)
            fk_sources[(table.strip(), col.strip())] = str(prop)

        for key in sorted(set(dt_sources) & set(fk_sources)):
            table, col = key
            report.issues.append(CoherenceIssue(
                severity="error",
                category="SOURCE_COLUMN_COLLISION",
                message=(
                    f"source column '{table}.{col}' is annotated on both "
                    f"DatatypeProperty <{dt_sources[key]}> (fm:sourceColumn) and "
                    f"ObjectProperty <{fk_sources[key]}> (fm:sourceForeignKey) — "
                    "cannot determine whether this column is a literal or a foreign key"
                ),
                subject_uri=f"{table}.{col}",
            ))

    def _check_range_coherence(
        self,
        report: CoherenceReport,
        props: List[rdflib.URIRef],
        classes: List[rdflib.URIRef],
    ) -> None:
        """Four-way ObjectProperty range check (per property, not per range triple).

        After filtering owl:Thing, ranges are bucketed into:
          class_ranges  — URIs in the declared owl:Class set
          xsd_ranges    — URIs in the XSD namespace
          dangling      — everything else (undeclared, non-XSD)

        Classification (DANGLING_RANGE is additive):
          class_ranges AND xsd_ranges → MIXED_RANGE (error): unsatisfiable
          xsd_ranges only, no class   → WRONG_RANGE_TYPE (error): typed wrong
          len(class_ranges) > 1       → MULTI_RANGE (warning): implicit intersection
          dangling non-empty          → DANGLING_RANGE (error): one per undeclared URI;
                                        fires even when MIXED_RANGE also fires
        """
        _XSD = "http://www.w3.org/2001/XMLSchema#"
        declared_class_uris: Set[str] = {str(c) for c in classes}
        declared_class_uris.add(self._OWL_THING)

        for p in props:
            prop_types = {str(o) for o in self._g.objects(p, RDF.type)}
            if str(OWL.ObjectProperty) not in prop_types:
                continue

            class_ranges: List[str] = []
            xsd_ranges: List[str] = []
            dangling: List[str] = []

            for rng in self._g.objects(p, RDFS.range):
                if not isinstance(rng, rdflib.URIRef):
                    continue
                rng_str = str(rng)
                if rng_str == self._OWL_THING:
                    continue  # always exempt
                if rng_str in declared_class_uris:
                    class_ranges.append(rng_str)
                elif rng_str.startswith(_XSD):
                    xsd_ranges.append(rng_str)
                else:
                    dangling.append(rng_str)

            prop_local = self._local_name(p)

            if class_ranges and xsd_ranges:
                report.issues.append(CoherenceIssue(
                    severity="error",
                    category="MIXED_RANGE",
                    message=(
                        f"ObjectProperty has both class range(s) {class_ranges} "
                        f"and XSD range(s) {xsd_ranges} — unsatisfiable"
                    ),
                    subject_uri=str(p),
                ))
            elif xsd_ranges and not class_ranges:
                report.issues.append(CoherenceIssue(
                    severity="error",
                    category="WRONG_RANGE_TYPE",
                    message=(
                        f"ObjectProperty has only XSD range(s) {xsd_ranges} "
                        f"— property is typed as ObjectProperty but range is literal-typed"
                    ),
                    subject_uri=str(p),
                ))
            elif len(class_ranges) > 1 and not xsd_ranges and not dangling:
                report.issues.append(CoherenceIssue(
                    severity="warning",
                    category="MULTI_RANGE",
                    message=(
                        f"ObjectProperty declares {len(class_ranges)} class ranges "
                        f"(implicit intersection — likely a generation error): {class_ranges}"
                    ),
                    subject_uri=str(p),
                ))

            for rng_str in dangling:
                report.issues.append(CoherenceIssue(
                    severity="error",
                    category="DANGLING_RANGE",
                    message=(
                        f"ObjectProperty range <{rng_str}> is not a declared owl:Class "
                        f"— likely a name-derivation mismatch"
                    ),
                    subject_uri=str(p),
                ))


# ---------------------------------------------------------------------------
# Resolution override helpers
# ---------------------------------------------------------------------------

def load_coherence_overrides(path: Path) -> List[Dict]:
    """Load resolution override sidecar. Returns [] if file is absent or malformed."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def apply_coherence_overrides(graph: rdflib.Graph, overrides: List[Dict]) -> int:
    """Remove the conflicting annotation for each resolved override.

    Each override: ``{"table": str, "column": str, "keep": "fk" | "literal"}``

    ``keep="fk"``      — strip ``fm:sourceColumn`` from the DatatypeProperty annotated
                         ``"table.column"``, leaving only the FK (ObjectProperty) side.
    ``keep="literal"`` — strip ``fm:sourceForeignKey`` from the ObjectProperty whose
                         annotation starts with ``"table.column ->"``, leaving only the
                         scalar (DatatypeProperty) side.

    Returns the number of annotation triples removed.
    """
    removed = 0
    for ov in overrides:
        table = ov.get("table", "")
        column = ov.get("column", "")
        keep = ov.get("keep", "")
        if not (table and column and keep in ("fk", "literal")):
            continue
        if keep == "fk":
            target = f"{table}.{column}"
            for prop, _, val in list(graph.triples((None, FM.sourceColumn, None))):
                if str(val) == target:
                    graph.remove((prop, FM.sourceColumn, val))
                    removed += 1
                    logger.debug(
                        "coherence override: stripped fm:sourceColumn '%s' from <%s>",
                        target, prop,
                    )
        else:
            prefix = f"{table}.{column} ->"
            for prop, _, val in list(graph.triples((None, FM.sourceForeignKey, None))):
                if str(val).startswith(prefix):
                    graph.remove((prop, FM.sourceForeignKey, val))
                    removed += 1
                    logger.debug(
                        "coherence override: stripped fm:sourceForeignKey '%s' from <%s>",
                        str(val), prop,
                    )
    return removed
