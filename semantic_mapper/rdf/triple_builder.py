# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef
from rdflib.namespace import SKOS, XSD

_FM = Namespace("http://forgemesh.ai/ontology#")
_FM_ENTITY_ID = _FM.entityIdentifierColumn
# Two distinct quality predicates.
# fm:incompleteReference — a FK column has a null value; the reference is missing.
# fm:invalidMapping     — a value_map (or pre-materialised URI) points at the
#                         property's rdfs:range *class* instead of an individual of
#                         that class.  This is a mapping configuration error, not a
#                         missing value, and must be tracked separately from null FKs
#                         so that the C5 incompleteness count stays precise.
_FM_INVALID_MAPPING = _FM.invalidMapping

from semantic_mapper.gold.models import GoldTableDefinition
from semantic_mapper.rdf.uri_factory import UriFactory
from semantic_mapper.rdf.nested_expansion import explode_nested_cell
from semantic_mapper.ontology.iterative.categorical_individuals import _value_slug

_DATE_RE     = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Match both ISO 8601 (T-separated, from Python/RDF) and MySQL format (space-separated).
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
# UUID format: 8-4-4-4-12 hex groups (case-insensitive).
# Used to detect FK columns in UUID-keyed schemas (BUG-01): a column that
# matches an entity name but holds UUID values is carrying the entity's PK,
# not a human-readable alternative identifier (IATA code, ISO code, etc.).
_UUID_RE     = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_INTEGER_RE  = re.compile(r"^-?\d+$")
_DECIMAL_RE  = re.compile(r"^-?\d+\.\d+$")

_STRING_SQL_TYPES = ("TEXT", "VARCHAR", "CHAR", "NVARCHAR", "NCHAR", "CLOB", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT")

# Matches MySQL datetime format "YYYY-MM-DD HH:MM:SS" (space separator).
_MYSQL_DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})")


def _normalise_literal(value: Any, datatype: URIRef) -> Any:
    """Normalise a value before creating an RDF Literal.

    Converts MySQL datetime strings ("YYYY-MM-DD HH:MM:SS") to ISO 8601
    ("YYYY-MM-DDTHH:MM:SS") when the datatype is xsd:dateTime.  Without this,
    Fuseki stores the literal but SPARQL range filters against properly-typed
    xsd:dateTime values silently return nothing.
    """
    if datatype == XSD.dateTime and isinstance(value, str):
        m = _MYSQL_DATETIME_RE.match(value)
        if m:
            return f"{m.group(1)}T{m.group(2)}"
    return value


def _xsd_type(source_data_type: str, value: Any) -> URIRef:
    """Map a source SQL type (and optionally the value) to an XSD datatype URI.

    For declared numeric SQL types the datatype is taken directly from the column
    declaration.  For string types (TEXT, VARCHAR, …) value-based sniffing is
    applied so that numeric values stored in string columns (common in Synthea /
    CSV-imported data) receive a proper XSD numeric type rather than xsd:string,
    allowing SPARQL aggregates (SUM, AVG, etc.) to work correctly.
    """
    upper = source_data_type.upper()
    if any(upper.startswith(t) for t in ("INT", "BIGINT", "SMALLINT", "TINYINT")):
        return XSD.integer
    if any(upper.startswith(t) for t in ("DECIMAL", "NUMERIC")):
        return XSD.decimal
    if any(upper.startswith(t) for t in ("FLOAT", "DOUBLE")):
        return XSD.double
    if any(upper.startswith(t) for t in ("BOOLEAN", "BIT")):
        return XSD.boolean
    if upper.startswith("DATE") and not upper.startswith("DATETIME"):
        if value is not None and _DATE_RE.match(str(value)):
            return XSD.date
        return XSD.string
    if any(upper.startswith(t) for t in ("DATETIME", "TIMESTAMP")):
        if value is not None and _DATETIME_RE.match(str(value)):
            return XSD.dateTime
        return XSD.string

    # For string column types, sniff the value so numerics stored as TEXT
    # get a proper XSD type and remain usable in SPARQL aggregates.
    if any(upper.startswith(t) for t in _STRING_SQL_TYPES) and value is not None:
        s = str(value).strip()
        if _INTEGER_RE.match(s):
            return XSD.integer
        if _DECIMAL_RE.match(s):
            return XSD.decimal

    return XSD.string


def _build_prop_range(ontology_graph: Graph) -> Dict[str, str]:
    """Return {property_uri: range_class_uri} for all OWL object properties."""
    result: Dict[str, str] = {}
    for prop in ontology_graph.subjects(RDF.type, OWL.ObjectProperty):
        range_ = next(ontology_graph.objects(prop, RDFS.range), None)
        if range_:
            result[str(prop)] = str(range_)
    return result


def _build_coded_properties(ontology_graph: Graph) -> Dict[str, Tuple[str, float]]:
    """Return ``{object_property_uri: (system_label, confidence)}`` for terminology-
    coded properties — high-cardinality code columns the binder re-typed to
    ObjectProperty and flagged with ``fm:terminologySystem``.  Their code individuals
    and ``skos:exactMatch`` links are minted here, at materialisation, one per distinct
    code (the full code set is never needed at generation time).
    """
    result: Dict[str, Tuple[str, float]] = {}
    for prop, _, sys in ontology_graph.triples((None, _FM.terminologySystem, None)):
        if (prop, RDF.type, OWL.ObjectProperty) not in ontology_graph:
            continue  # the same annotation on a bound individual is provenance, not a flag
        conf = next(ontology_graph.objects(prop, _FM.terminologyBindingConfidence), None)
        try:
            confidence = float(conf) if conf is not None else 1.0
        except (TypeError, ValueError):
            confidence = 1.0
        result[str(prop)] = (str(sys), confidence)
    return result


def _build_class_uri_set(ontology_graph: Graph) -> "set[str]":
    """Return the set of all OWL class URIs declared in the ontology.

    Used to detect when a value_map entry (or a pre-materialised URI in a gold
    column) resolves to a CLASS instead of an individual of that class.  Emitting
    a class URI as an ObjectProperty value corrupts join queries and is caught
    here so the TripleBuilder can substitute fm:incompleteReference.
    """
    return {
        str(cls)
        for cls in ontology_graph.subjects(RDF.type, OWL.Class)
        if isinstance(cls, URIRef)
    }


def _build_superclass_map(ontology_graph: Graph) -> Dict[str, List[str]]:
    """Return {class_uri: [ALL ancestor class URIs, transitively]} for rdfs:subClassOf declarations.

    Transitive closure means every ancestor is included, not just direct parents. This
    ensures instances typed as deep subclasses still emit rdf:type for top-level classes
    (e.g. EmergencyAdmission → RawAdmission → Admission: all three are emitted).
    owl:Thing is excluded — it is implicit and adds noise.
    """
    direct: Dict[str, List[str]] = {}
    owl_thing = str(OWL.Thing)
    for child, _, parent in ontology_graph.triples((None, RDFS.subClassOf, None)):
        parent_str = str(parent)
        if parent_str == owl_thing:
            continue
        direct.setdefault(str(child), []).append(parent_str)

    memo: Dict[str, List[str]] = {}
    in_progress: set = set()

    def _ancestors(cls: str) -> List[str]:
        if cls in memo:
            return memo[cls]
        if cls in in_progress:
            return []  # cycle guard
        in_progress.add(cls)
        result: List[str] = []
        for parent in direct.get(cls, []):
            if parent not in result:
                result.append(parent)
            for anc in _ancestors(parent):
                if anc not in result:
                    result.append(anc)
        in_progress.discard(cls)
        memo[cls] = result
        return result

    return {cls: _ancestors(cls) for cls in direct}


class TripleBuilder:
    """Converts a gold row + its GoldTableDefinition into a list of RDF triples.

    When an ontology_graph is supplied, object-property direction is resolved
    using rdfs:range declarations.  If the current entity's class matches the
    declared range of a property, the triple is emitted with the FK target as
    subject — making e.g. ``patientHasEncounter`` correctly produce
    ``<patient> patientHasEncounter <encounter>`` even when processing an
    encounter row.  This generalises to any domain (dogHasFleas, etc.).
    """

    def __init__(
        self,
        definitions: List[GoldTableDefinition],
        uri_factory: UriFactory,
        ontology_graph: Optional[Graph] = None,
        nested_columns: Optional[Dict[str, Dict[str, dict]]] = None,
        reconcile_by_key: Optional[Dict[str, Dict[str, str]]] = None,
        terminology_sources: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._uri_factory = uri_factory
        # Nested/packed columns to explode at the graph boundary, keyed by
        # gold_table_name → {gold_column_name: term_names}. A column listed here
        # holds a stringified list of flat objects (a denormalised 1-to-N group);
        # build() explodes it into edges to first-class entities instead of
        # emitting the opaque text literal. See rdf/nested_expansion.py.
        self._nested: Dict[str, Dict[str, dict]] = nested_columns or {}
        # field name → {value: existing value-mapped individual URI}. Lets a reconciled
        # child-key (term_names["key_links"]) resolve its value to an existing vocabulary
        # individual at explosion time instead of emitting a duplicate literal.
        self._reconcile_by_key: Dict[str, Dict[str, str]] = reconcile_by_key or {}
        # entity_map: short name → gold table name for FK detection
        # e.g. "patient" → "gold_patient"
        self._entity_map: Dict[str, str] = {
            d.gold_table_name[len("gold_"):]: d.gold_table_name
            for d in definitions
            if d.gold_table_name.startswith("gold_")
        }
        # property URI → range class URI (from ontology)
        self._prop_range: Dict[str, str] = (
            _build_prop_range(ontology_graph) if ontology_graph is not None else {}
        )
        # class URI → [parent class URIs] for emitting superclass type assertions
        self._superclass_map: Dict[str, List[str]] = (
            _build_superclass_map(ontology_graph) if ontology_graph is not None else {}
        )
        # Reverse: primary_class_uri → entity short name.
        # Used when column-name FK detection fails but the property's rdfs:range
        # identifies the target entity — e.g. hasDiagnosesIcdAdmission has
        # range=Admission, so hadm_id values should become Admission URIs.
        self._class_to_entity: Dict[str, str] = {
            d.primary_class_uri: d.gold_table_name[len("gold_"):]
            for d in definitions
            if d.gold_table_name.startswith("gold_") and d.primary_class_uri
        }
        # Set of all OWL class URIs — used to detect value_map entries or
        # pre-materialised URIs that resolve to a class rather than an individual.
        # Emitting a class URI as an ObjectProperty value corrupts SPARQL joins;
        # fm:incompleteReference is emitted instead (FK-to-missing-class fallback).
        self._class_uris: "set[str]" = (
            _build_class_uri_set(ontology_graph)
            if ontology_graph is not None
            else set()
        )
        # Cross-namespace extension: value_map entries may reference class URIs from
        # a base domain ontology (e.g. OrgHealth.owl#Payer) whose namespace differs
        # from the rigor OWL (e.g. synthea#Payer).  Since both denote the same class,
        # any value_map URI whose local fragment (#Payer) matches a known class fragment
        # is also a class URI and must trigger fm:incompleteReference rather than being
        # emitted as an object value.  This scan runs at init time and is cheap.
        _class_fragments: "set[str]" = {
            uri.split("#")[-1] for uri in self._class_uris if "#" in uri
        }
        for _defn in definitions:
            for _spec in _defn.column_specs:
                for _v in (_spec.value_map or {}).values():
                    if _v and "#" in _v and _v not in self._class_uris:
                        _frag = _v.split("#")[-1]
                        if _frag in _class_fragments:
                            self._class_uris.add(_v)
        # entity short name → id_column (the gold column name used when minting
        # entity subject URIs).  Used to validate FK columns: only emit an object
        # property URI when the column actually carries the entity's PK value, not
        # an alternative identifier (e.g. IATA code "2B" vs numeric airline_id 410).
        # Entities whose id_column is None are omitted — the guard is skipped for them.
        self._entity_id_col: Dict[str, str] = {
            d.gold_table_name[len("gold_"):]: d.id_column
            for d in definitions
            if d.gold_table_name.startswith("gold_") and d.id_column
        }
        # OWL fm:entityIdentifierColumn annotation: class URI → source column name.
        # Used when _pick_id_column() returns None (e.g. MIMIC's cgid, itemid —
        # bare *id columns that the *_id heuristic cannot auto-detect).
        self._class_id_source_col: Dict[str, str] = {}
        if ontology_graph is not None:
            for cls, _, val in ontology_graph.triples((None, _FM_ENTITY_ID, None)):
                self._class_id_source_col[str(cls)] = str(val)

        # Terminology-coded object properties (option b): property URI →
        # (system_label, confidence).  For these, each cell value is a code resolved
        # to a conformed code individual whose skos:exactMatch is minted on first sight.
        self._coded_props: Dict[str, Tuple[str, float]] = (
            _build_coded_properties(ontology_graph) if ontology_graph is not None else {}
        )
        # system_label → TerminologySource, used to validate codes and mint concept IRIs.
        self._term_sources: Dict[str, Any] = terminology_sources or {}
        # Distinct code individuals already fully described this run (dedup the binding).
        self._minted_codes: set = set()

    def build(
        self,
        row: Dict[str, Any],
        definition: GoldTableDefinition,
    ) -> List[Tuple[URIRef, URIRef, Any]]:
        """Return all triples for a single gold row."""
        triples: List[Tuple[URIRef, URIRef, Any]] = []

        # Subject URI: use the definition's detected id_column (e.g. "hadm_id").
        # id_column may be:
        #   - a single column name (normal case)
        #   - pipe-separated column names (composite key, e.g. "source_airport_id|dest_airport_id|airline_id")
        #   - None → fall back to fm:entityIdentifierColumn annotation, then "id", then _gold_id
        id_col = definition.id_column

        # Fallback subject key when no usable in-gold id column resolves. Prefer the
        # PK-based _bronze_source_ref (unique AND stable across re-materialisation)
        # over the _gold_id surrogate (an auto-increment that can shift between runs).
        # NEVER default to a blind `id` column — a non-unique business code such as
        # schedules.id would silently collapse rows onto one subject.
        def _fallback_key() -> str:
            bsr = row.get("_bronze_source_ref")
            if bsr is not None:
                # bronze_source_ref is "{table}.{key}={value}". Use the {value} — which
                # is the PK value after the silver fix — for a clean, unique, stable URI
                # fragment, equivalent to keying off the PK directly.
                s = str(bsr)
                return s.split("=", 1)[1] if "=" in s else s
            gold_id = row.get("_gold_id")
            return str(gold_id) if gold_id is not None else "unknown"

        if id_col and "|" in id_col:
            # Composite key: join the non-null parts with "_" to form the URI fragment.
            parts = id_col.split("|")
            vals = [row.get(c) for c in parts]
            if all(v is not None for v in vals):
                id_value = "_".join(str(v) for v in vals)
                subject = self._uri_factory.individual(definition.class_label, id_value)
            else:
                subject = self._uri_factory.individual(definition.class_label, _fallback_key())
        else:
            if id_col is None:
                src_col = self._class_id_source_col.get(definition.primary_class_uri)
                if src_col:
                    spec = next(
                        (s for s in definition.column_specs if s.source_column == src_col),
                        None,
                    )
                    id_col = spec.gold_column_name if spec else None
            id_value = row.get(id_col) if id_col else None
            if id_value is not None:
                subject = self._uri_factory.individual(definition.class_label, str(id_value))
            else:
                subject = self._uri_factory.individual(definition.class_label, _fallback_key())

        # Class assertion — emit the discriminated type, all its transitive ancestors,
        # and always also the primary class URI.
        #
        # The primary class guarantee handles two cases:
        #   1. The discriminated type is a NamedIndividual (no rdfs:subClassOf declared),
        #      so the superclass map has no path to the primary class.
        #   2. The discriminated type is a proper subclass but the primary class is several
        #      levels up; the transitive map resolves it but this guard makes it explicit.
        # Either way ?x a :Admission must match all instances of that gold table.
        class_uri = row.get("_ontology_class_uri")
        primary_class_uri = definition.primary_class_uri
        emitted_types: set = set()

        if class_uri:
            triples.append((subject, RDF.type, URIRef(str(class_uri))))
            emitted_types.add(str(class_uri))
            for anc in self._superclass_map.get(str(class_uri), []):
                if anc not in emitted_types:
                    triples.append((subject, RDF.type, URIRef(anc)))
                    emitted_types.add(anc)

        if primary_class_uri:
            if primary_class_uri not in emitted_types:
                triples.append((subject, RDF.type, URIRef(primary_class_uri)))
                emitted_types.add(primary_class_uri)
            for anc in self._superclass_map.get(primary_class_uri, []):
                if anc not in emitted_types:
                    triples.append((subject, RDF.type, URIRef(anc)))
                    emitted_types.add(anc)

        # Nested/packed columns for this gold table → explode instead of literal-emit.
        nested = self._nested.get(definition.gold_table_name, {})
        onto_ns = (
            primary_class_uri.rsplit("#", 1)[0] + "#"
            if primary_class_uri and "#" in primary_class_uri
            else None
        )

        # Property triples
        for spec in definition.column_specs:
            # Nested column: open up the packed cell into edges to first-class
            # entities (subject → <property_uri> → <child>), skipping the opaque
            # literal. The edge reuses the column's OWN mapped property — unpacking
            # repurposes that property from a string literal into an object link.
            if onto_ns and spec.property_uri and spec.gold_column_name in nested:
                triples.extend(
                    explode_nested_cell(
                        subject,
                        row.get(spec.gold_column_name),
                        nested[spec.gold_column_name],
                        self._uri_factory,
                        onto_ns,
                        parent_prop=spec.property_uri,
                        reconcile_by_key=self._reconcile_by_key,
                    )
                )
                continue
            if not spec.property_uri:
                # C2 collision loser: property_uri cleared by _resolve_property_uri_collisions()
                # in GoldDefinitionBuilder to suppress duplicate-predicate emission.
                # The column data is still in gold; no triple is emitted here.
                continue
            value = row.get(spec.gold_column_name)
            if value is None:
                # For object-property FK columns, record the missing reference so the
                # subject is visibly incomplete in the graph — not just absent from joins.
                prop = URIRef(spec.property_uri)
                _incomplete = False

                # Emit fm:incompleteReference for OWL ObjectProperty columns whose
                # range class either:
                #   (a) has a corresponding gold table (true FK join column), OR
                #   (b) has no gold table but IS declared as an OWL class and the
                #       column carries FK-style values (e.g. Synthea payer UUIDs
                #       where gold_payer was never materialised).
                # In both cases a null value means "reference is missing", which
                # must be visible in the graph and countable by C5.
                #
                # Terminal object properties (e.g. hasActiveStatus → Active/Inactive
                # NamedIndividuals with no gold table) are intentionally excluded via
                # the class_uris check: NamedIndividuals are not gold-table entities,
                # so their range class appears in _class_uris but NOT as a value_map
                # target in practice.  The null simply means an unknown categorical.
                #
                # Properties not declared as owl:ObjectProperty at all (DatatypeProperty)
                # are excluded: a null on a DatatypeProperty is a missing data value,
                # not a broken reference.
                _range_class = self._prop_range.get(str(prop))
                if _range_class:
                    # True FK join (gold table exists) OR FK-to-missing-entity (no gold
                    # table but the column's value_map targets this class URI, meaning the
                    # pipeline tried to resolve payer UUIDs to Payer individuals and found
                    # no gold table to mint them from).
                    has_gold_table = bool(self._class_to_entity.get(_range_class))
                    fk_to_missing = (not has_gold_table) and any(
                        v == _range_class for v in (spec.value_map or {}).values()
                    )
                    if has_gold_table or fk_to_missing:
                        _incomplete = True

                if _incomplete:
                    triples.append((subject, _FM.incompleteReference, Literal(str(prop))))
                continue

            prop = URIRef(spec.property_uri)

            # Terminology-coded object property (option b): the cell is a code.
            # Resolve it to a conformed code individual and mint that individual's
            # skos:exactMatch on first sight — full coverage of every code with no
            # per-code minting at generation time.
            if str(prop) in self._coded_props:
                triples.extend(self._emit_coded_value(subject, prop, value))
                continue

            # FK detection path 1: source column name matches another entity
            # (e.g. column "patient" → entity "patient" → gold_patient).
            # Guard: only mint a URI when the column is a *_id column (numeric FK
            # carrying the entity's PK) or when the entity has no known id_column.
            # A bare column named "airline" matches entity "airline" by name but holds
            # IATA codes ("2B"), not the numeric airline_id PK → must emit as literal.
            # A *_id column like "source_airport_id" ends in "_id" and carries the
            # airport PK even though the column name differs from "airport_id".
            referenced_table = self._entity_map.get(spec.source_column)
            if referenced_table and referenced_table != definition.gold_table_name:
                target_id_col = self._entity_id_col.get(spec.source_column)
                if (target_id_col is None
                        or spec.source_column.endswith("_id")
                        or spec.gold_column_name.endswith("_id")
                        or _UUID_RE.match(str(value))):  # BUG-01: UUID value = PK reference
                    obj_uri = self._uri_factory.individual(spec.source_column, str(value))
                    range_class = self._prop_range.get(str(prop))
                    if range_class and range_class == definition.primary_class_uri:
                        triples.append((obj_uri, prop, subject))
                    else:
                        triples.append((subject, prop, obj_uri))
                    continue
                # Non-_id column coincidentally matches an entity name but holds an
                # alternative identifier (e.g. IATA code) — fall through to path 2 / literal.

            # FK detection path 2: property's rdfs:range identifies the target
            # entity even when the column name doesn't match the entity name
            # (e.g. hasDiagnosesIcdAdmission has range=Admission but the column
            # is called hadm_id, not admission).
            # Same guard: *_id columns carry PKs; non-_id columns carry alternative
            # identifiers (e.g. IATA code for airline, IATA code for airport).
            range_class = self._prop_range.get(str(prop))
            target_entity = self._class_to_entity.get(range_class) if range_class else None
            if target_entity and target_entity != definition.gold_table_name[len("gold_"):]:
                target_id_col = self._entity_id_col.get(target_entity)
                if (target_id_col is None
                        or spec.source_column.endswith("_id")
                        or spec.gold_column_name.endswith("_id")
                        or _UUID_RE.match(str(value))           # BUG-01: UUID value = PK reference
                        or target_id_col == spec.source_column  # bare-PK FK: column name ≡ entity PK
                        or target_id_col == spec.gold_column_name):  # same check via gold name
                    # ^ bare-PK FK guard: handles columns like cgid (caregivers) or itemid
                    # (d_items/d_labitems) whose names don't end in _id but ARE the entity's
                    # PK column.  refine_id_columns() identifies these as id_columns; when the
                    # FK column name equals that id_column, the value is a PK reference.
                    obj_uri = self._uri_factory.individual(target_entity, str(value))
                    if range_class == definition.primary_class_uri:
                        triples.append((obj_uri, prop, subject))
                    else:
                        triples.append((subject, prop, obj_uri))
                    continue
                # Non-_id, non-PK-name column: the property is an OWL ObjectProperty
                # whose range is an entity class, but this column holds an alternative
                # identifier (e.g. IATA code "2N" for airline, IATA code for airport).
                # Emitting a literal for an object property is semantically invalid
                # — skip entirely.  The corresponding *_id column emits the correct URI.
                continue

            # BUG-02 guard: the property's rdfs:range class URI.
            # If a value_map or a pre-materialised URI in the gold column equals this
            # class URI (not an individual URI), the mapping is wrong — emitting the
            # class as an ObjectProperty target corrupts join queries.  Emit a quality
            # triple instead so the gap is visible rather than silently wrong.
            _range_class_uri = self._prop_range.get(str(prop))

            if spec.value_map:
                # Try value_map lookup first (raw value → URI).
                # If not found, the value may already be a materialised URI from the gold pipeline.
                mapped_uri = spec.value_map.get(str(value))
                if mapped_uri:
                    if _range_class_uri and mapped_uri == _range_class_uri:
                        # value_map resolved to the range class URI rather than an individual.
                        # Two sub-cases:
                        #   (a) Range class has a gold table → this IS a mapping error (the
                        #       value_map should point at individual URIs, not the class).
                        #       Emit fm:invalidMapping so it doesn't corrupt C5 counts.
                        #   (b) Range class has NO gold table → the pipeline tried to resolve
                        #       e.g. Synthea payer UUIDs to Payer individuals but no gold_payer
                        #       exists.  The value_map correctly names the target class; the gap
                        #       is a missing entity, not a bad mapping.  Emit incompleteReference
                        #       so C5 can count and reconcile it.
                        if self._class_to_entity.get(_range_class_uri):
                            triples.append((subject, _FM_INVALID_MAPPING, Literal(str(prop))))
                        else:
                            triples.append((subject, _FM.incompleteReference, Literal(str(prop))))
                    elif mapped_uri in self._class_uris:
                        # FK-to-missing-class fallback: the value_map resolved to an OWL
                        # CLASS URI instead of an individual URI.  This happens when the
                        # target entity has no gold table (e.g. Payer in Synthea) so no
                        # individual URIs were ever minted.  Emitting the class URI as an
                        # ObjectProperty object corrupts SPARQL join queries; record the
                        # gap as fm:incompleteReference instead.
                        triples.append((subject, _FM.incompleteReference, Literal(str(prop))))
                    else:
                        triples.append((subject, prop, URIRef(mapped_uri)))
                elif isinstance(value, str) and value.startswith(("http://", "https://")):
                    if _range_class_uri and value == _range_class_uri:
                        # BUG-02: gold column stores the range class URI directly.
                        triples.append((subject, _FM_INVALID_MAPPING, Literal(str(prop))))
                    elif value in self._class_uris:
                        # FK-to-missing-class fallback: pre-materialised value is a class URI.
                        triples.append((subject, _FM.incompleteReference, Literal(str(prop))))
                    else:
                        triples.append((subject, prop, URIRef(value)))
                else:
                    datatype = _xsd_type(spec.source_data_type, value)
                    triples.append((subject, prop, Literal(_normalise_literal(value, datatype), datatype=datatype)))
            else:
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    if _range_class_uri and value == _range_class_uri:
                        # BUG-02: pre-materialised URI is the range class, not an individual.
                        triples.append((subject, _FM_INVALID_MAPPING, Literal(str(prop))))
                    elif value in self._class_uris:
                        # FK-to-missing-class fallback: pre-materialised URI is a class.
                        triples.append((subject, _FM.incompleteReference, Literal(str(prop))))
                    else:
                        triples.append((subject, prop, URIRef(value)))
                else:
                    datatype = _xsd_type(spec.source_data_type, value)
                    triples.append((subject, prop, Literal(_normalise_literal(value, datatype), datatype=datatype)))

        # Provenance triples — link the subject back to the gold/silver/bronze audit trail.
        # Without these, prove_answer has no edge from a triple subject to a gold row and
        # every lineage query returns proof_failed with an empty data_layer_trace.
        bronze_ref = row.get("_bronze_source_ref")
        if bronze_ref is not None:
            triples.append((subject, _FM.bronzeSourceRef, Literal(str(bronze_ref))))
        gold_run_id = row.get("_gold_run_id")
        if gold_run_id is not None:
            triples.append((subject, _FM.goldRunId, Literal(str(gold_run_id))))

        return triples

    def _emit_coded_value(
        self,
        subject: URIRef,
        prop: URIRef,
        value: Any,
    ) -> List[Tuple[URIRef, URIRef, Any]]:
        """Emit the link ``subject → prop → codeIndividual`` for a terminology-coded
        property, minting that individual's type, label, and ``skos:exactMatch`` the
        first time the code is seen this run (one per distinct code — the long tail is
        bound here, not at generation).

        A value the source does not recognise still becomes a typed concept individual
        (no ``skos:exactMatch``) so the data is never dropped — the binding is simply
        absent for an unrecognised code.  Concept individuals are reference nodes with
        no bronze row, so they carry no provenance (and are excluded from the entity
        provenance check).
        """
        triples: List[Tuple[URIRef, URIRef, Any]] = []
        code = "" if value is None else str(value).strip()
        if not code:
            return triples
        system_label, confidence = self._coded_props[str(prop)]
        range_class = self._prop_range.get(str(prop))
        if not range_class or "#" not in range_class:
            return triples  # no conformed code class to type/derive the individual under
        onto_ns, class_local = range_class.rsplit("#", 1)
        onto_ns += "#"
        ind = URIRef(onto_ns + _value_slug(code) + class_local)

        triples.append((subject, prop, ind))
        if str(ind) in self._minted_codes:
            return triples
        self._minted_codes.add(str(ind))
        triples.append((ind, RDF.type, URIRef(range_class)))
        triples.append((ind, RDFS.label, Literal(code)))

        source = self._term_sources.get(system_label)
        concept_iri = source.concept_iri(code) if source is not None else None
        if concept_iri:
            triples.append((ind, SKOS.exactMatch, URIRef(concept_iri)))
            triples.append((ind, _FM.terminologySystem, Literal(system_label)))
            triples.append((ind, _FM.terminologyBindingConfidence,
                            Literal(round(confidence, 4), datatype=XSD.decimal)))
            triples.append((ind, _FM.terminologyBindingMethod, Literal("value-set-overlap")))
        return triples
