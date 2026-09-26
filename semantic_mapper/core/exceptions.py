# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
class SemanticMapperError(Exception):
    """Base exception for the semantic mapping pipeline."""
    pass

class DatabaseConnectionError(SemanticMapperError):
    """Raised when the MySQL source is unreachable."""
    pass

class OntologyLoadError(SemanticMapperError):
    """Raised when the OWL/RDF file is invalid or missing."""
    pass

class ProfilingError(SemanticMapperError):
    """Raised when data introspection fails."""
    pass

class MappingConfigError(SemanticMapperError):
    """Raised when configuration thresholds are invalid."""
    pass

class SilverLayerError(SemanticMapperError):
    """Base exception for silver layer failures."""
    pass

class TransformationError(SilverLayerError):
    """Raised on data-level transformation failure."""
    pass

class WriterError(SilverLayerError):
    """Raised on target-system write failure."""
    pass

class SchemaInitialisationError(SilverLayerError):
    """Raised when DDL creation/migration fails."""
    pass


class GoldLayerError(SemanticMapperError):
    """Base exception for gold layer failures."""
    pass

class GoldWriterError(GoldLayerError):
    """Raised on gold target-system write failure."""
    pass

class GoldSchemaInitialisationError(GoldLayerError):
    """Raised when gold DDL creation/migration fails."""
    pass


class RdfLayerError(SemanticMapperError):
    """Base exception for RDF layer failures."""
    pass

class RdfWriterError(RdfLayerError):
    """Raised on RDF target-system write failure."""
    pass

class RdfSchemaInitialisationError(RdfLayerError):
    """Raised when RDF dataset/graph initialisation fails."""
    pass


class QueryError(SemanticMapperError):
    """Base exception for semantic query interface failures."""
    pass


class QuerySafetyError(QueryError):
    """Raised when generated SQL contains DML or DDL statements."""
    pass


class QueryLLMError(QueryError):
    """Raised when the LLM API call fails or returns an unparseable response."""
    pass


class OntologyGroundingError(QueryError):
    """Raised when a proof claim cannot be grounded in the ontology subgraph.

    E.g. a SPARQL filter references a property URI that is absent from the
    supplied ontology, so the narrator cannot produce a warranted AppliedConstraint.
    """
    pass
