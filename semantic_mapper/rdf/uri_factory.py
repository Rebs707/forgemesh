# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import hashlib
import urllib.parse

from rdflib import URIRef


def hash_identifier(id_value) -> str:
    """Deterministic 64-bit (16 hex) hash of an entity identifier.

    The single, canonical transform used for BOTH entity subject URIs and FK-target
    URIs, so a foreign key reconstructs the exact same hash from the referenced PK
    value and the join holds. Normalisation (str + strip) is part of the contract:
    the same logical value must always hash identically regardless of call site.

    64 bits keeps birthday collisions astronomically unlikely (~4e9 distinct values
    per class) while staying short and URI-safe — independent of how ugly the raw
    key is (composite, unicode, spaces, long text).
    """
    return hashlib.sha256(str(id_value).strip().encode("utf-8")).hexdigest()[:16]


class UriFactory:
    """Mint globally-unique URIs for RDF entity instances.

    URI structure (issue #46):
    - Without source scoping: ``{base_uri}/{entity_name}/{id_value}``
    - With source scoping:    ``{base_uri}/{source_name}/{entity_name}/{id_value}``

    Source scoping is the recommended strategy for multi-source pipelines because
    it prevents collisions when the same ``id_value`` exists in different source
    systems (databases, files, APIs).  Set ``source_name`` to the database or
    system name when ingesting from multiple sources.

    Note: ``base_uri`` (data namespace, e.g. ``http://mesh.local/data/``) is
    distinct from the *ontology namespace* (e.g. ``http://mesh.local/ontology/``).
    The data namespace identifies *instances*; the ontology namespace identifies
    *classes and properties*.  Keep these separate so that ontology upgrades do
    not invalidate existing data URIs.
    """

    def __init__(self, base_uri: str, source_name: str = "") -> None:
        self._base = base_uri.rstrip("/") + "/"
        # Percent-encode the source name so arbitrary DB names are URI-safe
        self._source_prefix = (
            urllib.parse.quote(source_name.strip(), safe="") + "/"
            if source_name.strip()
            else ""
        )

    def individual(self, entity_name: str, id_value: str) -> URIRef:
        """Mint a URI for an entity instance.

        Without source scoping:
            ("patient", "abc-123") → <http://mesh.local/data/patient/abc-123>

        With source_name="synthea":
            ("patient", "abc-123") → <http://mesh.local/data/synthea/patient/abc-123>

        The id_value is reduced to a deterministic 64-bit hash (uniform, opaque, and
        URI-safe for any key shape); the entity/class name stays readable in the path
        for namespacing. The same transform is used wherever an FK reconstructs this
        URI, so joins resolve.
        """
        return URIRef(
            f"{self._base}{self._source_prefix}"
            f"{urllib.parse.quote(str(entity_name), safe='')}/{hash_identifier(id_value)}"
        )
