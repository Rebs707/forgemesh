# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
MappingEvaluator — issue #48 (Mapping Quality Evaluation).

Compares a SemanticMappingReport against a ground truth and returns precision,
recall, F1, and per-column details.

Ground-truth entries state what a column *means* (``refers_to`` a table, or holds a
``value_type``), not what the ontology should name it. Each confirmed mapping is
scored against the ontology the report was built with:

  - the mapped property's domain is the column's table class (or a superclass);
  - a reference is an ObjectProperty whose range is the referenced table's class;
  - a value has a matching kind: an XSD range in the right family, or, for
    categorical values, an ObjectProperty to a value class (not an entity).

Table classes come from the report itself (highest-confidence class candidate, the
same rule the gold builder uses), so nothing here depends on naming conventions.
Entries with only a legacy ``expected_property_uri`` are scored by name.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from semantic_mapper.eval.models import (
    ColumnEvalResult,
    GroundTruth,
    GroundTruthEntry,
    MappingQualityReport,
)
from semantic_mapper.ontology.iterative.property_namer import canonical_local
from semantic_mapper.schema.mapping_output import MappingStatus, SemanticMappingReport

_XSD = "http://www.w3.org/2001/XMLSchema#"
_RDFS_LITERAL = "http://www.w3.org/2000/01/rdf-schema#Literal"
_TEMPORAL = {"date", "dateTime", "dateTimeStamp", "time", "gYear", "gYearMonth"}
_INTEGER = {"integer", "int", "long", "short", "nonNegativeInteger", "positiveInteger",
            "unsignedInt", "unsignedLong"}
_NUMERIC = _INTEGER | {"decimal", "double", "float"}
_TEXT = {"string", "normalizedString", "token", "anyURI"}
_DATATYPE_FAMILIES = {
    "temporal": _TEMPORAL,
    "numeric": _NUMERIC,
    "boolean": {"boolean"} | _INTEGER,   # 0/1 flags are commonly integers
    "text": _TEXT,
    "identifier": _NUMERIC | _TEXT,
    "categorical": _TEXT,                # a plain coded string is also acceptable
}


def _normalize_uri(uri: str) -> str:
    """Extract local name and canonicalize it so case variants compare equal."""
    local = uri.split("#")[-1].split("/")[-1]
    return canonical_local(local).lower()


def _local(uri: str) -> str:
    return uri.split("#")[-1].split("/")[-1]


def load_report_ontology(report: SemanticMappingReport, base_dir: Optional[Path] = None):
    """Load the ontology a mapping report was built with, or None if unavailable."""
    path_str = getattr(report, "ontology_uri", "") or ""
    if not path_str:
        return None
    path = Path(path_str)
    if not path.is_absolute():
        base = base_dir or Path(__file__).resolve().parents[2]
        path = base / path
    if not path.exists():
        return None
    import rdflib
    g = rdflib.Graph()
    g.parse(str(path))
    return g


class _OntologyView:
    """The handful of OWL facts the semantic checks need."""

    def __init__(self, graph, table_classes: Dict[str, str]) -> None:
        from rdflib import OWL, RDF, RDFS, URIRef
        self._g, self._RDFS, self._URIRef = graph, RDFS, URIRef
        self._obj = {str(s) for s in graph.subjects(RDF.type, OWL.ObjectProperty)}
        self._dt = {str(s) for s in graph.subjects(RDF.type, OWL.DatatypeProperty)}
        self.table_classes = table_classes
        self.entity_classes = set(table_classes.values())

    def _objs(self, uri: str, pred) -> Set[str]:
        return {str(o) for o in self._g.objects(self._URIRef(uri), pred)}

    def kind(self, prop: str) -> str:
        if prop in self._obj:
            return "object"
        if prop in self._dt:
            return "datatype"
        return "unknown"

    def domains(self, prop: str) -> Set[str]:
        return self._objs(prop, self._RDFS.domain)

    def ranges(self, prop: str) -> Set[str]:
        return self._objs(prop, self._RDFS.range)

    def is_a(self, cls: str, ancestor: str) -> bool:
        """cls is ancestor, or a (transitive) rdfs:subClassOf it."""
        seen, todo = set(), [cls]
        while todo:
            c = todo.pop()
            if c == ancestor:
                return True
            if c in seen:
                continue
            seen.add(c)
            todo.extend(self._objs(c, self._RDFS.subClassOf))
        return False


def _table_classes(report: SemanticMappingReport) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for tm in report.tables:
        if tm.class_candidate_set:
            out[tm.source_table] = max(tm.class_candidate_set, key=lambda c: c.confidence_score).uri
    return out


def _check_semantic(entry: GroundTruthEntry, prop: str, onto: _OntologyView) -> Tuple[bool, Optional[str]]:
    table_cls = onto.table_classes.get(entry.table)
    if table_cls is None:
        return False, f"no class for table {entry.table} in the report"
    kind = onto.kind(prop)
    if kind == "unknown":
        return False, f"{_local(prop)} is not declared in the ontology"
    domains = onto.domains(prop)
    if not any(onto.is_a(table_cls, d) for d in domains):
        return False, (f"{_local(prop)} has domain {sorted(_local(d) for d in domains) or 'none'}, "
                       f"not the {entry.table} class {_local(table_cls)}")
    ranges = onto.ranges(prop)

    if entry.refers_to:
        target = onto.table_classes.get(entry.refers_to)
        if target is None:
            return False, f"no class for referenced table {entry.refers_to} in the report"
        if kind != "object":
            return False, f"{_local(prop)} is a literal, not a link to {_local(target)}"
        if not any(onto.is_a(r, target) for r in ranges):
            return False, (f"{_local(prop)} links to {sorted(_local(r) for r in ranges) or 'nothing'}, "
                           f"not {_local(target)}")
        return True, None

    kinds = [entry.value_type] if isinstance(entry.value_type, str) else list(entry.value_type or [])
    for vt in kinds:
        if vt == "categorical" and kind == "object":
            if ranges and not (ranges & onto.entity_classes):
                return True, None   # linked to a value class, not another entity
            continue
        family = _DATATYPE_FAMILIES.get(vt, set())
        if kind == "datatype" and any(
            r.startswith(_XSD) and r[len(_XSD):] in family or (vt in ("text", "categorical") and r == _RDFS_LITERAL)
            for r in ranges
        ):
            return True, None
    shape = "link" if kind == "object" else "literal"
    return False, (f"{_local(prop)} is a {shape} with range {sorted(_local(r) for r in ranges) or 'none'}, "
                   f"expected {entry.expectation()}")


class MappingEvaluator:
    """Compare a mapping report against a ground-truth definition."""

    def evaluate(
        self,
        report: SemanticMappingReport,
        ground_truth: GroundTruth,
        ground_truth_path: str = "",
        llm_cost_usd: Optional[float] = None,
        ontology_graph=None,
    ) -> MappingQualityReport:
        """Score *report*. Semantic entries are checked against *ontology_graph*, or
        the ontology named in the report when no graph is given."""
        # Build lookup: (table, column) → PropertyMapping
        actual: dict = {}
        for t in report.tables:
            for c in t.column_mappings:
                actual[(t.source_table, c.source_column)] = c

        onto: Optional[_OntologyView] = None
        if any(e.refers_to or e.value_type for e in ground_truth.mappings):
            graph = ontology_graph if ontology_graph is not None else load_report_ontology(report)
            if graph is not None:
                onto = _OntologyView(graph, _table_classes(report))

        per_column: List[ColumnEvalResult] = []
        tp = fp = fn = pending_count = unmapped_count = 0

        for entry in ground_truth.mappings:
            col = actual.get((entry.table, entry.column))
            actual_uri = col.mapped_property_uri if col else None
            actual_status = col.status.value if col else "unmapped"

            confirmed = col is not None and col.status == MappingStatus.CONFIRMED
            correct, reason = False, None
            if not confirmed or not actual_uri:
                reason = f"mapping is {actual_status}"
            elif entry.refers_to or entry.value_type:
                if onto is None:
                    reason = "no ontology available to check the meaning against"
                else:
                    correct, reason = _check_semantic(entry, actual_uri, onto)
            elif entry.expected_property_uri:
                correct = _normalize_uri(actual_uri) == _normalize_uri(entry.expected_property_uri)
                if not correct:
                    reason = f"name {_local(actual_uri)} != {_local(entry.expected_property_uri)}"
            else:
                reason = "ground-truth entry states no expectation"

            if correct:
                tp += 1
            else:
                fn += 1          # GT entry not correctly answered
                if confirmed:
                    fp += 1      # a wrong property was confirmed for this entry

            if actual_status == MappingStatus.PENDING_REVIEW.value:
                pending_count += 1
            elif actual_status == MappingStatus.UNMAPPED.value:
                unmapped_count += 1

            per_column.append(ColumnEvalResult(
                table=entry.table,
                column=entry.column,
                expected_uri=entry.expectation(),
                actual_uri=actual_uri,
                actual_status=actual_status,
                correct=correct,
                reason=reason,
            ))

        total_gt = len(ground_truth.mappings)
        total_confirmed = tp + fp
        precision = tp / total_confirmed if total_confirmed > 0 else 0.0
        recall = tp / total_gt if total_gt > 0 else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0.0
        )

        return MappingQualityReport(
            ground_truth_path=ground_truth_path,
            report_generated_at=datetime.now(timezone.utc).isoformat(),
            dataset=ground_truth.dataset,
            total_ground_truth_entries=total_gt,
            true_positives=tp,
            false_positives=fp,
            false_negatives=fn,
            precision=round(precision, 4),
            recall=round(recall, 4),
            f1=round(f1, 4),
            pending_rate=round(pending_count / total_gt, 4) if total_gt > 0 else 0.0,
            unmapped_rate=round(unmapped_count / total_gt, 4) if total_gt > 0 else 0.0,
            llm_cost_usd=llm_cost_usd,
            llm_cost_per_mapping=(
                round(llm_cost_usd / tp, 6) if llm_cost_usd and tp > 0 else None
            ),
            per_column=per_column,
        )
