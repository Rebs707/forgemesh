# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Manchester Syntax → rdflib triples parser.

Uses regex extraction for Class, DatatypeProperty, and ObjectProperty blocks.
Handles classes (with labels and subClassOf), object properties (domain/range/label),
and datatype properties (domain/range/label/skos:altLabel/rdfs:comment).

Never raises — returns an empty graph on total failure.
"""
import logging
import re
from typing import List, Tuple

import rdflib
from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef
from rdflib.namespace import SKOS

from semantic_mapper.ontology.iterative.prefix_leak_fix import normalise_entity_name

logger = logging.getLogger(__name__)

Triple = Tuple[URIRef, URIRef, rdflib.term.Node]

_BLOCK_SENTINEL = r"(?=\nClass\s*:|\nObjectProperty\s*:|\nDatatypeProperty\s*:|\nIndividual\s*:|$)"

_CLASS_BLOCK_RE = re.compile(
    r"Class\s*:\s*(<[^>]+>|[\w:]+)\s*(.*?)" + _BLOCK_SENTINEL,
    re.DOTALL,
)
_OBJ_PROP_BLOCK_RE = re.compile(
    r"ObjectProperty\s*:\s*(<[^>]+>|[\w:]+)\s*(.*?)" + _BLOCK_SENTINEL,
    re.DOTALL,
)
_DATA_PROP_BLOCK_RE = re.compile(
    r"DatatypeProperty\s*:\s*(<[^>]+>|[\w:]+)\s*(.*?)" + _BLOCK_SENTINEL,
    re.DOTALL,
)
_INDIVIDUAL_BLOCK_RE = re.compile(
    r"Individual\s*:\s*(<[^>]+>|[\w:]+)\s*(.*?)" + _BLOCK_SENTINEL,
    re.DOTALL,
)
_RDFS_LABEL_RE    = re.compile(r'rdfs:label\s+"([^"]+)"(?:@(\w+))?')
_SKOS_ALT_RE      = re.compile(r'skos:altLabel\s+"([^"]+)"(?:@(\w+))?')
_RDFS_COMMENT_RE  = re.compile(r'rdfs:comment\s+"([^"]+)"(?:@(\w+))?')
_DOMAIN_RE        = re.compile(r'Domain\s*:\s*(<[^>]+>|[\w:]+)')
_RANGE_RE         = re.compile(r'Range\s*:\s*(<[^>]+>|[\w:]+)')
_SUBCLASS_RE      = re.compile(r'SubClassOf\s*:\s*(<[^>]+>|[\w:]+)')
_SUBPROPERTY_RE   = re.compile(r'SubPropertyOf\s*:\s*(<[^>]+>|[\w:]+)')
_TYPES_RE         = re.compile(r'Types\s*:\s*(<[^>]+>|[\w:]+)')

# fm: annotation property patterns
_FM_SOURCE_TABLE_RE  = re.compile(r'fm:sourceTable\s+"([^"]+)"')
_FM_SOURCE_COLUMN_RE = re.compile(r'fm:sourceColumn\s+"([^"]+)"')
_FM_SOURCE_FK_RE     = re.compile(r'fm:sourceForeignKey\s+"([^"]+)"')
_FM_SQL_TYPE_RE      = re.compile(r'fm:sqlType\s+"([^"]+)"')
_FM_NULL_RATE_RE     = re.compile(r'fm:nullRate\s+"([^"]+)"')


def parse_manchester(
    manchester_syntax: str,
    base_iri: str,
    table_name: str = "",
) -> Tuple[Graph, List[str], List[str]]:
    """
    Parse Manchester Syntax into an rdflib Graph.

    Returns:
        (graph, classes_declared, properties_declared)

    Never raises — on total failure returns an empty graph and logs an error.
    """
    try:
        return _parse(manchester_syntax, base_iri, table_name)
    except Exception as exc:
        logger.error(
            "manchester_parser: parse failed for '%s': %s — returning empty graph",
            table_name, exc,
        )
        return Graph(), [], []


_XSD_BARE = {
    "string", "integer", "int", "decimal", "double", "float",
    "boolean", "dateTime", "date", "time", "anyURI", "long",
}

def _resolve_uri(token: str, base_iri: str) -> str:
    if token.startswith("<") and token.endswith(">"):
        return token[1:-1]
    # Strip any invented namespace prefix (e.g. "Ontology:Admission" -> "Admission",
    # "hasOntology:Caregiver" -> "hasCaregiver"). Known prefixes (xsd, skos, fm, …)
    # are left untouched; the prefix_map resolution below handles them correctly.
    token = normalise_entity_name(token)
    # Bare XSD type names without prefix (e.g. LLM wrote "Range: integer")
    if token in _XSD_BARE:
        return "http://www.w3.org/2001/XMLSchema#" + token
    if ":" in token and not token.startswith(":"):
        prefix, local = token.split(":", 1)
        prefix_map = {
            "owl":  "http://www.w3.org/2002/07/owl#",
            "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
            "rdf":  "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "xsd":  "http://www.w3.org/2001/XMLSchema#",
            "skos": "http://www.w3.org/2004/02/skos/core#",
            "fm":   "http://forgemesh.ai/ontology#",
        }
        if prefix in prefix_map:
            return prefix_map[prefix] + local
    return base_iri + token.lstrip(":")


def _parse(
    manchester_syntax: str,
    base_iri: str,
    table_name: str,
) -> Tuple[Graph, List[str], List[str]]:
    body = _strip_fences(manchester_syntax)

    g = Graph()
    g.bind("owl", OWL)
    g.bind("rdfs", RDFS)
    g.bind("skos", SKOS)
    g.bind("xsd", XSD)
    FM_NS = rdflib.Namespace("http://forgemesh.ai/ontology#")
    g.bind("fm", FM_NS)

    SKOS_ALT     = URIRef("http://www.w3.org/2004/02/skos/core#altLabel")
    RDFS_COMMENT = URIRef("http://www.w3.org/2000/01/rdf-schema#comment")

    classes_declared: List[str] = []
    props_declared:   List[str] = []

    for m in _CLASS_BLOCK_RE.finditer(body):
        uri_str = _resolve_uri(m.group(1), base_iri)
        uri = URIRef(uri_str)
        classes_declared.append(uri_str)
        g.add((uri, RDF.type, OWL.Class))
        block = m.group(2)
        for lm in _RDFS_LABEL_RE.finditer(block):
            g.add((uri, RDFS.label, Literal(lm.group(1), lang=lm.group(2) or "en")))
        for cm in _RDFS_COMMENT_RE.finditer(block):
            g.add((uri, RDFS_COMMENT, Literal(cm.group(1), lang=cm.group(2) or "en")))
        for sm in _SUBCLASS_RE.finditer(block):
            g.add((uri, RDFS.subClassOf, URIRef(_resolve_uri(sm.group(1), base_iri))))
        for fm in _FM_SOURCE_TABLE_RE.finditer(block):
            g.add((uri, FM_NS.sourceTable, Literal(fm.group(1))))

    for m in _OBJ_PROP_BLOCK_RE.finditer(body):
        uri_str = _resolve_uri(m.group(1), base_iri)
        uri = URIRef(uri_str)
        props_declared.append(uri_str)
        g.add((uri, RDF.type, OWL.ObjectProperty))
        block = m.group(2)
        for lm in _RDFS_LABEL_RE.finditer(block):
            g.add((uri, RDFS.label, Literal(lm.group(1), lang=lm.group(2) or "en")))
        for cm in _RDFS_COMMENT_RE.finditer(block):
            g.add((uri, RDFS_COMMENT, Literal(cm.group(1), lang=cm.group(2) or "en")))
        for dm in _DOMAIN_RE.finditer(block):
            g.add((uri, RDFS.domain, URIRef(_resolve_uri(dm.group(1), base_iri))))
        for rm in _RANGE_RE.finditer(block):
            g.add((uri, RDFS.range, URIRef(_resolve_uri(rm.group(1), base_iri))))
        for spm in _SUBPROPERTY_RE.finditer(block):
            g.add((uri, RDFS.subPropertyOf, URIRef(_resolve_uri(spm.group(1), base_iri))))
        for fm in _FM_SOURCE_FK_RE.finditer(block):
            g.add((uri, FM_NS.sourceForeignKey, Literal(fm.group(1))))

    for m in _DATA_PROP_BLOCK_RE.finditer(body):
        uri_str = _resolve_uri(m.group(1), base_iri)
        uri = URIRef(uri_str)
        props_declared.append(uri_str)
        g.add((uri, RDF.type, OWL.DatatypeProperty))
        block = m.group(2)
        for lm in _RDFS_LABEL_RE.finditer(block):
            g.add((uri, RDFS.label, Literal(lm.group(1), lang=lm.group(2) or "en")))
        for cm in _RDFS_COMMENT_RE.finditer(block):
            g.add((uri, RDFS_COMMENT, Literal(cm.group(1), lang=cm.group(2) or "en")))
        for dm in _DOMAIN_RE.finditer(block):
            g.add((uri, RDFS.domain, URIRef(_resolve_uri(dm.group(1), base_iri))))
        for rm in _RANGE_RE.finditer(block):
            g.add((uri, RDFS.range, URIRef(_resolve_uri(rm.group(1), base_iri))))
        for am in _SKOS_ALT_RE.finditer(block):
            g.add((uri, SKOS_ALT, Literal(am.group(1), lang=am.group(2) or "en")))
        for spm in _SUBPROPERTY_RE.finditer(block):
            g.add((uri, RDFS.subPropertyOf, URIRef(_resolve_uri(spm.group(1), base_iri))))
        for fm in _FM_SOURCE_COLUMN_RE.finditer(block):
            g.add((uri, FM_NS.sourceColumn, Literal(fm.group(1))))
        for fm in _FM_SQL_TYPE_RE.finditer(block):
            g.add((uri, FM_NS.sqlType, Literal(fm.group(1))))
        for fm in _FM_NULL_RATE_RE.finditer(block):
            g.add((uri, FM_NS.nullRate, Literal(fm.group(1))))

    for m in _INDIVIDUAL_BLOCK_RE.finditer(body):
        uri_str = _resolve_uri(m.group(1), base_iri)
        uri = URIRef(uri_str)
        g.add((uri, RDF.type, OWL.NamedIndividual))
        block = m.group(2)
        for lm in _RDFS_LABEL_RE.finditer(block):
            g.add((uri, RDFS.label, Literal(lm.group(1), lang=lm.group(2) or "en")))
        for tm in _TYPES_RE.finditer(block):
            type_uri = URIRef(_resolve_uri(tm.group(1), base_iri))
            g.add((uri, RDF.type, type_uri))

    logger.debug(
        "manchester_parser: parsed '%s' — %d classes, %d properties",
        table_name, len(classes_declared), len(props_declared),
    )
    return g, classes_declared, props_declared


def _strip_fences(text: str) -> str:
    """Remove ```manchester, ```omn, or ``` fences if present."""
    text = re.sub(r"^```[\w]*\n?", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text.strip(), flags=re.MULTILINE)
    return text.strip()
