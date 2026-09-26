# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import logging
import os
from typing import Optional

import rdflib

from semantic_mapper.core.exceptions import OntologyLoadError
from semantic_mapper.ontology.annotation_sweep import (
    remove_legacy_provenance_comments,
    sweep_provenance_annotations,
)

logger = logging.getLogger(__name__)


class OntologyLoader:
    @staticmethod
    def load(path: str, mapping_report=None) -> rdflib.Graph:
        """Load an OWL file and run the provenance annotation sweep.

        Args:
            path:           Filesystem path to the OWL/XML file.
            mapping_report: Optional SemanticMappingReport passed to the sweep
                            for richer synthesis of missing rdfs:comment values.
                            When None the sweep falls back to URI local names.
        """
        if not os.path.exists(path):
            raise OntologyLoadError(f"Ontology file not found at {path}")

        g = rdflib.Graph()
        try:
            g.parse(path, format="xml")
        except Exception as e:
            raise OntologyLoadError(f"Failed to parse OWL file: {e}")

        result = sweep_provenance_annotations(g, mapping_report)
        removed = remove_legacy_provenance_comments(g)
        if removed:
            logger.info(
                "OntologyLoader: removed %d legacy 'Derived from' rdfs:comment triples",
                removed,
            )
        if result.synthesised_classes or result.synthesised_data_properties or result.synthesised_object_properties:
            logger.warning(
                "OntologyLoader: annotation sweep synthesised %d class, "
                "%d data property, and %d object property fm: annotation(s) — "
                "ontology may predate Phase 0b",
                result.synthesised_classes,
                result.synthesised_data_properties,
                result.synthesised_object_properties,
            )

        return g
