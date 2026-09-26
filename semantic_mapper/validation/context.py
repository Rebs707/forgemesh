# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""ValidationContext: bundles all inputs the check suite needs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from .sparql_backend import SparqlBackend


@dataclass
class NLQuestion:
    """A natural-language question with a known-correct non-empty answer."""
    question: str
    expect_non_empty: bool = True  # False if the correct answer is genuinely empty


@dataclass
class ValidationContext:
    """All inputs needed to run the full check suite against one dataset.

    Parameters
    ----------
    project_name : str
        Active project name — used by C6 (project identity check).
    graph_uri : str
        Named graph URI, e.g. ``http://mesh.local/graph/openflights``.
    sparql : SparqlBackend
        SPARQL SELECT executor — RdflibBackend in tests, FusekiBackend in prod.
    gold_engine : Any
        SQLAlchemy engine connected to the gold database.
    mapping_report : Any
        ``SemanticMappingReport`` from the most recent successful map phase.
    ontology : Any
        ``rdflib.Graph`` of the active OWL ontology.
    baseline_run_id : str
        Run ID of the most recent successful RDF phase — used by C6.
    nl_questions : list of NLQuestion
        Fixed questions with known-correct answers for C7.
    settings : optional Settings
        Needed only by C7 (NL-to-SPARQL check) to construct the query engine.
    """

    project_name: str
    graph_uri: str
    sparql: SparqlBackend
    gold_engine: Any
    mapping_report: Any          # SemanticMappingReport
    ontology: Any                # rdflib.Graph
    baseline_run_id: str
    nl_questions: List[NLQuestion] = field(default_factory=list)
    settings: Optional[Any] = None   # Settings — only needed for C7
    ontology_source: str = ""    # OWL filename basename, e.g. "mimic_bronze_rigor_260527_001.owl"
