# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
PropertyNamer — canonical has{CamelCase} naming for OWL datatype properties.

Two public functions:

  canonical_local(source, word_splits) -> str
      Pure function.  Given any raw name — column name, URI local part,
      or already-prefixed variant — returns the canonical local name that
      should be used as the OWL property's URI fragment.

  normalize_datatype_property_uris(graph, base_iri, word_splits) -> int
      Post-generation graph pass.  Renames every owl:DatatypeProperty in
      the graph whose URI is not yet canonical.  Uses skos:altLabel as the
      authoritative source name when present; falls back to the URI local
      part for obviously non-canonical URIs.

      owl:ObjectProperties are left untouched — they are intentionally named
      after their target entity (hasPatient, hasAdmissionType), not the
      source column name.

Naming rules (applied in order):
  1. Strip a leading "has_" or "has" prefix from the source string.
  2. Lowercase the remainder.
  3. Apply optional domain-specific word-split expansions
     (e.g. "dischtime" -> "discharge_time").
  4. Split on underscore / whitespace separators.
  5. Capitalize each token -> CamelCase body.
  6. Prepend "has".

Collision handling is the caller's responsibility; see generator.py
_fill_missing_column_properties for the qualified fallback pattern.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import SKOS

logger = logging.getLogger(__name__)

_SEP_RE = re.compile(r"[_\s]+")


# ---------------------------------------------------------------------------
# Pure naming function
# ---------------------------------------------------------------------------

def canonical_local(
    source: str,
    word_splits: Optional[dict] = None,
) -> str:
    """
    Compute the canonical ``has{CamelCase}`` local name for an OWL property.

    Examples (with MIMIC word_splits)::

        canonical_local("admission_location")   -> "hasAdmissionLocation"
        canonical_local("admittime", ws)         -> "hasAdmitTime"
        canonical_local("dischtime", ws)         -> "hasDischargeTime"
        canonical_local("hasAdmittime", ws)      -> "hasAdmitTime"
        canonical_local("has_chartevents_data")  -> "hasCharteventsData"
        canonical_local("hospital_expire_flag")  -> "hasHospitalExpireFlag"
        canonical_local("hadm_id")               -> "hasHadmId"
        canonical_local("insurance")             -> "hasInsurance"
    """
    ws = word_splits or {}
    s = source.strip()

    # 1. Strip leading has_ or has prefix (case-insensitive)
    lo = s.lower()
    if lo.startswith("has_"):
        s = s[4:]
    elif lo.startswith("has") and len(s) > 3 and not s[3].isdigit():
        s = s[3:]

    # 2. Insert separator at camelCase boundaries BEFORE lowercasing so that
    #    "AdmissionLocation" -> "Admission_Location" retains word structure.
    s = re.sub(r"([a-z])([A-Z])", r"\1_\2", s)

    # 3. Lowercase
    s = s.lower()

    # 4. Split on separators -> individual tokens
    tokens = [t for t in _SEP_RE.split(s) if t]

    # 5. Apply word-splits per token; expansion may itself contain underscores
    expanded: list[str] = []
    for tok in tokens:
        if tok in ws:
            expanded.extend(p for p in _SEP_RE.split(ws[tok]) if p)
        else:
            expanded.append(tok)

    if not expanded:
        return f"has{source.strip().capitalize()}"

    # 6. Capitalize each token and prepend has
    camel = "".join(p.capitalize() for p in expanded)
    return f"has{camel}"


def _is_canonical(local: str) -> bool:
    """True if ``local`` is already ``has{UpperCamelCase}`` with no separators."""
    if not local.startswith("has"):
        return False
    rest = local[3:]
    return bool(rest) and rest[0].isupper() and "_" not in rest and " " not in rest


# ---------------------------------------------------------------------------
# Graph normalization pass
# ---------------------------------------------------------------------------

def normalize_datatype_property_uris(
    graph: Graph,
    base_iri: str,
    word_splits: Optional[dict] = None,
) -> int:
    """
    Rename every ``owl:DatatypeProperty`` in *graph* to its canonical URI.

    Source priority per property:
    1. ``skos:altLabel`` — the original column name; most reliable.
    2. URI local name — only when not already canonical, to avoid breaking
       correctly-named properties that happen to have no altLabel.

    When the computed canonical URI already exists in the graph as a
    *different* property, the renaming is skipped and a warning is logged
    (the caller's deduplication pass will reconcile them).

    Returns the count of properties actually renamed.
    """
    renamed = 0

    for prop in list(graph.subjects(RDF.type, OWL.DatatypeProperty)):
        prop_str = str(prop)
        local = prop_str.split("#")[-1].split("/")[-1]

        # Choose source name
        alt_labels = list(graph.objects(prop, SKOS.altLabel))
        if alt_labels:
            source = str(alt_labels[0])
        elif not _is_canonical(local):
            source = local
        else:
            continue  # already canonical, no altLabel to guide us

        new_local = canonical_local(source, word_splits)
        new_uri = URIRef(base_iri + new_local)

        if new_uri == prop:
            continue

        # If new URI already occupied by a DIFFERENT property, skip
        # (dedup pass resolves same-altLabel duplicates afterward)
        if (new_uri, RDF.type, OWL.ObjectProperty) in graph:
            # The column is already modelled as an ObjectProperty (e.g. a categorical
            # or FK relationship).  The DataProperty is a redundant, wrong-type
            # duplicate — remove it entirely.
            if alt_labels:  # only remove when we have authoritative column evidence
                # Transfer domain to ObjectProperty before deleting the DataProp —
                # the DataProp's domain triple is the authoritative class link.
                if not list(graph.objects(new_uri, RDFS.domain)):
                    for _, _, domain in list(graph.triples((prop, RDFS.domain, None))):
                        graph.add((new_uri, RDFS.domain, domain))
                        logger.debug(
                            "property_namer: transferred domain <%s> to ObjectProperty <%s>",
                            domain, new_uri,
                        )
                for s, p, o in list(graph.triples((prop, None, None))):
                    graph.remove((s, p, o))
                for s, p, o in list(graph.triples((None, None, prop))):
                    graph.remove((s, p, o))
                logger.debug(
                    "property_namer: removed DataProperty <%s> — shadowed by ObjectProperty <%s>",
                    prop, new_uri,
                )
            else:
                logger.debug(
                    "property_namer: skipped rename <%s> → <%s>: target ObjectProperty occupied",
                    prop, new_uri,
                )
            continue

        if (new_uri, RDF.type, OWL.DatatypeProperty) in graph:
            logger.debug(
                "property_namer: skipped rename <%s> → <%s>: target DataProperty occupied",
                prop, new_uri,
            )
            continue

        # Rewrite all triples involving the old URI
        for s, p, o in list(graph.triples((prop, None, None))):
            graph.remove((s, p, o))
            graph.add((new_uri, p, o))
        for s, p, o in list(graph.triples((None, None, prop))):
            graph.remove((s, p, o))
            graph.add((s, p, new_uri))

        logger.debug("property_namer: renamed <%s> → <%s>", prop, new_uri)
        renamed += 1

    if renamed:
        logger.info(
            "property_namer: normalized %d datatype property URIs", renamed
        )

    return renamed
