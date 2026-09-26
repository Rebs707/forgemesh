# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from abc import ABC, abstractmethod
from typing import Any, List, Optional, Tuple

from rdflib import URIRef

from semantic_mapper.rdf.models import RdfPipelineStats


class RdfWriter(ABC):
    """
    Abstract writer interface for the RDF layer.

    Extension point
    ---------------
    Register alternative implementations (Fuseki, rdflib in-memory) via
    the _REGISTRY in factory.py.
    """

    @abstractmethod
    def initialise(
        self,
        dataset_name: str,
        named_graph: str,
        ontology_path: Optional[str] = None,
    ) -> None:
        """Create/clear the named graph. Load ontology if path is given.

        Raises RdfSchemaInitialisationError on failure.
        """
        ...

    @abstractmethod
    def write_batch(self, triples: List[Tuple[URIRef, URIRef, Any]]) -> int:
        """Write a batch of triples. Returns count written.

        Raises RdfWriterError on failure.
        """
        ...

    @abstractmethod
    def finalise(
        self,
        run_id: str,
        triples_written: int,
        tables_processed: List[str],
        project_name: Optional[str] = None,
        ontology_source: Optional[str] = None,
    ) -> RdfPipelineStats:
        """Commit and return pipeline stats with duration populated.

        ``project_name`` is stamped onto the named graph node as
        ``fm:projectName`` when provided.  ``ontology_source`` (the OWL
        filename basename) is stamped as ``fm:ontologySource`` so C6 can
        detect ontology drift between runs.
        """
        ...
